"""IC-7300MK2 frequency and spectrum commands on the Icom CAT channel."""

import struct
import time

CONTROLLER = 0xe0
SCOPE_DESTINATION = 0xe1
SCOPE_BINS = 475
MIN_HZ, MAX_HZ = 30_000, 74_800_000


def decode_frequency(data):
    if any((value & 15) > 9 or (value >> 4) > 9 for value in data):
        return None
    return int("".join(f"{value:02x}" for value in reversed(data)))


def encode_frequency(hz):
    return bytes.fromhex(f"{hz:010d}")[::-1]


class Scope:
    def __init__(self):
        self.edges = None
        self.next_part = 0
        self.bins = bytearray()

    def parse(self, data):
        if len(data) < 2:
            return None
        part, total = data[:2]
        if part == 1:
            self.bins.clear()
            self.next_part = 0
            if len(data) < 14 or data[13]:
                return None
            start, end = decode_frequency(data[3:8]), decode_frequency(data[8:13])
            if start is None or end is None:
                return None
            if data[2] == 0:  # Center mode reports the center and half-span.
                start, end = start - end, start + end
            if not 0 < start < end:
                return None
            self.edges = (start, end)
            if total != 1:
                if total == 0x11 and len(data) == 14:
                    self.next_part = 2
                return None
            self.bins.extend(data[14:])
        else:
            if total != 0x11 or part != self.next_part or not self.next_part:
                self.next_part = 0
                return None
            self.bins.extend(data[2:])
            self.next_part = 0 if part == 0x11 else (0x10 if part == 9 else part + 1)
            if self.next_part:
                return None
        if len(self.bins) == SCOPE_BINS:
            return (*self.edges, bytes(self.bins))
        return None


class Civ:
    def __init__(self, channel, address, on_frequency, on_scope, take_tune,
                 take_commands=lambda: (), on_state=lambda *_: None):
        self.channel = channel
        self.address = address
        self.on_frequency = on_frequency
        self.on_scope = on_scope
        self.take_tune = take_tune
        self.take_commands = take_commands
        self.on_state = on_state
        self.nr = None
        self.toggle_nr = False
        self.scope = Scope()
        self.seq = 0
        self.next_poll = 0
        self.next_open = 0
        self.next_scope = 0
        self.next_tune = 0
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

    def tick(self, now):
        if not self.channel.ready:
            return
        if not self.open and now >= self.next_open:
            self.open_close(4)
            self.send(b"\x03")
            self.started = True
            self.next_open = now + 0.5
        if not self.open:
            return
        if now >= self.next_poll:
            self.send(b"\x03")
            self.send(b"\x04")
            self.send(b"\x16\x40")
            self.send(b"\x1c\x01")
            self.next_poll = now + 0.4
        for action, value in self.take_commands():
            if action == "mode" and value in (0, 1, 3, 5):
                self.send(bytes((6, value, 1)))
            elif action == "nr":
                if self.nr is None:
                    self.toggle_nr = not self.toggle_nr
                    self.send(b"\x16\x40")
                else:
                    self.nr = not self.nr
                    self.send(bytes((0x16, 0x40, self.nr)))
            elif action == "tuner":
                self.send(b"\x1c\x01\x02")
        if now >= self.next_tune:
            target = self.take_tune()
            if target is not None and MIN_HZ <= target <= MAX_HZ:
                self.send(b"\x05" + encode_frequency(target))
                self.next_tune = now + 0.08
                self.next_poll = now + 0.12
        if now >= self.next_scope and now - self.last_scope > 2:
            for setting in (0x10, 0x11):
                self.send(bytes((0x27, setting)))
            self.next_scope = now + 2

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
            if len(command) == 6 and command[0] in (0, 3, 5):
                frequency = decode_frequency(command[1:])
                if frequency is not None:
                    self.on_frequency(frequency)
            elif len(command) in (2, 3) and command[0] in (1, 4):
                self.on_state("mode", command[1])
            elif len(command) == 3 and command[:2] == b"\x16\x40" and command[2] in (0, 1):
                self.nr = bool(command[2])
                if self.toggle_nr:
                    self.toggle_nr = False
                    self.nr = not self.nr
                    self.send(bytes((0x16, 0x40, self.nr)))
                self.on_state("nr", self.nr)
            elif len(command) == 3 and command[:2] == b"\x1c\x01":
                self.on_state("tuner", command[2])
            elif command.startswith(b"\x27\x00\x00"):
                result = self.scope.parse(command[3:])
                if result:
                    self.last_scope = time.monotonic()
                    self.on_scope(*result)
            elif len(command) == 3 and command[0] == 0x27 and command[1] in (0x10, 0x11):
                setting, value = command[1:]
                if value in (0, 1):
                    self.scope_settings.setdefault(setting, value)
                    if value == 0:
                        self.send(bytes((0x27, setting, 1)))
                        self.changed_scope.add(setting)

    def close(self):
        for setting in (0x11, 0x10):
            if setting in self.changed_scope:
                self.send(bytes((0x27, setting, self.scope_settings[setting])))
        if self.started:
            self.open_close(0)
