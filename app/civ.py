"""Native Icom frequency, controls and spectrum transport."""

from collections import deque
import struct
import select
import time

from meters import Protection
from models import DEFAULT_PROFILE, MIN_HZ, MAX_HZ

CONTROLLER = 0xE0
SCOPE_DESTINATION = 0xE1


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
            size = (
                6 if self.profile["model"] == "IC-905" and len(data) in (16, 491) else 5
            )
            header_size = 4 + 2 * size
            if len(data) < header_size or data[header_size - 1]:
                return None
            start, end = decode_frequency(data[3 : 3 + size]), decode_frequency(
                data[3 + size : 3 + 2 * size]
            )
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
    def __init__(
        self,
        channel,
        address,
        on_frequency,
        on_scope,
        take_tune,
        take_commands=lambda: (),
        on_state=lambda *_: None,
        profile=DEFAULT_PROFILE,
        tx_enabled=False,
    ):
        self.profile = profile
        self.channel = channel
        self.address = address
        self.on_frequency = on_frequency
        self.on_scope = on_scope
        self.take_tune = take_tune
        self.take_commands = take_commands
        self.on_state = on_state
        self.tx_enabled = (
            tx_enabled and profile.get("transmit", False) and "tx_source" in profile
        )
        self.protection = Protection(profile, on_state)
        self.fault_applied = False
        self.clear_requested = None
        self.ptt_at = self.tuner_at = 0
        self.stop_retry = 0
        self.tune_seen_active = False
        self.tuning_requested = False
        self.tuner_state = None
        self.tune_started = None
        self.original_power = self.current_power = None
        self.requested_power = self.power_target = None
        self.power_applied = False
        self.power_changed = False
        self.tx_source = bytes.fromhex(profile.get("tx_source", ""))
        self.tx_controls = {
            name: bytes.fromhex(value)
            for name, value in profile.get("tx_controls", {}).items()
        }
        self.controls = {}
        self.original_controls = {}
        self.created = time.monotonic()
        self.started_at = None
        self.last_control = None
        self.source = self.data_mode = self.frequency = self.mode = None
        self.original_source = self.original_data = None
        self.preparing = self.prepared = False
        self.ptt_state = False
        self.ptt_wanted = None
        self.ptt_pending = False
        self.tx_poll = 0
        self.meter_poll = 0
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
        frame = bytes((0xFE, 0xFE, self.address, CONTROLLER)) + command + b"\xfd"
        body = (
            struct.pack("<BH", 0xC1, len(frame)) + struct.pack(">H", self.seq) + frame
        )
        self.channel.send(0, body=body, tracked=True)
        if command == b"\x1c\x01\x02":
            self.channel.history.pop((self.channel.seq - 1) & 0xFFFF, None)
            self.tune_started = time.monotonic()
            self.tune_seen_active = False
            self.protection.begin(tuning=True)
            self.on_state("tune_started", self.tune_started)
        self.seq = (self.seq + 1) & 0xFFFF

    def open_close(self, flag):
        body = b"\xc0\x01\x00" + struct.pack(">HB", self.seq, flag)
        self.channel.send(0, body=body, tracked=True)
        self.seq = (self.seq + 1) & 0xFFFF

    def schedule(self, command, urgent=False):
        if urgent:
            if command in self.pending:
                self.pending.remove(command)
            self.pending.appendleft(command)
        elif command not in self.pending:
            self.pending.append(command)

    def ptt(self, enabled):
        if self.tx_enabled:
            # PTT preempts tuning and polls but shares the radio's command pace.
            # An off request replaces an on request that has not been sent yet.
            self.ptt_wanted = bool(enabled)

    def tx_ready(self, dial):
        return (
            self.tx_enabled
            and self.prepared
            and not self.protection.fault
            and not self.tuning_requested
            and self.tuner_state != 2
            and (self.power_target is None or self.current_power == self.power_target)
            and self.source == self.profile["tx_lan"]
            and self.data_mode == 0
            and self.mode == 1
            and self.frequency == dial
            and all(self.controls.get(name) == 0 for name in self.tx_controls)
        )

    def cancel_tuner(self):
        active = self.tuner_state == 2 or (
            self.tuning_requested and self.tune_started is not None
        )
        self.pending = deque(cmd for cmd in self.pending if cmd != b"\x1c\x01\x02")
        self.tuning_requested = False
        if active:
            self.schedule(b"\x1c\x01", urgent=True)
            self.schedule(b"\x1c\x01\x00", urgent=True)

    def tick(self, now):
        if not self.channel.ready:
            return
        if self.tx_enabled:
            if self.ptt_state or self.tuning_requested or self.tuner_state == 2:
                self.protection.check()
            if self.protection.fault and (
                not self.fault_applied
                or (
                    (self.ptt_state or self.ptt_pending or self.tuner_state == 2)
                    and now >= self.stop_retry
                )
            ):
                self.fault_applied = True
                self.ptt(False)
                self.cancel_tuner()
                self.stop_retry = now + 0.25
            if (
                self.clear_requested is not None
                and self.ptt_at >= self.clear_requested
                and self.tuner_at >= self.clear_requested
                and not self.ptt_state
                and not self.ptt_pending
                and self.tuner_state != 2
            ):
                self.protection.clear()
                self.fault_applied = False
                self.clear_requested = None
                self.on_state("fault_clear_status", "Warning cleared")
        if not self.open and now >= self.next_open:
            self.open_close(4)
            self.schedule(b"\x03", urgent=True)
            self.started = True
            if self.started_at is None:
                self.started_at = now
            self.next_open = now + 0.5
            self.next_command = max(self.next_command, now + 0.03)
        if self.open:
            target = self.take_tune()
            if target is not None and MIN_HZ <= target <= MAX_HZ:
                self.tune_target = target
                self.tune_until = now + 3
                self.retry_tune = now
            if self.tx_enabled and self.mode is not None and self.frequency is not None:
                if now >= self.tx_poll:
                    self.schedule(b"\x1c\x00")
                    if not (
                        self.ptt_state or self.tuning_requested or self.tuner_state == 2
                    ):
                        self.schedule(self.tx_source)
                        self.schedule(b"\x1a\x06")
                        self.schedule(b"\x14\x0a")
                        for query in self.tx_controls.values():
                            self.schedule(query)
                    self.tx_poll = now + (
                        0.15
                        if self.preparing and not self.prepared
                        else (2 if self.ptt_state or self.tuning_requested else 0.5)
                    )
                if (
                    self.ptt_state
                    or self.tuning_requested
                    or self.tuner_state == 2
                    or self.protection.fault
                    or self.clear_requested is not None
                ) and now >= self.meter_poll:
                    queries = (
                        b"\x15\x12",
                        b"\x15\x13",
                        b"\x15\x11",
                        b"\x1c\x00",
                        b"\x1c\x01",
                    )
                    for query in reversed(queries):
                        self.schedule(query, urgent=True)
                    self.meter_poll = now + 0.25
                if (
                    self.requested_power is not None
                    and self.original_power is not None
                    and not self.power_applied
                    and not self.ptt_state
                    and not self.ptt_pending
                    and not self.tuning_requested
                    and self.tuner_state != 2
                ):
                    rated = self.profile.get("rated_power_w", 100)
                    if not 1 <= self.requested_power <= rated:
                        self.protection.trip(f"Power limit is {rated} W")
                    else:
                        self.power_target = round(self.requested_power / rated * 255)
                        self.schedule(b"\x14\x0a", urgent=True)
                        self.schedule(
                            b"\x14\x0a" + bytes.fromhex(f"{self.power_target:04d}"),
                            urgent=True,
                        )
                        self.power_applied = True
                        self.power_changed = True
                self.on_state(
                    "power_ready",
                    self.power_target is not None
                    and self.current_power == self.power_target,
                )
            if (
                self.preparing
                and not self.prepared
                and self.original_source is not None
                and self.original_data is not None
                and len(self.original_controls) == len(self.tx_controls)
                and not self.ptt_state
                and not self.ptt_pending
            ):
                self.schedule(
                    self.tx_source + bytes((self.profile["tx_lan"],)), urgent=True
                )
                self.schedule(b"\x1a\x06\x00\x01", urgent=True)
                for query in self.tx_controls.values():
                    self.schedule(query + b"\x00", urgent=True)
                self.prepared = True
            (
                self.on_state("tx_ready", self.tx_ready(self.frequency))
                if self.tx_enabled
                else None
            )
            for action, value in self.take_commands():
                if action == "clear_fault" and self.tx_enabled:
                    self.clear_requested = time.monotonic()
                    self.on_state("fault_clear_status", "Waiting for confirmed RX")
                    self.ptt(False)
                    self.cancel_tuner()
                    self.schedule(b"\x1c\x01", urgent=True)
                    self.schedule(b"\x1c\x00", urgent=True)
                elif action == "tx_fault" and self.tx_enabled:
                    self.protection.trip(str(value))
                elif action == "ft8_power" and self.tx_enabled:
                    self.requested_power = int(value)
                    self.power_target = None
                    self.on_state("power_ready", False)
                    self.power_applied = False
                elif action == "tuner_stop" and self.tx_enabled:
                    self.cancel_tuner()
                    if self.ptt_state or self.ptt_pending:
                        self.ptt(False)
                elif action == "ft8_prepare" and self.tx_enabled:
                    self.preparing = True
                elif (
                    action == "tx_power"
                    and self.tx_enabled
                    and (value is None or type(value) is int and 0 <= value <= 255)
                ):
                    self.schedule(
                        b"\x14\x0a"
                        + (b"" if value is None else bytes.fromhex(f"{value:04d}")),
                        urgent=True,
                    )
                elif action == "mode" and value in (0, 1, 3, 5):
                    self.schedule(bytes((6, value, 1)), urgent=True)
                elif action == "nr" and self.profile["nr"]:
                    if self.nr is None:
                        self.toggle_nr = not self.toggle_nr
                        self.schedule(b"\x16\x40", urgent=True)
                    else:
                        self.nr = not self.nr
                        self.schedule(bytes((0x16, 0x40, self.nr)), urgent=True)
                elif (
                    action == "tuner"
                    and self.profile["tuner"]
                    and self.profile["transmit"]
                ):
                    if not self.tx_enabled or not self.protection.fault:
                        self.tuning_requested = self.tx_enabled
                        self.tune_started = None
                        self.protection.started = None
                        self.schedule(b"\x1c\x01\x02", urgent=True)
            if self.tune_target is not None and now >= self.tune_until:
                self.tune_target = None
            if now >= self.next_poll and self.tune_target is None:
                queries = [b"\x03", b"\x04"]
                if self.profile["nr"]:
                    queries.append(b"\x16\x40")
                if self.profile["tuner"]:
                    queries.append(b"\x1c\x01")
                self.schedule(queries[self.poll_index % len(queries)])
                self.poll_index += 1
                self.next_poll = now + (
                    0.5 if self.ptt_state or self.tuning_requested else 0.15
                )
            if (
                self.profile["scope_bins"]
                and now >= self.next_scope
                and now - self.last_scope > 2
            ):
                for setting in ((0x11,) if self.tx_enabled else (0x10, 0x11)):
                    self.schedule(bytes((0x27, setting)), urgent=self.tx_enabled)
                self.next_scope = now + 2
        if now < self.next_command:
            return
        if self.ptt_wanted is not None:
            enabled = self.ptt_wanted and not self.protection.fault
            self.ptt_wanted = None
            self.send(bytes((0x1C, 0, int(enabled))))
            self.ptt_pending = True
            self.on_state("ptt_pending", True)
            if enabled:
                self.protection.begin()
                self.meter_poll = 0
                # Never replay an expired PTT-on in response to a late repair.
                self.channel.history.pop((self.channel.seq - 1) & 0xFFFF, None)
            self.schedule(b"\x1c\x00", urgent=True)
            self.next_command = now + 0.025
            return
        # Coalesce tuning to the latest target, ahead of routine state polls.
        if (
            self.tune_target is not None
            and now >= self.retry_tune
            and not (
                self.tx_enabled
                and (
                    self.ptt_state
                    or self.ptt_pending
                    or self.tuning_requested
                    or self.tuner_state == 2
                )
            )
        ):
            self.send(b"\x05" + encode_frequency(self.tune_target))
            self.schedule(b"\x03", urgent=True)
            self.retry_tune = now + 0.2
        elif self.pending:
            self.send(self.pending.popleft())
        else:
            return
        self.next_command = now + 0.025

    def receive(self, packet):
        if (
            len(packet) < 27
            or struct.unpack_from("<H", packet, 17)[0] != len(packet) - 21
        ):
            return
        for frame in packet[21:].split(b"\xfd")[:-1]:
            if len(frame) < 5 or frame[:2] != b"\xfe\xfe":
                continue
            command = frame[4:]
            destinations = (
                (0, CONTROLLER, SCOPE_DESTINATION)
                if command.startswith(b"\x27\x00")
                else (0, CONTROLLER)
            )
            if frame[2] not in destinations or frame[3] != self.address:
                continue
            if (
                self.started
                and frame[2] == CONTROLLER
                and not command.startswith(b"\x27\x00")
            ):
                self.open = True
                self.last_control = time.monotonic()
            for name, prefix in self.tx_controls.items():
                if command.startswith(prefix) and len(command) == len(prefix) + 1:
                    self.controls[name] = command[-1]
                    self.original_controls.setdefault(name, command[-1])
                    self.on_state(name, command[-1])
            if len(command) in (6, 7) and command[0] in (0, 3, 5):
                frequency = decode_frequency(command[1:])
                if frequency is not None:
                    if frequency == self.tune_target:
                        self.tune_target = None
                    self.frequency = frequency
                    self.on_frequency(frequency)
            elif len(command) in (2, 3) and command[0] in (1, 4):
                self.mode = command[1]
                self.on_state("mode", command[1])
            elif (
                self.tx_enabled
                and command.startswith(self.tx_source)
                and len(command) == len(self.tx_source) + 1
            ):
                self.source = command[-1]
                self.on_state("mod_source", self.source)
                if self.original_source is None:
                    self.original_source = self.source
            elif self.tx_enabled and command[:2] == b"\x1a\x06" and len(command) == 4:
                self.data_mode = command[2]
                self.on_state("data_mode", self.data_mode)
                if self.original_data is None:
                    self.original_data = command[2:]
            elif self.tx_enabled and len(command) == 4 and command[:2] == b"\x14\x0a":
                self.current_power = decode_frequency(command[2:][::-1])
                if self.original_power is None:
                    self.original_power = self.current_power
                self.on_state("tx_power", self.current_power)
            elif (
                self.tx_enabled
                and len(command) == 4
                and command[:2] in (b"\x15\x11", b"\x15\x13", b"\x15\x12")
            ):
                value = decode_frequency(command[2:][::-1])
                self.on_state(
                    {0x11: "rf_meter", 0x12: "swr_meter", 0x13: "alc_meter"}[
                        command[1]
                    ],
                    value,
                )
                self.protection.update(
                    {0x11: "watts", 0x12: "swr", 0x13: "alc"}[command[1]], value
                )
            elif len(command) == 3 and command[:2] == b"\x1c\x00":
                self.ptt_state = bool(command[2])
                self.ptt_at = time.monotonic()
                if (
                    self.tuning_requested
                    and self.tune_started is not None
                    and self.ptt_state
                ):
                    self.tune_seen_active = True
                self.ptt_pending = False
                self.on_state("ptt_pending", False)
                self.on_state("ptt", self.ptt_state)
            elif (
                len(command) == 3
                and command[:2] == b"\x16\x40"
                and command[2] in (0, 1)
            ):
                self.nr = bool(command[2])
                if self.toggle_nr:
                    self.toggle_nr = False
                    self.nr = not self.nr
                    self.schedule(bytes((0x16, 0x40, self.nr)), urgent=True)
                self.on_state("nr", self.nr)
            elif len(command) == 3 and command[:2] == b"\x1c\x01":
                self.tuner_state = command[2]
                self.tuner_at = time.monotonic()
                if (
                    command[2] == 2
                    and self.tuning_requested
                    and self.tune_started is not None
                ):
                    self.tune_seen_active = True
                self.on_state("tuner", command[2])
                self.on_state("tuner_at", self.tuner_at)
                self.on_state("tune_seen_active", self.tune_seen_active)
                if (
                    self.tuning_requested
                    and self.tune_started is not None
                    and time.monotonic() - self.tune_started > 0.6
                    and (command[2] == 1 or (command[2] == 0 and self.tune_seen_active))
                    and not self.ptt_state
                ):
                    self.tuning_requested = False
                    self.protection.started = None
            elif command.startswith(b"\x27\x00\x00") and not self.tx_enabled:
                result = self.scope.parse(command[3:])
                if result:
                    self.last_scope = time.monotonic()
                    start, end, bins = result
                    maximum = self.profile["scope_max"]
                    self.on_scope(
                        start, end, bytes(min(160, v * 160 // maximum) for v in bins)
                    )
            elif (
                len(command) == 3 and command[0] == 0x27 and command[1] in (0x10, 0x11)
            ):
                setting, value = command[1:]
                if value in (0, 1):
                    self.scope_settings.setdefault(setting, value)
                    wanted = 0 if self.tx_enabled and setting == 0x11 else 1
                    if value != wanted:
                        self.schedule(
                            bytes((0x27, setting, wanted)), urgent=self.tx_enabled
                        )
                        self.changed_scope.add(setting)

    def close(self):
        commands = []
        if self.tx_enabled:
            self.ptt_wanted = None
            if (self.tuning_requested or self.tuner_state == 2) and hasattr(
                self.channel, "sock"
            ):
                self.send(b"\x1c\x01\x00")
                time.sleep(0.03)
                self.send(b"\x1c\x01")
                time.sleep(0.03)
                self.tuning_requested = False
            if hasattr(self.channel, "sock"):
                # Confirm the final unkey before restoring inputs or split.
                # Repeated PTT-off requests may still be awaiting readback.
                deadline = time.monotonic() + 0.4
                retry = 0
                self.ptt_pending = True
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if now >= retry:
                        time.sleep(max(0, self.next_command - now))
                        self.send(b"\x1c\x00\x00")
                        time.sleep(0.03)
                        self.send(b"\x1c\x00")
                        self.next_command = time.monotonic() + 0.03
                        retry = time.monotonic() + 0.1
                    if select.select([self.channel.sock], [], [], 0.02)[0]:
                        for kind, packet in self.channel.drain():
                            if kind == 0:
                                self.receive(packet)
                    if (
                        not self.ptt_state
                        and not self.ptt_pending
                        and self.tuner_state != 2
                    ):
                        break
            else:
                commands.append(b"\x1c\x00\x00")
            if (
                (self.prepared or self.power_changed)
                and not self.ptt_state
                and not self.ptt_pending
                and self.tuner_state != 2
            ):
                if self.original_power is not None and self.power_changed:
                    commands.append(
                        b"\x14\x0a" + bytes.fromhex(f"{self.original_power:04d}")
                    )
                if self.original_source is not None:
                    commands.append(self.tx_source + bytes((self.original_source,)))
                if self.original_data is not None:
                    commands.append(b"\x1a\x06" + self.original_data)
                for name, value in self.original_controls.items():
                    commands.append(self.tx_controls[name] + bytes((value,)))
        for setting in (0x11, 0x10):
            if setting in self.changed_scope:
                commands.append(bytes((0x27, setting, self.scope_settings[setting])))
        for command in commands:
            time.sleep(max(0, self.next_command - time.monotonic()))
            self.send(command)
            self.next_command = time.monotonic() + 0.03
        if self.started:
            time.sleep(max(0, self.next_command - time.monotonic()))
            self.open_close(0)
