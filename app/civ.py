"""Native Icom frequency, controls and spectrum transport."""

from collections import deque
import struct
import time

from models import DEFAULT_PROFILE, MIN_HZ, MAX_HZ

CONTROLLER = 0xe0
SCOPE_DESTINATION = 0xe1


def decode_frequency(data):
    if any((value & 15) > 9 or (value >> 4) > 9 for value in data):
        return None
    return int("".join(f"{value:02x}" for value in reversed(data)))


def encode_frequency(hz):
    digits = 12 if hz >= 10_000_000_000 else 10
    return bytes.fromhex(f"{hz:0{digits}d}")[::-1]


class Scope:
    def __init__(self, profile=DEFAULT_PROFILE):
        self.profile = profile
        self.total = 0
        self.edges = None
        self.next_part = 0
        self.bins = bytearray()

    def parse(self, data):
        if len(data) < 2:
            return None
        part, total = (decode_frequency(bytes((v,))) for v in data[:2])
        if part is None or total is None or not 1 <= part <= total <= 99:
            self.next_part = 0
            return None
        if part == 1:
            self.bins.clear()
            self.next_part = 0
            size = 6 if self.profile["model"] == "IC-905" and len(data) in (16, 491) else 5
            header_size = 4 + 2 * size
            if len(data) < header_size or data[header_size - 1]:
                return None
            start, end = decode_frequency(data[3:3 + size]), decode_frequency(data[3 + size:3 + 2 * size])
            if start is None or end is None:
                return None
            if data[2] == 0:  # Center mode reports the center and half-span.
                start, end = start - end, start + end
            if not 0 < start < end:
                return None
            self.edges = (start, end)
            self.total = total
            if total != 1:
                if len(data) == header_size:
                    self.next_part = 2
                return None
            self.bins.extend(data[header_size:])
        else:
            if total != self.total or part != self.next_part or not self.next_part:
                self.next_part = 0
                return None
            self.bins.extend(data[2:])
            self.next_part = 0 if part == total else part + 1
            if self.next_part:
                return None
        widths = {self.profile["scope_bins"]}
        if self.profile["model"] == "IC-R8600":
            widths.add(689)
        if len(self.bins) in widths:
            return (*self.edges, bytes(self.bins))
        return None


class Civ:
    def __init__(self, channel, address, on_frequency, on_scope, take_tune,
                 take_commands=lambda: (), on_state=lambda *_: None, profile=DEFAULT_PROFILE):
        self.profile = profile
        self.channel = channel
        self.address = address
        self.on_frequency = on_frequency
        self.on_scope = on_scope
        self.take_tune = take_tune
        self.take_commands = take_commands
        self.on_state = on_state
        self.nr = None
        self.toggle_nr = False
        self.scope = Scope(profile)
        self.seq = 0
        self.next_poll = 0
        self.next_command = 0
        self.pending = deque()
        self.poll_index = 0
        self.tune_target = None
        self.tune_until = 0
        self.retry_tune = 0
        self.next_open = 0
        self.next_scope = 0
        self.open = self.started = False
        self.scope_settings = {}
        self.changed_scope = set()
        self.last_scope = 0

    def send(self, command):
        frame = bytes((0xfe, 0xfe, self.address, CONTROLLER)) + command + b"\xfd"
        body = struct.pack("<BH", 0xc1, len(frame)) + struct.pack(">H", self.seq) + frame
        self.channel.send(0, body=body, tracked=True)
        self.seq = (self.seq + 1) & 0xffff

    def open_close(self, flag):
        body = b"\xc0\x01\x00" + struct.pack(">HB", self.seq, flag)
        self.channel.send(0, body=body, tracked=True)
        self.seq = (self.seq + 1) & 0xffff

    def schedule(self, command, urgent=False):
        if command not in self.pending:
            (self.pending.appendleft if urgent else self.pending.append)(command)

    def tick(self, now):
        if not self.channel.ready:
            return
        if not self.open and now >= self.next_open:
            self.open_close(4)
            self.schedule(b"\x03", urgent=True)
            self.started = True
            self.next_open = now + 0.5
        if self.open:
            target = self.take_tune()
            if target is not None and MIN_HZ <= target <= MAX_HZ:
                self.tune_target = target
                self.tune_until = now + 3
                self.retry_tune = now
            for action, value in self.take_commands():
                if action == "mode" and value in (0, 1, 3, 5):
                    self.schedule(bytes((6, value, 1)), urgent=True)
                elif action == "nr" and self.profile["nr"]:
                    if self.nr is None:
                        self.toggle_nr = not self.toggle_nr
                        self.schedule(b"\x16\x40", urgent=True)
                    else:
                        self.nr = not self.nr
                        self.schedule(bytes((0x16, 0x40, self.nr)), urgent=True)
                elif action == "tuner" and self.profile["tuner"] and self.profile["transmit"]:
                    self.schedule(b"\x1c\x01\x02", urgent=True)
            if self.tune_target is not None and now >= self.tune_until:
                self.tune_target = None
            if now >= self.next_poll and not self.pending and self.tune_target is None:
                queries = [b"\x03", b"\x04"]
                if self.profile["nr"]:
                    queries.append(b"\x16\x40")
                if self.profile["tuner"]:
                    queries.append(b"\x1c\x01")
                self.schedule(queries[self.poll_index % len(queries)])
                self.poll_index += 1
                self.next_poll = now + 0.15
            if self.profile["scope_bins"] and now >= self.next_scope and now - self.last_scope > 2:
                for setting in (0x10, 0x11):
                    self.schedule(bytes((0x27, setting)))
                self.next_scope = now + 2
        if now < self.next_command:
            return
        # Coalesce tuning to the latest target, ahead of routine state polls.
        if self.tune_target is not None and now >= self.retry_tune:
            self.send(b"\x05" + encode_frequency(self.tune_target))
            self.schedule(b"\x03", urgent=True)
            self.retry_tune = now + 0.2
        elif self.pending:
            self.send(self.pending.popleft())
        else:
            return
        self.next_command = now + 0.025

    def receive(self, packet):
        if len(packet) < 27 or struct.unpack_from("<H", packet, 17)[0] != len(packet) - 21:
            return
        for frame in packet[21:].split(b"\xfd")[:-1]:
            if len(frame) < 5 or frame[:2] != b"\xfe\xfe":
                continue
            command = frame[4:]
            destinations = (0, CONTROLLER, SCOPE_DESTINATION) if command.startswith(b"\x27\x00") else (0, CONTROLLER)
            if frame[2] not in destinations or frame[3] != self.address:
                continue
            self.open = True
            if len(command) in (6, 7) and command[0] in (0, 3, 5):
                frequency = decode_frequency(command[1:])
                if frequency is not None:
                    if frequency == self.tune_target:
                        self.tune_target = None
                    self.on_frequency(frequency)
            elif len(command) in (2, 3) and command[0] in (1, 4):
                self.on_state("mode", command[1])
            elif len(command) == 3 and command[:2] == b"\x16\x40" and command[2] in (0, 1):
                self.nr = bool(command[2])
                if self.toggle_nr:
                    self.toggle_nr = False
                    self.nr = not self.nr
                    self.schedule(bytes((0x16, 0x40, self.nr)), urgent=True)
                self.on_state("nr", self.nr)
            elif len(command) == 3 and command[:2] == b"\x1c\x01":
                self.on_state("tuner", command[2])
            elif command.startswith(b"\x27\x00\x00"):
                result = self.scope.parse(command[3:])
                if result:
                    self.last_scope = time.monotonic()
                    start, end, bins = result
                    maximum = self.profile["scope_max"]
                    self.on_scope(start, end, bytes(min(160, v * 160 // maximum) for v in bins))
            elif len(command) == 3 and command[0] == 0x27 and command[1] in (0x10, 0x11):
                setting, value = command[1:]
                if value in (0, 1):
                    self.scope_settings.setdefault(setting, value)
                    if value == 0:
                        self.schedule(bytes((0x27, setting, 1)))
                        self.changed_scope.add(setting)

    def close(self):
        for setting in (0x11, 0x10):
            if setting in self.changed_scope:
                self.send(bytes((0x27, setting, self.scope_settings[setting])))
        if self.started:
            self.open_close(0)
