"""Non-blocking station controller; UI actions explicitly arm a contact/CQ."""

import queue
import re
import os
import subprocess
import threading
import time
from ft8qso import Sequencer, waveform
from ft8log import load_settings


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
        result = subprocess.run(
            ["timedatectl", "timesync-status"], capture_output=True, timeout=2, env=env
        )
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


class Station:
    def __init__(self, receiver, decoder, logbook):
        self.receiver = receiver
        self.decoder = decoder
        self.logbook = logbook
        self.commands = queue.Queue()
        self.closed = threading.Event()
        self.version = 0
        self.status = "Receive only"
        self.active = False
        self.clock_status = "Checking clock"
        self.clock_ok = False
        self.sequence = Sequencer(load_settings(), logbook.add)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def action(self, action, value=None, dial=7074000):
        self.version += 1
        self.receiver.stop_tx()
        self.commands.put((self.version, action, value, dial))
        if action == "stop":
            self.active = False
            self.status = "Transmit stopped"

    def run(self):
        version = 0
        job = None
        pending = None
        cache = {}
        prepared_at = 0
        clock_at = 0
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
            try:
                while True:
                    current, action, value, dial = self.commands.get_nowait()
                    if current != self.version:
                        continue
                    version = current
                    self.sequence.stop()
                    job = pending = None
                    if action == "stop":
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
                    if action == "cq":
                        self.sequence.cq(dial)
                    elif action == "reply":
                        self.sequence.reply(value, dial)
                    self.receiver.command("ft8_prepare")
                    prepared_at = time.monotonic()
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
            if self.sequence.enabled and time.time() - self.sequence.start > 180:
                self.receiver.stop_tx()
                self.sequence.stop("Contact timed out")
            if self.sequence.enabled:
                try:
                    for message in self.decoder.snapshot()[0]:
                        self.sequence.receive(message)
                except Exception as error:
                    self.receiver.stop_tx()
                    self.sequence.stop("Contact/log error: " + type(error).__name__)
                if job is not None and job.done.is_set():
                    self.sequence.transmitted(pending, job.success)
                    if not job.success:
                        self.sequence.status = job.error or "Transmit stopped"
                    job = pending = None
                if self.receiver.radio_state.get("tx_ready") and job is None:
                    previous_decoded = self.decoder.last_decoded_slot
                    phase_ready = (
                        previous_decoded == int(time.time() // 15) - 1
                        or self.sequence.last_sent_slot is None
                    )
                    pending = self.sequence.next() if phase_ready else None
                    if pending:
                        try:
                            key = (
                                pending[2],
                                self.sequence.hz,
                                48000,
                                int(self.sequence.settings.get("tx_level", "50")),
                            )
                            if key not in cache:
                                cache[key] = waveform(*key)
                                if len(cache) > 12:
                                    cache = {key: cache[key]}
                            if version != self.version or self.closed.is_set():
                                continue
                            job = self.receiver.queue_tx(
                                cache[key], pending[3] * 15 + 0.5, self.sequence.dial
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
                    self.sequence.stop("LAN modulation setup not confirmed")
            self.active = self.sequence.enabled
            self.status = self.sequence.status
        self.receiver.stop_tx()

    def close(self):
        self.action("stop")
        self.closed.set()
        self.thread.join(timeout=1)
