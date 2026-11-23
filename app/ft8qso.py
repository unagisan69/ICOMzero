"""One explicitly started FT8 contact at a time; no autonomous hunting."""

import hashlib
import re
import subprocess
import time
import numpy as np
from ft8 import decoder_path, BANDS
from ft8log import validate_station, DEFAULTS


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


def tx_frequency(settings, message=None):
    value = str(settings.get("tx_hz", "Auto")).strip()
    hz = (
        (1500 if message is None else round(message.hz))
        if value.lower() == "auto"
        else int(value)
    )
    if not 200 <= hz <= 2800:
        raise ValueError("TX audio must be 200–2800 Hz")
    return hz


def latest_reply(message, messages, own_call):
    partner, _ = call_fields(message)
    candidates = [
        m
        for m in messages
        if not m.local_tx
        and m.slot >= message.slot
        and call_fields(m)[0] == partner
        and (m.text.startswith("CQ ") or m.text.startswith(own_call + " "))
    ]
    return max(candidates, key=lambda m: m.slot, default=message)


def contact_draft(settings, messages, selected, dial):
    """Recover actual exchanged reports; never invent a missing report."""
    if selected.local_tx:
        parts = selected.text.split()
        partner = (
            parts[0] if len(parts) == 3 and parts[1] == settings["callsign"] else ""
        )
    else:
        partner, _ = call_fields(selected)
    if (
        not re.fullmatch(r"[A-Z0-9/]*[0-9][A-Z0-9/]+", partner)
        or partner == settings["callsign"]
    ):
        raise ValueError("Select a station's CQ or conversation")
    own = settings["callsign"]
    messages = list(messages)
    if selected not in messages:
        messages.append(selected)
    cqs = [
        m.slot
        for m in messages
        if (
            not m.local_tx and m.text.startswith("CQ ") and call_fields(m)[0] == partner
        )
        or (m.local_tx and m.text.startswith("CQ " + own + " "))
    ]
    lower = max([selected.slot - 12] + [slot for slot in cqs if slot <= selected.slot])
    upper = min(
        [selected.slot + 12] + [slot - 1 for slot in cqs if slot > selected.slot]
    )
    rows = sorted(
        (m for m in messages if lower <= m.slot <= upper), key=lambda m: m.slot
    )
    related = []
    sent = received = None
    grid = ""
    for m in rows:
        parts = m.text.replace("<", "").replace(">", "").split()
        if m.text.startswith("CQ ") and call_fields(m)[0] == partner:
            extra = call_fields(m)[1]
            if re.fullmatch(r"[A-R]{2}[0-9]{2}", extra):
                grid = extra
        if len(parts) != 3:
            continue
        outgoing = m.local_tx and parts[:2] == [partner, own]
        incoming = not m.local_tx and parts[:2] == [own, partner]
        if not (outgoing or incoming):
            continue
        related.append(m)
        extra = parts[2]
        if re.fullmatch(r"R?[+-][0-9]{2}", extra):
            report = int(extra.lstrip("R"))
            if outgoing:
                sent = report
            else:
                received = report
        if (
            incoming
            and extra not in ("RRR", "RR73", "R73", "73")
            and re.fullmatch(r"[A-R]{2}[0-9]{2}", extra)
        ):
            grid = extra
    slots = [m.slot for m in related] or [selected.slot]
    return dict(
        call=partner,
        own_call=own,
        own_grid=settings["grid"],
        grid=grid,
        dial=dial,
        band=next((n for n, f in BANDS if f == dial), ""),
        start=min(slots) * 15 + 0.5,
        end=max(slots) * 15 + 13.14,
        sent=sent,
        received=received,
    )


def finalize_contact(draft):
    q = dict(draft)
    validate_station({**DEFAULTS, "callsign": q["own_call"], "grid": q["own_grid"]})
    q["call"] = str(q["call"]).strip().upper()
    q["grid"] = str(q.get("grid", "")).strip().upper()
    if (
        not re.fullmatch(r"[A-Z0-9/]*[0-9][A-Z0-9/]+", q["call"])
        or q["call"] == q["own_call"]
    ):
        raise ValueError("Enter the contacted station's callsign")
    if q["grid"] and not re.fullmatch(r"[A-R]{2}[0-9]{2}(?:[A-X]{2})?", q["grid"]):
        raise ValueError("Enter a valid grid or leave it empty")
    for name in ("sent", "received"):
        try:
            q[name] = int(q[name])
        except (TypeError, ValueError):
            raise ValueError("Enter both exchanged reports") from None
        if not -50 <= q[name] <= 49:
            raise ValueError("Reports must be -50 to +49 dB")
    identity = f"{q['own_call']}|{q['call']}|{q['dial']}|{int(q['start'])}"
    q["id"] = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return q


class Sequencer:
    def __init__(self, settings, complete):
        self.settings = dict(settings)
        self.complete = complete
        self.enabled = False
        self.repeat_limit = 5
        self.timeout_seconds = 180
        self.partner = ""
        self.grid = ""
        self.sent = None
        self.received = None
        self.message = ""
        self.stage = "idle"
        self.parity = 0
        self.hz = tx_frequency(settings)
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
        self.allow_unprompted_report = False
        self.last_sent_slot = None
        self.last_sent_stage = None
        self.qso_start = None
        self.completed_contacts = {}
        self.ack_queue = {}
        self.ack_resume = None

    def stop(self, reason="Transmit stopped"):
        self.enabled = False
        self.pending = None
        self.ack_resume = None
        self.ack_queue.clear()
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
        self.allow_unprompted_report = False
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
        self.hz = tx_frequency(self.settings)
        self.message = f"CQ {self.settings['callsign']} {self.settings['grid'][:4]}"
        self.status = "Next: " + self.message

    def reply(self, message, dial, history=()):
        if message.local_tx:
            raise ValueError("Select a received decode, not a local TX row")
        message = latest_reply(message, history, self.settings["callsign"])
        age = time.time() - message.slot * 15
        if not -2 <= age <= 120:
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
        hz = tx_frequency(self.settings, message)
        draft = contact_draft(self.settings, history, message, dial)
        continuing = (
            self.partner == partner
            and not message.text.startswith("CQ ")
            and self.dial == dial
            and time.time() - self.start <= 180
        )
        old = dict(
            sent=self.sent,
            received=self.received,
            grid=self.grid,
            qso_start=self.qso_start,
            start=self.start,
            last_slot=self.last_slot,
        )
        self.begin(dial)
        self.partner = partner
        self.grid = (old["grid"] if continuing else "") or draft["grid"]
        self.sent = (
            old["sent"] if continuing and old["sent"] is not None else draft["sent"]
        )
        self.received = (
            old["received"]
            if continuing and old["received"] is not None
            else draft["received"]
        )
        self.parity = 1 - message.slot % 2
        self.hz = hz
        self.last_rx = message.slot
        if continuing:
            self.qso_start = old["qso_start"]
            self.start = old["start"]
            self.last_slot = old["last_slot"]
        if message.text.startswith("CQ "):
            self.received = None
            self.sent = max(-50, min(49, message.snr))
            self.set_message("grid", self.settings["grid"][:4])
        elif extra in ("RRR", "RR73", "R73", "73"):
            self.final_seen = True
            self.set_message("73", "73")
        elif re.fullmatch(r"[A-R]{2}[0-9]{2}", extra):
            self.grid = extra
            self.sent = max(-50, min(49, message.snr))
            self.set_message("report", f"{self.sent:+03d}")
        elif re.fullmatch(r"[+-][0-9]{2}", extra):
            self.received = int(extra)
            self.sent = max(-50, min(49, message.snr))
            self.set_message("rreport", f"R{self.sent:+03d}")
        elif re.fullmatch(r"R[+-][0-9]{2}", extra):
            self.received = int(extra[1:])
            self.set_message("rrr", "RRR")
        else:
            self.stop("Unrecognized contact stage")
            raise ValueError("Select a CQ, grid, report or acknowledgment")

    def queue_ack(self, m):
        """A completed station may have missed our final 73; never log it again."""
        parts = m.text.split()
        now = time.time()
        if (m.local_tx or len(parts) != 3 or parts[0] != self.settings["callsign"]
                or parts[2] not in ("RRR", "RR73") or m.key in self.seen
                or not 0 <= now - m.slot * 15 <= 120):
            return False
        completed = self.completed_contacts.get(parts[1])
        if completed is None or completed[0] != self.dial or now - completed[1] > 600:
            return False
        self.seen.add(m.key)
        self.ack_queue[parts[1]] = m
        if len(self.ack_queue) > 8:
            del self.ack_queue[next(iter(self.ack_queue))]
        return True

    def prepare_ack(self):
        if not self.enabled or self.pending or self.ack_resume is not None:
            return False
        while self.ack_queue:
            call = next(iter(self.ack_queue))
            message = self.ack_queue.pop(call)
            if not 0 <= time.time() - message.slot * 15 <= 120:
                continue
            self.ack_resume = self.__dict__.copy()
            self.partner = call
            self.message = f"{call} {self.settings['callsign']} 73"
            self.stage = "ack73"
            self.parity = 1 - message.slot % 2
            self.hz = tx_frequency(self.settings, message)
            self.repeats = 0
            self.status = "Next: " + self.message
            return True
        return False

    def receive(self, m):
        if self.enabled and self.queue_ack(m):
            return
        if self.ack_resume is not None:
            return
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
            if extra in ("RRR", "RR73", "R73", "73") or not (
                re.fullmatch(r"[A-R]{2}[0-9]{2}", extra)
                or re.fullmatch(r"[+-][0-9]{2}", extra)
            ):
                return
            self.partner = partner
            self.parity = 1 - m.slot % 2
            self.hz = tx_frequency(self.settings, m)
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
        if re.fullmatch("[+-][0-9]{2}", extra) and (
            self.stage in ("grid", "report")
            or (self.stage == "rreport" and self.allow_unprompted_report)
        ):
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
            if self.allow_unprompted_report and self.received is None:
                self.status = "Waiting for actual signal report"
                return
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
        if (
            self.timeout_seconds is not None and now - self.start > self.timeout_seconds
        ) or (self.repeat_limit is not None and self.repeats >= self.repeat_limit):
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
        if pending[1] == "ack73" and self.ack_resume is not None:
            resume = self.ack_resume
            self.__dict__.update(resume)
            self.ack_resume = None
            self.last_slot = max(self.last_slot or 0, pending[3])
            return
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
        added = self.complete(qso)
        self.completed_contacts[self.partner] = (self.dial, end)
        if len(self.completed_contacts) > 64:
            del self.completed_contacts[next(iter(self.completed_contacts))]
        self.stop(("Already logged " if added is False else "Logged ") + self.partner)
