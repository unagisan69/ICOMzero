"""One explicitly started FT8 contact at a time; no autonomous hunting."""

import hashlib
import re
import subprocess
import time
import numpy as np
from ft8 import decoder_path, BANDS
from ft8log import validate_station


def waveform(message, hz, rate=48000, level=50):
    path = decoder_path().with_name("icomzero-ft8-encode")
    result = subprocess.run(
        [str(path), message, str(hz), str(rate)],
        capture_output=True,
        timeout=5,
        check=True,
    )
    if len(result.stdout) != int(rate * 12.64) * 2:
        raise ValueError("Invalid FT8 waveform")
    if not 10 <= int(level) <= 80:
        raise ValueError("TX level must be 10–80%")
    samples = np.frombuffer(result.stdout, dtype="<i2").astype(np.int32)
    return (samples * int(level) // 20).astype("<i2").tobytes()


def call_fields(message):
    parts = message.text.replace("<", "").replace(">", "").split()
    if parts and parts[0] == "CQ":
        # CQ DX / CQ region messages are displayed but only unambiguous standard calls answered.
        if len(parts) >= 3 and re.fullmatch("[A-R]{2}[0-9]{2}", parts[-1]):
            return parts[-2], parts[-1]
        return (parts[-1], "") if len(parts) >= 2 else ("", "")
    return (parts[1], parts[2]) if len(parts) == 3 else ("", "")


class Sequencer:
    def __init__(self, settings, complete):
        self.settings = dict(settings)
        self.complete = complete
        self.enabled = False
        self.partner = ""
        self.grid = ""
        self.sent = None
        self.received = None
        self.message = ""
        self.stage = "idle"
        self.parity = 0
        self.hz = int(settings["tx_hz"])
        self.start = 0
        self.last_slot = None
        self.last_rx = None
        self.repeats = 0
        self.pending = None
        self.seen = set()
        self.status = "Receive only"
        self.dial = 7074000
        self.serial = 0
        self.final_seen = False
        self.last_sent_slot = None
        self.last_sent_stage = None
        self.qso_start = None

    def stop(self, reason="Transmit stopped"):
        self.enabled = False
        self.pending = None
        self.serial += 1
        self.status = reason

    def begin(self, dial):
        validate_station(self.settings)
        self.stop()
        self.serial += 1
        self.enabled = True
        self.dial = dial
        self.partner = ""
        self.grid = ""
        self.sent = self.received = None
        self.last_slot = self.last_rx = None
        self.repeats = 0
        self.seen = set()
        self.start = time.time()
        self.final_seen = False
        self.last_sent_slot = None
        self.last_sent_stage = None
        self.qso_start = None

    def set_message(self, stage, extra):
        self.stage = stage
        self.repeats = 0
        self.message = f"{self.partner} {self.settings['callsign']} {extra}"
        self.status = "Next: " + self.message

    def cq(self, dial):
        self.begin(dial)
        self.parity = int(self.settings["cq_period"] == "Odd")
        self.stage = "cq"
        self.hz = int(self.settings["tx_hz"])
        self.message = f"CQ {self.settings['callsign']} {self.settings['grid'][:4]}"
        self.status = "Next: " + self.message

    def reply(self, message, dial):
        if message.local_tx:
            raise ValueError("Select a received decode, not a local TX row")
        if time.time() - message.slot * 15 > 120:
            raise ValueError("Choose a recent decode")
        partner, extra = call_fields(message)
        if (
            not re.fullmatch(r"[A-Z0-9/]*[0-9][A-Z0-9/]+", partner)
            or partner == self.settings["callsign"]
            or "<" in message.text
        ):
            raise ValueError("Select a standard CQ or addressed call")
        if not message.text.startswith("CQ ") and not message.text.startswith(
            self.settings["callsign"] + " "
        ):
            raise ValueError("Decode is not addressed to you")
        self.begin(dial)
        self.partner = partner
        self.grid = extra if re.fullmatch("[A-R]{2}[0-9]{2}", extra) else ""
        self.sent = max(-50, min(49, message.snr))
        self.parity = 1 - message.slot % 2
        self.hz = int(self.settings["tx_hz"])
        self.last_rx = message.slot
        self.set_message("grid", self.settings["grid"][:4])
        # A directed report can be answered immediately with an R-report.
        if re.fullmatch("[+-][0-9]{2}", extra):
            self.received = int(extra)
            self.set_message("rreport", f"R{self.sent:+03d}")

    def receive(self, m):
        if m.local_tx or not self.enabled or m.key in self.seen:
            return
        self.seen.add(m.key)
        if (
            m.slot * 15 < self.start
            or self.last_sent_slot is None
            or m.slot <= self.last_sent_slot
        ):
            return
        parts = m.text.replace("<", "").replace(">", "").split()
        if len(parts) != 3 or parts[0] != self.settings["callsign"] or "<" in m.text:
            return
        partner, extra = parts[1:]
        if self.stage == "cq" and re.fullmatch("[A-Z0-9/]+", partner):
            self.partner = partner
            self.parity = 1 - m.slot % 2
            self.hz = int(self.settings["tx_hz"])
            self.sent = max(-50, min(49, m.snr))
            self.last_rx = m.slot
            if re.fullmatch("[A-R]{2}[0-9]{2}", extra):
                self.grid = extra
                self.set_message("report", f"{self.sent:+03d}")
                return
            if re.fullmatch("[+-][0-9]{2}", extra):
                self.received = int(extra)
                self.set_message("rreport", f"R{self.sent:+03d}")
                return
            self.partner = ""
            return
        if partner != self.partner or m.slot % 2 == self.parity:
            return
        if self.last_rx is not None and m.slot <= self.last_rx:
            return
        self.last_rx = m.slot
        if re.fullmatch("[+-][0-9]{2}", extra) and self.stage in ("grid", "report"):
            self.received = int(extra)
            self.set_message("rreport", f"R{self.sent:+03d}")
        elif re.fullmatch("R[+-][0-9]{2}", extra) and self.stage in (
            "report",
            "grid",
            "rreport",
        ):
            self.received = int(extra[1:])
            self.set_message("rrr", "RRR")
        elif extra in ("RRR", "RR73") and self.stage in ("rreport", "rrr"):
            self.final_seen = True
            self.set_message("73", "73")
        elif (
            extra == "73"
            and self.stage in ("rrr", "73")
            and self.sent is not None
            and self.received is not None
        ):
            self.final_seen = True
            if self.stage == "rrr" and self.last_sent_stage == "rrr":
                self.finish()

    def next(self, now=None):
        now = time.time() if now is None else now
        if not self.enabled or self.pending:
            return None
        if now - self.start > 180 or self.repeats >= 5:
            self.stop("Contact timed out")
            return None
        slot = int(now // 15)
        # Prebuild before the chosen period; never start late into an existing period.
        target = slot
        if target % 2 != self.parity or not 0 <= now - slot * 15 <= 0.2:
            return None
        if self.last_slot is not None and target <= self.last_slot:
            return None
        self.last_slot = target
        self.repeats += 1
        self.pending = (self.serial, self.stage, self.message, target)
        return self.pending

    def transmitted(self, pending, success):
        if not self.pending or pending != self.pending:
            return
        self.pending = None
        if not success:
            self.stop("Transmit failed; stopped")
            return
        if self.qso_start is None:
            self.qso_start = pending[3] * 15 + 0.5
        self.last_sent_slot = pending[3]
        self.last_sent_stage = pending[1]
        if pending[1] == "73" and self.final_seen:
            self.finish()
        else:
            self.status = "Waiting for " + (self.partner or "reply")

    def finish(self):
        if self.sent is None or self.received is None or not self.final_seen:
            self.stop("Incomplete contact; not logged")
            return
        end = time.time()
        identity = (
            f"{self.settings['callsign']}|{self.partner}|{self.dial}|{int(self.start)}"
        )
        qso = {
            "id": hashlib.sha256(identity.encode()).hexdigest()[:24],
            "call": self.partner,
            "own_call": self.settings["callsign"],
            "own_grid": self.settings["grid"],
            "grid": self.grid,
            "dial": self.dial,
            "band": next((n for n, f in BANDS if f == self.dial), ""),
            "start": self.qso_start or self.start,
            "end": end,
            "sent": self.sent,
            "received": self.received,
        }
        self.complete(qso)
        self.stop("Logged " + self.partner)
