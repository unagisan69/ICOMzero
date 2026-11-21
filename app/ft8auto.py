"""Explicitly armed Auto CQ sessions and private, reviewable JSONL journals."""

from collections import OrderedDict
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import time
import uuid

from ft8log import directory
from ft8qso import call_fields
from countries import country, activation_cq


class SessionLog:
    def __init__(self, root=None, rotate_bytes=8 * 1024 * 1024):
        self.root = Path(root) if root is not None else directory() / "auto-cq"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.session = (
            datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + uuid.uuid4().hex[:8]
        )
        self.rotate_bytes = rotate_bytes
        self.part = self.seq = 0
        self.started_mono = time.monotonic()
        self.stream = None
        self.dirty = False
        self._open()

    def _open(self):
        self.part += 1
        suffix = "" if self.part == 1 else f"_part{self.part:03d}"
        self.path = self.root / f"{self.session}{suffix}.jsonl"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.stream = os.fdopen(fd, "w", encoding="utf-8", buffering=65536)
        self.bytes = 0

    def emit(self, event, **fields):
        if self.stream is None:
            raise OSError("Auto CQ log is closed")
        self.seq += 1
        data = {
            "schema": 1,
            "session": self.session,
            "seq": self.seq,
            "utc": datetime.fromtimestamp(time.time(), timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "elapsed_s": round(time.monotonic() - self.started_mono, 6),
            "event": event,
            **fields,
        }
        line = json.dumps(data, ensure_ascii=False) + "\n"
        if self.bytes and self.bytes + len(line.encode()) > self.rotate_bytes:
            self.flush(durable=True)
            self.stream.close()
            self._open()
        self.stream.write(line)
        self.bytes += len(line.encode())
        self.dirty = True
        if event in (
            "session_start",
            "tx_scheduled",
            "tx_end",
            "session_stop",
            "qso_saved",
        ):
            self.flush(durable=event != "tx_scheduled")

    def flush(self, durable=False):
        if self.stream is not None and self.dirty:
            self.stream.flush()
            if durable:
                os.fsync(self.stream.fileno())
            self.dirty = False

    def close(self):
        if self.stream is not None:
            try:
                self.flush(durable=True)
            finally:
                self.stream.close()
                self.stream = None


class AutoCQ:
    """Explicit Auto CQ rounds, queued callers, and fresh CQ fallback targets."""

    def __init__(self, sequence, emit=lambda *_, **__: None):
        self.sequence = sequence
        self.emit = emit
        self.active = False
        self.mode = "auto"
        self.own_cq_limit = 1
        self.target_limit = 5
        self.rest_until = 0
        self.pota_cooldown = {}
        self.cq_count = self.contact_count = 0
        self.seen = OrderedDict()
        self.queue = OrderedDict()
        self.cqs = OrderedDict()
        self.completed = set()
        self.attempted = set()
        self.fallback_used = []
        self.hunt_waiting = False
        self.stop_reason = ""
        self.finished_no_response = False

    def record(self, event, **fields):
        s = self.sequence
        try:
            self.emit(
                event,
                mode=self.mode,
                dial_hz=s.dial,
                tx_audio_hz=s.hz,
                stage=s.stage,
                partner=s.partner,
                tx_parity=s.parity,
                cq_unanswered=self.cq_count,
                contact_unanswered=self.contact_count,
                queued_calls=list(self.queue),
                fallback_calls=list(self.fallback_used),
                **fields,
            )
        except Exception as error:
            self.active = False
            self.sequence.repeat_limit = 5
            self.sequence.timeout_seconds = 180
            self.sequence.stop("Auto CQ log failed; TX stopped")
            raise OSError("Auto CQ log failed; TX stopped") from error

    def start(self, dial, mode="auto"):
        self.mode = mode
        self.pota_cooldown.clear()
        self.rest_until = 0
        self.cq_count = self.contact_count = 0
        self.seen.clear()
        self.queue.clear()
        self.cqs.clear()
        self.completed.clear()
        self.attempted.clear()
        self.fallback_used.clear()
        self.hunt_waiting = False
        self.started_slot = int(time.time() // 15)
        self.stop_reason = ""
        self.finished_no_response = False
        self.sequence.cq(dial)
        self.sequence.repeat_limit = self.sequence.timeout_seconds = None
        self.active = True
        if mode == "pota":
            self._monitor("POTA Hunt monitoring", rest=False)
        self.record(
            "session_start",
            policy={
                "own_cqs": 0 if mode == "pota" else self.own_cq_limit,
                "cq_targets": "continuous" if mode == "pota" else self.target_limit,
                "target_calls": 3 if mode == "pota" else 2,
                "contact_stage_attempts": 3 if mode == "pota" else 4,
                "cq_max_age_s": 90,
                "queue_max_age_s": 120,
                "queue_limit": 16,
            },
            station={
                k: self.sequence.settings[k]
                for k in (
                    "callsign",
                    "grid",
                    "cq_period",
                    "tx_hz",
                    "tx_level",
                    "tx_power_w",
                )
            },
        )

    def stop(self, reason, no_response=False):
        was_active = self.active
        self.active = False
        self.stop_reason = reason
        self.finished_no_response = no_response
        self.sequence.stop(reason)
        self.sequence.repeat_limit = 5
        self.sequence.timeout_seconds = 180
        if was_active:
            self.record("session_stop", reason=reason, no_responses=no_response)

    @staticmethod
    def valid_call(call):
        return bool(re.fullmatch(r"[A-Z0-9/]*[0-9][A-Z0-9/]+", call))

    @staticmethod
    def automatic_call(call):
        # The current receiver rejects hashed calls. Only select calls that
        # round-trip through standard FT8 messages, including /P and /R.
        return bool(
            re.fullmatch(
                r"(?:[A-Z0-9]{1,2}[0-9][A-Z]{1,3}|3DA0[A-Z]{1,3}|3X[A-Z][0-9][A-Z]{1,3})(?:/[PR])?",
                call,
            )
        )

    def _prune(self, now):
        for call, message in list(self.queue.items()):
            if now - message.slot * 15 > 120 or call in self.completed:
                del self.queue[call]
                self.record("queue_expired", call=call, rx_slot=message.slot)
        for call, message in list(self.cqs.items()):
            if now - message.slot * 15 > 90 or call in self.completed:
                del self.cqs[call]

    def _monitor(self, reason, rest=True):
        now = time.time()
        call = self.sequence.partner
        if rest and call:
            self.pota_cooldown[call] = now + 60
        self.sequence.begin(self.sequence.dial)
        self.sequence.repeat_limit = self.sequence.timeout_seconds = None
        self.sequence.stage = "monitor"
        self.sequence.message = ""
        self.sequence.status = "POTA Hunt resting" if rest else "POTA Hunt monitoring"
        self.rest_until = now + 15 if rest else now
        self.contact_count = self.cq_count = 0
        self.hunt_waiting = False
        if rest:
            self.record(
                "pota_rest",
                reason=reason,
                previous_partner=call,
                rest_seconds=15,
                station_cooldown_seconds=60,
            )

    def _pota_next(self, now):
        self._prune(now)
        if now < self.rest_until:
            return False
        self.sequence.status = "POTA Hunt monitoring"
        candidates = sorted(
            (
                m
                for call, m in self.cqs.items()
                if activation_cq(m)
                and call not in self.completed
                and now >= self.pota_cooldown.get(call, 0)
            ),
            key=lambda m: (m.slot, m.snr),
            reverse=True,
        )
        for message in candidates:
            if self._target(message, "pota"):
                return True
        return False

    def _restart_cq(self, reason):
        self.record("cq_resume", reason=reason, completed_partner=self.sequence.partner)
        self.sequence.cq(self.sequence.dial)
        self.sequence.repeat_limit = self.sequence.timeout_seconds = None
        self.cq_count = self.contact_count = 0
        self.hunt_waiting = False
        self.fallback_used.clear()
        self.attempted = set(self.completed)

    def _target(self, message, source):
        call, _ = call_fields(message)
        if not self.automatic_call(call):
            self.record(
                "target_rejected",
                call=call,
                reason="unsupported_hashed_call",
                source=source,
            )
            return False
        try:
            self.sequence.reply(message, self.sequence.dial)
        except ValueError as error:
            self.record("target_rejected", call=call, reason=str(error), source=source)
            return False
        self.sequence.repeat_limit = self.sequence.timeout_seconds = None
        if source == "pota":
            self.sequence.allow_unprompted_report = True
            self.sequence.received = None
            self.sequence.set_message("rreport", f"R{self.sequence.sent:+03d}")
        self.contact_count = self.cq_count = 0
        self.hunt_waiting = source == "cq"
        self.attempted.add(call)
        self.queue.pop(call, None)
        self.record(
            "target_selected",
            source=source,
            call=call,
            text=message.text,
            rx_slot=message.slot,
            rx_audio_hz=message.hz,
            snr_db=message.snr,
        )
        return True

    def _queued(self, now):
        if self.mode == "pota":
            return False
        self._prune(now)
        while self.queue:
            call, message = self.queue.popitem(last=False)
            if self._target(message, "queued"):
                return True
        return False

    def _hunt_next(self, now):
        if self._queued(now):
            return
        self._prune(now)
        candidates = sorted(
            (
                m
                for c, m in self.cqs.items()
                if c not in self.attempted and c not in self.completed
            ),
            key=lambda m: (m.snr, m.slot),
            reverse=True,
        )
        for message in candidates:
            if len(self.fallback_used) >= self.target_limit:
                break
            call, _ = call_fields(message)
            if self._target(message, "cq"):
                self.fallback_used.append(call)
                self.record("fallback_attempt", call=call, ranking="snr_then_recency")
                return
        self.stop("Auto CQ ended: no replies / fresh CQ targets", no_response=True)

    def _after_contact(self, reason, now):
        if self.mode == "pota":
            self._monitor(reason)
            return
        self.hunt_waiting = False
        if not self._queued(now):
            self._restart_cq(reason)

    def synchronize(self):
        if self.active and not self.sequence.enabled:
            reason = self.sequence.status
            if reason.startswith(("Logged ", "Already logged ")):
                self.completed.add(self.sequence.partner)
                self.record("qso_complete", result=reason)
                self._after_contact("qso_complete", time.time())
            elif reason == "Incomplete contact; not logged":
                self.record("contact_abandoned", reason="missing_exchanged_reports")
                self._after_contact("incomplete_contact", time.time())
            else:
                self.stop(reason)

    def receive(self, message):
        if not self.active or message.local_tx or message.key in self.seen:
            return
        self.seen[message.key] = None
        if len(self.seen) > 4096:
            self.seen.popitem(last=False)
        if message.slot < self.started_slot:
            return
        s = self.sequence
        before = (s.stage, s.partner, s.received, s.grid, s.last_rx)
        own = s.settings["callsign"]
        parts = message.text.replace("<", "").replace(">", "").split()
        call, extra = call_fields(message)
        valid = (
            self.valid_call(call)
            and call != own
            and "<" not in message.text
            and 200 <= message.hz <= 2800
        )
        eligible = valid and self.automatic_call(call)
        if eligible and message.text.startswith("CQ ") and call not in self.completed:
            previous = self.cqs.get(call)
            if previous is None or (message.slot, message.snr) > (
                previous.slot,
                previous.snr,
            ):
                self.cqs[call] = message
                self.cqs.move_to_end(call)
                if len(self.cqs) > 256:
                    self.cqs.popitem(last=False)
        elif valid:
            # A later conversation means this station is no longer calling CQ.
            previous = self.cqs.get(call)
            if previous is not None and message.slot >= previous.slot:
                self.cqs.pop(call, None)
        queued = False
        if (
            eligible
            and self.mode != "pota"
            and len(parts) == 3
            and parts[0] == own
            and call != s.partner
            and (s.partner or s.last_sent_slot is None)
            and call not in self.completed
            and 0 <= time.time() - message.slot * 15 <= 120
            and (
                re.fullmatch(r"[A-R]{2}[0-9]{2}", extra)
                or re.fullmatch(r"[+-][0-9]{2}", extra)
            )
        ):
            previous = self.queue.get(call)
            if previous is None or (message.slot, message.snr) > (
                previous.slot,
                previous.snr,
            ):
                self.queue[call] = message
                queued = True
                self.record(
                    "queue_updated" if previous else "caller_queued",
                    call=call,
                    text=message.text,
                    rx_slot=message.slot,
                    rx_audio_hz=message.hz,
                    snr_db=message.snr,
                )
                if len(self.queue) > 16:
                    expired, _ = self.queue.popitem(last=False)
                    self.record("queue_dropped", call=expired, reason="capacity")
        already_worked = (
            valid and call in self.completed and len(parts) == 3 and parts[0] == own
        )
        unsupported = valid and not eligible and len(parts) == 3 and parts[0] == own
        if not already_worked and not unsupported:
            s.receive(message)
        after = (s.stage, s.partner, s.received, s.grid, s.last_rx)
        progressed = before[0] != after[0] or (before[1] != after[1] and bool(after[1]))
        if progressed:
            self.contact_count = self.cq_count = 0
            self.hunt_waiting = False
            self.queue.pop(s.partner, None)
        if progressed:
            decision = "progress"
        elif unsupported:
            decision = "unsupported_hashed_call"
        elif already_worked:
            decision = "already_worked_this_session"
        elif queued:
            decision = "queued_while_busy"
        elif len(parts) != 3 or parts[0] != own:
            decision = "not_addressed_to_us"
        elif s.partner and parts[1] != s.partner:
            decision = "other_station_while_busy"
        elif message.slot % 2 == s.parity:
            decision = "same_transmit_period"
        elif before == after:
            decision = "stale_or_no_progress"
        else:
            decision = "response_without_stage_progress"
        self.record(
            "rx_decode",
            text=message.text,
            rx_slot=message.slot,
            rx_slot_utc=datetime.fromtimestamp(
                message.slot * 15, timezone.utc
            ).isoformat(),
            rx_audio_hz=message.hz,
            snr_db=message.snr,
            rx_country=country(call) if valid else "Unknown",
            auto_eligible=eligible,
            decision=decision,
            previous_stage=before[0],
        )
        if progressed:
            self.record(
                "contact_progress",
                previous_stage=before[0],
                received_report=s.received,
                sent_report=s.sent,
                last_rx_slot=s.last_rx,
            )
        self.synchronize()

    def prepare(self, now=None, decoded_slot=None):
        """Change targets after a receive opportunity, before waveform encoding."""
        if not self.active or not self.sequence.enabled:
            return False
        now = time.time() if now is None else now
        s = self.sequence
        if s.pending:
            return False
        if self.mode == "pota" and s.stage == "monitor":
            return self._pota_next(now)
        if s.stage == "cq" and s.last_sent_slot is None and self.queue:
            return self._queued(now)
        if s.last_sent_slot is None:
            return False
        ready = now >= (s.last_sent_slot + 2) * 15 or (
            decoded_slot is not None and decoded_slot >= s.last_sent_slot + 1
        )
        if not ready:
            return False
        if s.stage == "cq" and self.cq_count >= self.own_cq_limit:
            self.record("own_cq_exhausted")
            self._hunt_next(now)
            return True
        limit = 3 if self.mode == "pota" else (2 if self.hunt_waiting else 4)
        if s.stage != "cq" and self.contact_count >= limit:
            hunting = self.hunt_waiting
            self.record(
                "contact_abandoned",
                reason=(
                    "3_unanswered_attempts"
                    if self.mode == "pota"
                    else (
                        "2_unanswered_target_calls"
                        if hunting
                        else "4_unanswered_attempts"
                    )
                ),
            )
            if self.mode == "pota":
                self._monitor("3_unanswered_attempts")
            elif hunting:
                self._hunt_next(now)
            else:
                self._after_contact("contact_no_response", now)
            return True
        return False

    def next(self, now=None):
        if not self.active:
            return None
        now = time.time() if now is None else now
        # If no receive callback prepared a transition, let the controller
        # encode the new target on its next iteration before queuing audio.
        if self.prepare(now):
            return None
        if self.sequence.stage == "monitor":
            return None
        pending = self.sequence.next(now)
        if pending:
            self.record(
                "tx_scheduled",
                text=pending[2],
                tx_slot=pending[3],
                audio_start_utc=datetime.fromtimestamp(
                    pending[3] * 15 + 0.5, timezone.utc
                ).isoformat(),
            )
        return pending

    def transmitted(self, pending, success, error=""):
        if not self.active:
            return
        self.record(
            "tx_end", text=pending[2], tx_slot=pending[3], success=success, error=error
        )
        if not success:
            self.sequence.transmitted(pending, False)
            self.stop(error or "Auto CQ transmit failed")
            return
        if pending[1] == "cq" and self.sequence.stage == "cq":
            self.cq_count += 1
        elif pending[1] == self.sequence.stage:
            self.contact_count += 1
        self.sequence.transmitted(pending, True)
        self.record("attempt_count", transmitted_stage=pending[1])
        self.synchronize()
