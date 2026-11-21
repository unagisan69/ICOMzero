"""TS-890S native KNS TCP control and 16 kHz LPCM RTP audio.

Protocol reference: wfview kenwoodCommander and rtpAudio at the radios.json commit.
"""

from array import array
from contextlib import suppress
import select
import socket
import struct
import sys
import time

from meters import Protection
from models import PROFILES
from radio import RadioError, ConnectionLost

PROFILE = PROFILES["TS-890S"]
MODE_TO_ICOM = {1: 0, 2: 1, 3: 3, 4: 5, 5: 2, 6: 4, 7: 7, 9: 8}
MODE_FROM_ICOM = {value: key for key, value in MODE_TO_ICOM.items()}


class Pcm:
    """Convert streaming 16 kHz little-endian mono to the app's 48 kHz PCM."""

    def __init__(self):
        self.previous = None

    def convert(self, data):
        source = array("h", data)
        if sys.byteorder != "little":
            source.byteswap()
        output = array("h")
        previous = self.previous
        for sample in source:
            if previous is None:
                previous = sample
            output.extend(
                (previous, (2 * previous + sample) // 3, (previous + 2 * sample) // 3)
            )
            previous = sample
        self.previous = previous
        if sys.byteorder != "little":
            output.byteswap()
        return output.tobytes()


def rtp_payload(data):
    if len(data) < 12 or data[0] >> 6 != 2 or data[1] & 127 != 96:
        return None
    offset = 12 + (data[0] & 15) * 4
    if data[0] & 16:
        if len(data) < offset + 4:
            return None
        words = struct.unpack_from(">H", data, offset + 2)[0]
        offset += 4 + words * 4
    end = len(data)
    if data[0] & 32:
        padding = data[-1]
        if not padding or padding > end - offset:
            return None
        end -= padding
    if offset >= end or (end - offset) % 2:
        return None
    return struct.unpack_from(">H", data, 2)[0], data[offset:end]


class Cat:
    def __init__(
        self,
        send,
        user,
        password,
        admin,
        on_frequency,
        on_scope,
        on_state,
        tx_enabled=False,
    ):
        self.send = send
        self.user, self.password, self.admin = user, password, admin
        self.on_frequency, self.on_scope, self.on_state = (
            on_frequency,
            on_scope,
            on_state,
        )
        self.protection = Protection(PROFILE, on_state)
        self.fault_applied = False
        self.tuning_requested = False
        self.tuner_state = None
        self.tune_started = None
        self.original_power = self.current_power = None
        self.requested_power = self.power_target = None
        self.meters_subscribed = False
        self.tx_enabled = tx_enabled
        self.frequency = self.mode = None
        self.controls = {}
        self.original_controls = {}
        self.sources = {}
        self.original_sources = {}
        self.preparing = self.prepared = False
        self.ptt_state = False
        self.ptt_pending = False
        self.ptt_expected = None
        self.authenticated = False
        self.original_scope = None
        self.nr = None
        self.pending_nr = False
        self.edges = None
        self.parts = bytearray()
        self.next_part = None

    def receive(self, command):
        if command.startswith("##CN"):
            if command != "##CN1":
                raise RadioError("Radio refused the connection")
            self.send(
                f"##ID{0 if self.admin else 1}{len(self.user):02d}{len(self.password):02d}"
                f"{self.user}{self.password}"
            )
        elif command.startswith("##ID"):
            if command != "##ID1":
                raise RadioError("Radio rejected the login")
            if not self.authenticated:
                self.authenticated = True
                self.on_state("model", "TS-890S")
                self.on_state("profile", PROFILE)
                for query in ("ID", "DD0", "##VP1", "FA", "OM0", "NR", "AC"):
                    self.send(query)
        elif command.startswith("FA") and command[2:].isdigit():
            self.frequency = int(command[2:])
            self.on_frequency(self.frequency)
        elif command.startswith("OM0") and command[3:].isdigit():
            mode = MODE_TO_ICOM.get(int(command[3:4]))
            if mode is not None:
                self.mode = mode
                self.on_state("mode", mode)
        elif (
            self.tx_enabled and command[:3] in ("MS0", "MS1") and command[3:].isdigit()
        ):
            source = command[:3]
            value = int(command[3:])
            self.sources[source] = value
            self.original_sources.setdefault(source, value)
        elif (
            self.tx_enabled
            and command.startswith("IF")
            and len(command) > 28
            and command[28] in "01"
        ):
            self.ptt_state = command[28] == "1"
            if self.ptt_expected is None or self.ptt_state == self.ptt_expected:
                self.ptt_pending = False
            if (
                not self.ptt_state
                and not self.ptt_pending
                and not self.tuning_requested
                and self.tuner_state != 2
            ):
                self.protection.end()
            self.on_state("ptt_pending", self.ptt_pending)
            self.on_state("ptt", self.ptt_state)
        elif (
            self.tx_enabled
            and command[:2] in ("TB", "RT", "XT")
            and command[2:] in ("0", "1")
        ):
            self.controls[command[:2]] = int(command[2:])
            self.original_controls.setdefault(command[:2], int(command[2:]))
        elif self.tx_enabled and command.startswith("PC") and command[2:].isdigit():
            self.current_power = int(command[2:])
            if self.original_power is None:
                self.original_power = self.current_power
            self.on_state(
                "power_ready",
                self.power_target is not None
                and self.current_power == self.power_target,
            )
        elif (
            self.tx_enabled
            and len(command) == 7
            and command[:3] in ("RM1", "RM2")
            and command[3:].isdigit()
        ):
            self.protection.update(
                "alc" if command[2] == "1" else "swr", int(command[3:])
            )
        elif (
            self.tx_enabled
            and command.startswith("SM")
            and command[2:].isdigit()
            and (self.ptt_state or self.tuning_requested)
        ):
            self.protection.update("watts", int(command[2:]))
        elif command.startswith("NR") and command[2:] in ("0", "1", "2"):
            self.nr = int(command[2:]) != 0
            if self.pending_nr:
                self.pending_nr = False
                self.toggle_noise()
            self.on_state("nr", self.nr)
        elif command.startswith("AC") and len(command) == 5 and command[2:].isdigit():
            state = 2 if command[-1] != "0" else int(command[-2] != "0")
            self.tuner_state = state
            self.on_state("tuner", state)
            self.on_state("tuner_at", time.monotonic())
            if (
                self.tuning_requested
                and self.tune_started
                and time.monotonic() - self.tune_started > 0.6
                and state != 2
                and not self.ptt_state
            ):
                self.tuning_requested = False
                self.protection.end()
        elif (
            command.startswith("DD0")
            and command[3:].isdigit()
            and self.original_scope is None
        ):
            self.original_scope = command[3:]
            self.send("DD01")
        elif command.startswith("DD4"):
            self.scope_part(command[3:])
        elif command.startswith("##DD2"):
            self.emit_scope(command[5:])
        elif command.startswith("DD2"):
            self.scope_part(command[3:])
        elif command == "##VP0" and self.authenticated:
            self.send("##VP1")

    def scope_part(self, data):
        if len(data) < 2 or not data[:2].isdigit():
            return
        part = int(data[:2])
        if part == 0:
            self.parts.clear()
            self.next_part = 1
            if len(data) < 26 or data[25] != "0" or not data[2:25].isdigit():
                self.edges = None
                self.next_part = None
                return
            first, second = int(data[3:14]), int(data[14:25])
            self.edges = (
                (second - first // 2, second + first // 2)
                if data[2] == "0"
                else (first, second)
            )
            return
        if part != self.next_part or not self.edges:
            self.next_part = None
            self.parts.clear()
            return
        self.parts.extend(data[2:].encode("ascii"))
        self.next_part = part + 1
        if part == PROFILE["scope_parts"]:
            self.emit_scope(self.parts.decode("ascii"))
            self.next_part = None

    def emit_scope(self, encoded):
        if not self.edges or len(encoded) != PROFILE["scope_bins"] * 2:
            return
        try:
            raw = bytes.fromhex(encoded)
        except ValueError:
            return
        start, end = self.edges
        if not 0 < start < end:
            return
        levels = bytes(
            max(0, PROFILE["scope_max"] - v) * 160 // PROFILE["scope_max"] for v in raw
        )
        self.on_scope(start, end, levels)

    def toggle_noise(self):
        if self.nr is None:
            self.pending_nr = not self.pending_nr
            self.send("NR")
        else:
            self.nr = not self.nr
            self.send(f"NR{int(self.nr)}")

    def ptt(self, enabled):
        if self.tx_enabled:
            enabled = enabled and not self.protection.fault
            if enabled:
                self.protection.begin()
            self.send("TX0" if enabled else "RX")
            self.ptt_pending = True
            self.ptt_expected = enabled
            self.on_state("ptt_pending", True)
            self.send("IF")

    def tx_ready(self, dial):
        return (
            self.tx_enabled
            and self.prepared
            and not self.protection.fault
            and not self.tuning_requested
            and self.tuner_state != 2
            and (self.power_target is None or self.current_power == self.power_target)
            and self.sources.get("MS0") == 3
            and self.sources.get("MS1") == 3
            and self.frequency == dial
            and self.mode == 1
            and all(self.controls.get(name) == 0 for name in ("TB", "RT", "XT"))
        )

    def tx_poll(self):
        if not self.meters_subscribed:
            self.send("RM11")
            self.send("RM21")
            self.meters_subscribed = True
        self.send("PC")
        if (
            self.requested_power is not None
            and self.original_power is not None
            and not self.ptt_state
            and not self.ptt_pending
            and not self.tuning_requested
            and self.tuner_state != 2
        ):
            if not 5 <= self.requested_power <= 100:
                self.protection.trip("Power must be 5–100 W")
            elif self.current_power != self.requested_power:
                self.power_target = self.requested_power
                self.send(f"PC{self.power_target:03d}")
            else:
                self.power_target = self.requested_power
            self.on_state("power_ready", self.current_power == self.power_target)
        if self.ptt_state or self.tuning_requested or self.tuner_state == 2:
            self.send("RM")
            self.send("SM")
            self.send("AC")
        reason = (
            self.protection.check()
            if self.ptt_state or self.tuning_requested or self.tuner_state == 2
            else None
        )
        if reason and not self.fault_applied:
            self.fault_applied = True
            active_tuner = self.tuning_requested or self.tuner_state == 2
            self.tuning_requested = False
            self.ptt(False)
            if active_tuner:
                self.send("AC100")

        if (
            self.preparing
            and not self.prepared
            and len(self.original_sources) == 2
            and len(self.original_controls) == 3
            and not self.ptt_state
            and not self.ptt_pending
        ):
            self.send("MS003")
            self.send("MS103")
            for query in ("TB", "RT", "XT"):
                self.send(query + "0")
            self.prepared = True
        for query in ("MS0", "MS1", "IF", "TB", "RT", "XT"):
            self.send(query)
        self.on_state("tx_ready", self.tx_ready(self.frequency))

    def action(self, action, value):
        if (
            action == "clear_fault"
            and self.tx_enabled
            and not self.ptt_state
            and self.tuner_state != 2
        ):
            self.protection.clear()
            self.fault_applied = False
        elif action == "tx_fault" and self.tx_enabled:
            self.protection.trip(str(value))
        elif action == "ft8_power" and self.tx_enabled:
            self.requested_power = int(value)
            self.power_target = None
            self.on_state("power_ready", False)
        elif action in ("tuner_stop", "force_rx") and self.tx_enabled:
            active_tuner = self.tuning_requested or self.tuner_state == 2
            self.tuning_requested = False
            self.ptt(False)
            if active_tuner:
                self.send("AC100")
        elif action == "ft8_prepare" and self.tx_enabled:
            self.preparing = True
        elif action == "mode" and value in MODE_FROM_ICOM:
            self.send(f"OM0{MODE_FROM_ICOM[value]}")
        elif action == "nr":
            self.toggle_noise()
        elif action == "tuner" and not self.protection.fault:
            self.tuning_requested = self.tx_enabled
            self.tune_started = time.monotonic()
            if self.tx_enabled:
                self.protection.begin(tuning=True)
                self.on_state("tune_started", self.tune_started)
            self.send("AC111")

    def close(self):
        if self.tx_enabled:
            if self.tuning_requested or self.tuner_state == 2:
                self.send("AC100")
            self.ptt(False)
            confirmed_rx = self.ptt_state is False and self.tuner_state != 2
            if (
                confirmed_rx
                and self.original_power is not None
                and self.requested_power is not None
            ):
                self.send(f"PC{self.original_power:03d}")
            if self.prepared and confirmed_rx:
                for source, value in self.original_sources.items():
                    self.send(f"{source}{value:02d}")
                for query, value in self.original_controls.items():
                    self.send(f"{query}{value}")
        self.send("##VP0")
        if self.original_scope is not None:
            self.send("DD0" + self.original_scope)


def listen(
    host,
    port,
    username,
    password,
    stop,
    on_status,
    on_audio,
    on_frequency,
    on_scope,
    take_tune,
    take_commands,
    on_state,
    audio_port,
    admin,
    tx_enabled=False,
    take_tx=lambda: None,
):
    for value in (username, password):
        if not 1 <= len(value) <= 99 or any(
            not 32 <= ord(c) <= 126 or c == ";" for c in value
        ):
            raise RadioError(
                "KNS login requires 1–99 ASCII characters without semicolons"
            )
    on_status("Finding radio")
    address = socket.gethostbyname(host)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as audio:
        audio.bind(("", audio_port))
        audio.setblocking(False)
        with socket.create_connection((address, port), timeout=5) as control:

            def send(command):
                control.sendall(command.encode("ascii") + b";\n")

            cat = Cat(
                send,
                username,
                password,
                admin,
                on_frequency,
                on_scope,
                on_state,
                tx_enabled,
            )
            pcm = Pcm()
            buffer = bytearray()
            last_seq = None
            deadline = time.monotonic() + 8
            poll = 0
            pacer = None
            tx_sequence = 0
            pending_tune = None
            if tx_enabled:
                from ft8tx import Pacer

                def tx_job():
                    job = take_tx()
                    if job is not None:
                        samples = array("h", job.pcm)
                        job.pcm = samples[::3].tobytes()
                    return job

                def send_pcm(partial):
                    nonlocal tx_sequence
                    header = struct.pack(">BBHII", 0x80, 96, tx_sequence, 0, 0x38393000)
                    audio.sendto(header + partial, (address, audio_port))
                    tx_sequence = (tx_sequence + 1) & 65535

                pacer = Pacer(
                    tx_job,
                    cat.ptt,
                    send_pcm,
                    cat.tx_ready,
                    lambda: cat.ptt_state,
                    lambda value: on_state("tx_status", value),
                    16000,
                    protection=cat.protection.check,
                    off_confirmed=lambda: not cat.ptt_state and not cat.ptt_pending,
                )
            on_status("Signing in")
            send("##CN")
            try:
                while not stop.is_set():
                    now = time.monotonic()
                    if pacer and cat.authenticated:
                        pacer.tick()
                        if pacer.armed or cat.tuning_requested or cat.tuner_state == 2:
                            deadline = now + 6
                    if now >= deadline:
                        raise ConnectionLost(
                            "KNS audio timed out; check UDP audio port and radio settings"
                        )
                    if cat.authenticated and now >= poll:
                        for query in ("FA", "OM0", "NR", "AC", "DD4", "##DD2"):
                            send(query)
                        if tx_enabled:
                            cat.tx_poll()
                        poll = now + 0.1 if pacer and pacer.armed else now + 0.25
                    if cat.authenticated:
                        for action, value in take_commands():
                            cat.action(action, value)
                        target = take_tune()
                        if target is not None:
                            pending_tune = target
                        if (
                            pending_tune is not None
                            and 30_000 <= pending_tune <= 74_800_000
                            and not (
                                cat.ptt_state
                                or cat.ptt_pending
                                or cat.tuning_requested
                                or cat.tuner_state == 2
                            )
                        ):
                            send(f"FA{pending_tune:011d}")
                            pending_tune = None
                    readable, _, _ = select.select(
                        (control, audio),
                        (),
                        (),
                        0.005 if pacer and pacer.armed else 0.02,
                    )
                    for source in readable:
                        if source is control:
                            data = control.recv(8192)
                            if not data:
                                raise ConnectionLost("Radio disconnected")
                            buffer.extend(data)
                            if len(buffer) > 65536:
                                raise RadioError("Malformed KNS control reply")
                            while b";" in buffer:
                                command, _, rest = buffer.partition(b";")
                                buffer = bytearray(rest)
                                try:
                                    cat.receive(command.decode("ascii").strip())
                                except UnicodeDecodeError:
                                    continue
                        else:
                            data, peer = audio.recvfrom(65535)
                            if peer[0] != address or not cat.authenticated:
                                continue
                            parsed = rtp_payload(data)
                            if not parsed:
                                continue
                            seq, samples = parsed
                            if (
                                last_seq is not None
                                and not 0 < (seq - last_seq) % 65536 < 32768
                            ):
                                continue
                            last_seq = seq
                            deadline = now + 6
                            on_status("Receiving audio")
                            on_audio(pcm.convert(samples))
            finally:
                if pacer:
                    with suppress(OSError):
                        pacer.close()
                if cat.authenticated:
                    with suppress(OSError):
                        cat.close()
