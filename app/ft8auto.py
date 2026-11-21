"""Explicitly armed Auto CQ sessions and private, reviewable JSONL journals."""

from collections import OrderedDict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import uuid

from ft8log import directory


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
    """Four successful unanswered sends per stage, with a full RX chance last."""

    def __init__(self, sequence, emit=lambda *_, **__: None):
        self.sequence = sequence
        self.emit = emit
        self.active = False
        self.cq_count = self.contact_count = 0
        self.seen = OrderedDict()
        self.stop_reason = ""
        self.finished_no_response = False

    def record(self, event, **fields):
        s = self.sequence
        try:
            self.emit(
                event,
                dial_hz=s.dial,
                tx_audio_hz=s.hz,
                stage=s.stage,
                partner=s.partner,
                tx_parity=s.parity,
                cq_unanswered=self.cq_count,
                contact_unanswered=self.contact_count,
                **fields,
            )
        except Exception as error:
            self.active = False
            self.sequence.repeat_limit = 5
            self.sequence.timeout_seconds = 180
            self.sequence.stop("Auto CQ log failed; TX stopped")
            raise OSError("Auto CQ log failed; TX stopped") from error

    def start(self, dial):
        self.cq_count = self.contact_count = 0
        self.seen.clear()
        self.started_slot = int(time.time() // 15)
        self.stop_reason = ""
        self.finished_no_response = False
        self.sequence.cq(dial)
        self.sequence.repeat_limit = self.sequence.timeout_seconds = None
        self.active = True
        self.record(
            "session_start",
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

    def _restart_cq(self, reason):
        self.record("cq_resume", reason=reason, completed_partner=self.sequence.partner)
        dial = self.sequence.dial
        self.sequence.cq(dial)
        self.sequence.repeat_limit = self.sequence.timeout_seconds = None
        self.cq_count = self.contact_count = 0

    def synchronize(self):
        if self.active and not self.sequence.enabled:
            reason = self.sequence.status
            if reason.startswith(("Logged ", "Already logged ")):
                self.record("qso_complete", result=reason)
                self._restart_cq("qso_complete")
            elif reason == "Incomplete contact; not logged":
                self.record("contact_abandoned", reason="missing_exchanged_reports")
                self._restart_cq("incomplete_contact")
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
        s.receive(message)
        after = (s.stage, s.partner, s.received, s.grid, s.last_rx)
        progressed = before[0] != after[0] or (before[1] != after[1] and bool(after[1]))
        if progressed:
            self.contact_count = 0
            self.cq_count = 0
        if progressed:
            decision = "progress"
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

    def next(self, now=None):
        if not self.active:
            return None
        now = time.time() if now is None else now
        slot = int(now // 15)
        s = self.sequence
        # Exhaustion is considered only at our next TX window, after the
        # previous receive period has had its decode opportunity.
        if slot % 2 != s.parity or not 0 <= now - slot * 15 <= 0.2:
            return None
        if s.pending or (s.last_slot is not None and slot <= s.last_slot):
            return None
        if s.stage == "cq" and self.cq_count >= 4:
            self.stop("Auto CQ stopped: 4 unanswered CQs", no_response=True)
            return None
        if s.stage != "cq" and self.contact_count >= 4:
            self.record("contact_abandoned", reason="4_unanswered_attempts")
            self._restart_cq("contact_no_response")
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
        # A late decoded reply can advance the stage while a previously
        # queued frame is in flight. Do not charge the new stage for that frame.
        if pending[1] == "cq" and self.sequence.stage == "cq":
            self.cq_count += 1
        elif pending[1] == self.sequence.stage:
            self.contact_count += 1
        self.sequence.transmitted(pending, True)
        self.record("attempt_count", transmitted_stage=pending[1])
        self.synchronize()
