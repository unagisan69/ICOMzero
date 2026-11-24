"""Non-blocking station controller; UI actions explicitly arm a contact/CQ."""

import queue
import csv
import math
import shutil
import json
import re
import os
import subprocess
import threading
import time
from radio import FT8_RATE
from ft8qso import Sequencer, waveform, finalize_contact
from ft8log import load_settings, directory
from ft8auto import AutoCQ, SessionLog
from notifications import Sounds


def chrony_health(report, now=None):
    """Check current remaining correction, not a historical NTP sample."""
    fields = next(csv.reader([report.strip()]))
    if len(fields) == 14:
        # Current chronyc CSV includes a reference-name column after its ID.
        fields = [fields[0]] + fields[2:]
    if len(fields) != 13:
        raise ValueError("Invalid chrony tracking report")
    reference, stratum = fields[0], int(fields[1])
    stamp, correction = float(fields[2]), float(fields[3])
    skew, delay, dispersion, interval = map(float, fields[8:12])
    if not all(
        math.isfinite(v) for v in (stamp, correction, skew, delay, dispersion, interval)
    ):
        raise ValueError("Invalid chrony tracking values")
    if (
        fields[12] not in ("Normal", "Insert second", "Delete second")
        or not 1 <= stratum <= 15
        or reference.upper() in ("00000000", "7F7F0101")
    ):
        return False, "Clock not synchronized"
    age = (time.time() if now is None else now) - stamp
    if not -5 <= age <= max(180, min(900, interval * 3)):
        return False, "Clock reference stale; TX blocked"
    # Include network uncertainty and drift since the last accepted sample.
    uncertainty = (
        abs(correction)
        + abs(delay) / 2
        + abs(dispersion)
        + max(0, age) * abs(skew) / 1e6
    )
    if abs(correction) > 0.250:
        return False, f"Clock correction {correction*1000:+.0f}ms; TX blocked"
    if uncertainty > 0.250:
        return False, f"Clock uncertainty {uncertainty*1000:.0f}ms; TX blocked"
    return True, f"Clock synced ({correction*1000:+.1f}ms)"


def clock_health():
    try:
        env = {**os.environ, "LC_ALL": "C"}
        sync = (
            subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True,
                timeout=2,
                check=True,
                env=env,
            ).stdout.strip()
            == b"yes"
        )
        if not sync:
            return False, "Clock not synchronized"
        if shutil.which("chronyc"):
            tracking = subprocess.run(
                ["chronyc", "-n", "-c", "tracking"],
                capture_output=True,
                timeout=2,
                env=env,
            )
            if tracking.returncode == 0:
                return chrony_health(tracking.stdout.decode(errors="replace"))
        result = subprocess.run(
            ["timedatectl", "timesync-status"], capture_output=True, timeout=2, env=env
        )
        if result.returncode != 0:
            return False, "Clock sync could not be verified"
        match = re.search(
            r"Offset:\s*([+-]?[0-9.]+)(us|ms|s)", result.stdout.decode(errors="replace")
        )
        if match:
            offset = float(match[1]) * {"us": 0.001, "ms": 1, "s": 1000}[match[2]]
            if abs(offset) > 250:
                return False, f"Clock offset {offset:+.0f}ms; TX blocked"
            return True, f"Clock synced ({offset:+.1f}ms)"
        return True, "Clock synchronized"
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, "Clock sync could not be verified"


def clock_synced():
    return clock_health()[0]


def record_fault(receiver, reason, job=None):
    """Private, bounded diagnostics; never store credentials or decoded traffic."""
    try:
        state = receiver.radio_state
        now = time.monotonic()
        data = {
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "fault": reason,
            "model": state.get("profile", {}).get("model"),
            "frequency": receiver.frequency,
            "power_control": state.get("tx_power"),
            "readiness": {
                name: state.get(name)
                for name in (
                    "tx_ready",
                    "civ_recovering",
                    "mod_source",
                    "data_mode",
                    "data_filter",
                    "mode",
                    "power_ready",
                    "split",
                    "rit",
                    "xit",
                )
            },
            "transport_gap_ms": state.get("transport_gap_ms"),
            "transport_gap_age_s": (
                round(now - state["transport_gap_at"], 3)
                if state.get("transport_gap_at") is not None
                else None
            ),
            "ptt": state.get("ptt"),
            "ptt_pending": state.get("ptt_pending"),
            "tuner": state.get("tuner"),
        }
        current = job if job is not None else receiver.tx_current
        data["audio_started"] = bool(
            current
            and current.started.is_set()
            and (job is not None or not current.done.is_set())
        )
        for name in ("ptt_on_sent", "ptt_readback"):
            stamp = state.get(name + "_at")
            data[name + "_age_s"] = round(now - stamp, 3) if stamp is not None else None
        for name in ("control_rx", "cat_rx", "audio_rx", "civ_reply"):
            stamp = state.get(name + "_at")
            data[name + "_age_s"] = round(now - stamp, 3) if stamp is not None else None
        for name in ("swr", "alc", "watts"):
            data[name] = state.get(name)
            stamp = state.get(name + "_at")
            data[name + "_age_s"] = round(now - stamp, 3) if stamp is not None else None
        directory().mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory() / "rf-diagnostics.jsonl"
        if path.exists() and path.stat().st_size > 131072:
            path.replace(directory() / "rf-diagnostics.previous.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(data) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def record_power_summary(receiver, summary):
    try:
        root = directory()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "rf-power.jsonl"
        if path.exists() and path.stat().st_size > 131072:
            path.replace(root / "rf-power.previous.jsonl")
        data = {
            **summary,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "frequency": receiver.frequency,
            "model": receiver.radio_state.get("profile", {}).get("model"),
            "power_control": receiver.radio_state.get("tx_power"),
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(data) + "\n")
    except (OSError, TypeError, ValueError):
        pass


class Station:
    def __init__(self, receiver, decoder, logbook):
        self.receiver = receiver
        self.decoder = decoder
        self.audio_rate = getattr(decoder, "rate", FT8_RATE)
        self.logbook = logbook
        self.commands = queue.Queue()
        self.closed = threading.Event()
        self.version = 0
        self.status = "Receive only"
        self.active = False
        self.tuning = False
        self.tune_phase = None
        self.tune_issued = None
        self.tune_deadline = 0
        self.clock_status = "Checking clock"
        self.clock_ok = False
        self.qso_revision = 0
        self.saved_qso = None
        self.sequence = Sequencer(load_settings(), self.complete_contact)
        self.journal = None
        self.auto = AutoCQ(self.sequence, self.audit, self.previously_worked)
        self.sounds = Sounds(
            lambda: receiver.radio_state.get("ptt") is False
            and not receiver.radio_state.get("ptt_pending")
        )
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    @property
    def current_partner(self):
        s = self.sequence
        return s.partner if s.enabled and s.stage not in ("cq", "monitor") else ""

    def previously_worked(self, call, dial=None):
        if (
            not self.sequence.settings.get("hide_worked", False)
            or call == self.current_partner
        ):
            return False
        from ft8 import BANDS

        band = next(
            (
                name
                for name, hz in BANDS
                if hz == (self.sequence.dial if dial is None else dial)
            ),
            "",
        )
        return call in self.logbook.worked_calls(band, self.sequence.settings)

    @property
    def auto_active(self):
        return self.auto.active

    @property
    def automation_label(self):
        return "POTA" if self.auto.mode == "pota" else "AUTO"

    @property
    def queued_count(self):
        return len(self.auto.queue)

    def audit(self, event, **fields):
        if self.journal is None:
            raise OSError("Auto CQ journal unavailable")
        self.journal.emit(event, **fields)

    def settle_auto(self):
        try:
            self.auto.synchronize()
            if self.journal is not None:
                if self.auto.active:
                    self.journal.flush()
                else:
                    self.journal.close()
                    self.journal = None
                    if self.auto.finished_no_response:
                        self.sounds.play("auto_stop")
        except OSError:
            self.receiver.stop_tx()
            self.sequence.stop("Auto CQ log failed; TX stopped")
            self.auto.active = False
            self.sequence.repeat_limit = 5
            self.sequence.timeout_seconds = 180
            if self.journal is not None:
                try:
                    self.journal.close()
                except OSError:
                    pass
                self.journal = None

    def complete_contact(self, qso):
        added = self.logbook.add(qso)
        if added is not False:
            self.saved_qso = dict(qso)
            self.qso_revision += 1
            if getattr(self, "sounds", None):
                self.sounds.play("qso")
        if getattr(self, "auto", None) and self.auto.active:
            self.auto.record("qso_saved", qso=dict(qso), newly_saved=added is not False)
        return added

    def action(self, action, value=None, dial=7074000):
        if action in ("cq", "reply", "auto_cq", "pota_hunt") and self.tuning:
            self.status = "Wait for automatic tuning"
            return
        self.version += 1
        self.receiver.stop_tx()
        if action in ("stop", "tune", "record_qso"):
            self.receiver.command("tuner_stop")
        self.commands.put((self.version, action, value, dial))
        if action == "tune":
            self.tuning = True
        if action == "stop":
            self.active = False
            self.tuning = False
            self.status = "Transmit stopped"
        elif action in ("cq", "reply", "auto_cq", "pota_hunt"):
            self.active = True

    def run(self):
        version = 0
        job = None
        pending = None
        cache = {}
        prepared_at = 0
        setup_confirmed = False
        clock_at = 0
        clearing_at = None
        logged_fault = None
        deferred_tx = []
        power_summary_at = 0
        audit_at = 0
        last_wall, last_mono = time.time(), time.monotonic()
        while not self.closed.wait(0.03):
            wall, mono = time.time(), time.monotonic()
            if abs((wall - last_wall) - (mono - last_mono)) > 0.15:
                self.receiver.stop_tx()
                self.sequence.stop("Clock changed; transmit stopped")
            last_wall, last_mono = wall, mono
            if time.monotonic() >= clock_at:
                self.clock_ok, self.clock_status = clock_health()
                clock_at = time.monotonic() + 15
                if not self.clock_ok and self.sequence.enabled:
                    self.receiver.stop_tx()
                    self.sequence.stop(self.clock_status)
            if (
                job is not None
                and job.started.is_set()
                and not getattr(job, "listed", False)
            ):
                deferred_tx.append(
                    (pending[3] + 1, pending[3], self.sequence.hz, pending[2])
                )
                job.listed = True
                if self.auto.active:
                    try:
                        self.auto.record(
                            "tx_audio_started",
                            text=pending[2],
                            tx_slot=pending[3],
                            observed_wall=time.time(),
                        )
                    except OSError:
                        self.receiver.stop_tx()
            for item in deferred_tx[:]:
                if time.time() >= item[0] * 15:
                    self.decoder.record_tx(item[1], item[2], item[3])
                    deferred_tx.remove(item)
            try:
                while True:
                    current, action, value, dial = self.commands.get_nowait()
                    if current != self.version:
                        continue
                    version = current
                    if self.auto.active:
                        self.auto.stop("Operator action: " + action)
                    self.settle_auto()
                    self.sequence.stop()
                    job = pending = None
                    cache.clear()
                    if action == "stop":
                        self.tuning = False
                        continue
                    if action == "clear_fault":
                        self.receiver.command("clear_fault")
                        clearing_at = time.monotonic()
                        self.status = "Waiting for confirmed RX"
                        self.sequence.status = self.status
                        continue
                    if action == "record_qso":
                        qso = finalize_contact(value)
                        qso["manual"] = True
                        added = self.complete_contact(qso)
                        self.sequence.status = (
                            "Already logged " if added is False else "Logged "
                        ) + qso["call"]
                        self.tuning = False
                        continue
                    if self.receiver.radio_state.get("tx_fault"):
                        raise ValueError("Press Enter to clear the RF warning first")
                    if action == "tune":
                        self.tuning = False
                        profile = self.receiver.radio_state.get("profile", {})
                        if not (profile.get("tuner") and profile.get("transmit")):
                            self.sequence.status = "Automatic tuner unavailable"
                            continue
                        if self.receiver.frequency != dial:
                            raise ValueError("Wait for band readback")
                        self.receiver.command(
                            "ft8_power", int(load_settings()["tx_power_w"])
                        )
                        self.tuning = True
                        self.tune_phase = "power"
                        self.tune_issued = None
                        self.tune_deadline = time.monotonic() + 25
                        self.sequence.status = "Preparing automatic tuner"
                        continue
                    self.clock_ok, self.clock_status = clock_health()
                    if not self.clock_ok:
                        raise ValueError(self.clock_status)
                    profile = self.receiver.radio_state.get("profile", {})
                    if not profile.get("transmit"):
                        raise ValueError("This radio cannot transmit")
                    if not self.receiver.busy() or self.receiver.frequency != dial:
                        raise ValueError("Wait for the radio connection")
                    self.sequence.settings = load_settings()
                    if action in ("auto_cq", "pota_hunt"):
                        self.journal = SessionLog()
                        self.auto.start(
                            dial, mode="pota" if action == "pota_hunt" else "auto"
                        )
                        self.auto.record(
                            "radio_ready",
                            model=profile.get("model"),
                            clock=self.clock_status,
                        )
                    elif action == "cq":
                        self.sequence.cq(dial)
                    elif action == "reply":
                        from ft8qso import call_fields

                        if self.previously_worked(call_fields(value)[0], dial):
                            raise ValueError("Already worked FT8 on this band")
                        self.sequence.reply(value, dial, self.decoder.snapshot()[0])
                    self.receiver.command(
                        "ft8_power", int(self.sequence.settings["tx_power_w"])
                    )
                    self.receiver.command("ft8_prepare")
                    prepared_at = time.monotonic()
                    setup_confirmed = False
            except queue.Empty:
                pass
            except (ValueError, OSError) as error:
                self.sequence.stop(str(error))
                self.status = str(error)
            if version != self.version:
                continue
            if (
                not self.receiver.busy()
                or self.receiver.stop.is_set()
                or (self.sequence.enabled and self.receiver.frequency is None)
            ):
                self.receiver.stop_tx()
                self.sequence.stop("Radio disconnected")
            summary = self.receiver.radio_state.get("power_summary")
            if summary and summary.get("at", 0) > power_summary_at:
                power_summary_at = summary["at"]
                record_power_summary(self.receiver, summary)
            fault = self.receiver.radio_state.get("tx_fault")
            if fault and fault != logged_fault:
                record_fault(self.receiver, fault)
            logged_fault = fault
            if clearing_at is not None:
                if not fault:
                    self.sequence.status = "Warning cleared"
                    clearing_at = None
                elif time.monotonic() - clearing_at > 5:
                    self.sequence.status = "Cannot clear: waiting for radio RX"
                    clearing_at = None
            if fault:
                self.receiver.stop_tx()
                self.sequence.stop(
                    "Waiting for confirmed RX" if clearing_at is not None else fault
                )
                self.tuning = False
            if self.tuning:
                now = time.monotonic()
                if (
                    self.tune_phase == "power"
                    and self.receiver.radio_state.get("power_ready")
                    and not self.receiver.radio_state.get("ptt")
                    and not self.receiver.radio_state.get("ptt_pending")
                ):
                    self.receiver.command("tuner")
                    self.tune_issued = now
                    self.tune_phase = "running"
                    self.sequence.status = "Auto-tuning antenna"
                elif self.tune_phase == "running":
                    tuner = self.receiver.radio_state.get("tuner")
                    started = self.receiver.radio_state.get("tune_started", 0)
                    fresh = (
                        started is not None
                        and started >= self.tune_issued
                        and now - started > 0.8
                        and self.receiver.radio_state.get("tuner_at", 0) > started
                    )
                    if (
                        fresh
                        and tuner == 1
                        and not self.receiver.radio_state.get("ptt")
                    ):
                        self.tuning = False
                        self.sequence.status = "Antenna tuner ready"
                    elif (
                        fresh
                        and tuner == 0
                        and self.receiver.radio_state.get("tune_seen_active")
                        and not self.receiver.radio_state.get("ptt")
                    ):
                        self.receiver.command("tx_fault", "Automatic tuner failed")
                        self.tuning = False
                        self.sequence.status = "Automatic tuner failed"
                    elif not started and now - self.tune_issued > 5:
                        self.receiver.command("tuner_stop")
                        self.receiver.command("tx_fault", "Tuner start not confirmed")
                        self.tuning = False
                if self.tuning and now > self.tune_deadline:
                    self.receiver.command("tuner_stop")
                    self.receiver.command("tx_fault", "Automatic tuner timed out")
                    self.tuning = False
                    self.sequence.stop("Automatic tuner timed out")
            self.settle_auto()
            if (
                self.sequence.enabled
                and not self.auto.active
                and time.time() - self.sequence.start > 180
            ):
                self.receiver.stop_tx()
                self.sequence.stop("Contact timed out")
            if self.sequence.enabled:
                try:
                    for message in self.decoder.snapshot()[0]:
                        (
                            self.auto.receive
                            if self.auto.active
                            else self.sequence.receive
                        )(message)
                except Exception as error:
                    self.receiver.stop_tx()
                    self.sequence.stop("Contact/log error: " + type(error).__name__)
                if job is not None and job.done.is_set():
                    try:
                        if self.auto.active:
                            self.auto.transmitted(pending, job.success, job.error)
                        else:
                            self.sequence.transmitted(pending, job.success)
                    except OSError:
                        self.receiver.stop_tx()
                    if not job.success:
                        self.sequence.status = job.error or "Transmit stopped"
                        record_fault(self.receiver, self.sequence.status, job)
                    job = pending = None
                if job is None:
                    self.sequence.prepare_ack()
                if self.auto.active and job is None:
                    try:
                        self.auto.prepare(decoded_slot=self.decoder.last_decoded_slot)
                    except OSError:
                        self.receiver.stop_tx()
                # This deadline measures a continuous loss of readiness, not
                # the age of the CQ session. Brief CI-V recovery must not trip
                # an already-expired startup timer after successful frames.
                if self.receiver.radio_state.get("tx_ready"):
                    prepared_at = time.monotonic()
                    setup_confirmed = True
                # Encode during RX, not after entering the small key-up window.
                if self.sequence.enabled and self.sequence.stage != "monitor":
                    key = (
                        self.sequence.message,
                        self.sequence.hz,
                        self.audio_rate,
                        int(self.sequence.settings.get("tx_level", "50")),
                    )
                    if key not in cache:
                        try:
                            cache[key] = waveform(*key)
                            if len(cache) > 12:
                                # Auto CQ may resume from next() at the slot
                                # boundary. Keep its prebuilt CQ across partners.
                                cache = {
                                    k: pcm
                                    for k, pcm in cache.items()
                                    if k == key or k[0].startswith("CQ ")
                                }
                        except Exception as error:
                            self.receiver.stop_tx()
                            self.sequence.stop("Encoder error: " + type(error).__name__)
                if (
                    self.sequence.enabled
                    and self.receiver.radio_state.get("tx_ready")
                    and job is None
                ):
                    previous_decoded = self.decoder.last_decoded_slot
                    phase_ready = (
                        previous_decoded == int(time.time() // 15) - 1
                        or self.sequence.last_sent_slot is None
                    )
                    try:
                        pending = (
                            (
                                self.auto.next
                                if self.auto.active
                                else self.sequence.next
                            )()
                            if phase_ready
                            else None
                        )
                    except OSError:
                        self.receiver.stop_tx()
                        pending = None
                    if pending:
                        try:
                            key = (
                                pending[2],
                                self.sequence.hz,
                                self.audio_rate,
                                int(self.sequence.settings.get("tx_level", "50")),
                            )
                            if version != self.version or self.closed.is_set():
                                continue
                            job = self.receiver.queue_tx(
                                cache[key],
                                pending[3] * 15 + 0.5,
                                self.sequence.dial,
                                message=pending[2],
                            )
                        except Exception as error:
                            self.receiver.stop_tx()
                            self.sequence.stop(
                                "Transmit error: " + type(error).__name__
                            )
                elif (
                    not self.receiver.radio_state.get("tx_ready")
                    and time.monotonic() - prepared_at > 8
                ):
                    reason = (
                        "Radio TX settings not confirmed"
                        if setup_confirmed
                        else "LAN modulation setup not confirmed"
                    )
                    self.receiver.stop_tx()
                    self.sequence.stop(reason)
                    record_fault(self.receiver, reason)
            if self.auto.active and mono >= audit_at:
                audit_at = mono + 1
                try:
                    state = self.receiver.radio_state
                    self.auto.record(
                        "station_status",
                        clock_ok=self.clock_ok,
                        frequency_hz=self.receiver.frequency,
                        status=self.sequence.status,
                        radio={
                            k: state.get(k)
                            for k in (
                                "ptt",
                                "ptt_pending",
                                "tx_ready",
                                "tx_fault",
                                "swr",
                                "alc",
                                "watts",
                                "tx_power",
                                "transport_gap_ms",
                                "transport_gap_at",
                            )
                        },
                        logging_status=self.logbook.status,
                    )
                except (OSError, AttributeError):
                    self.receiver.stop_tx()
                    self.sequence.stop("Auto CQ diagnostics failed; TX stopped")
            self.settle_auto()
            self.active = self.sequence.enabled
            self.status = self.sequence.status
        self.receiver.stop_tx()
        if self.auto.active:
            self.sequence.stop("Application closed")
        self.settle_auto()

    def close(self):
        self.action("stop")
        self.closed.set()
        self.thread.join(timeout=1)
        self.sounds.close()
