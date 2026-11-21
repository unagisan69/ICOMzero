"""TS-890S native KNS TCP control and 16 kHz LPCM RTP receive audio.

Protocol reference: wfview kenwoodCommander and rtpAudio at the radios.json commit.
"""
from array import array
from contextlib import suppress
import select
import socket
import struct
import sys
import time

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
            output.extend((previous, (2 * previous + sample) // 3, (previous + 2 * sample) // 3))
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
    def __init__(self, send, user, password, admin, on_frequency, on_scope, on_state):
        self.send = send
        self.user, self.password, self.admin = user, password, admin
        self.on_frequency, self.on_scope, self.on_state = on_frequency, on_scope, on_state
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
            self.send(f"##ID{0 if self.admin else 1}{len(self.user):02d}{len(self.password):02d}"
                      f"{self.user}{self.password}")
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
            self.on_frequency(int(command[2:]))
        elif command.startswith("OM0") and command[3:].isdigit():
            mode = MODE_TO_ICOM.get(int(command[3:4]))
            if mode is not None:
                self.on_state("mode", mode)
        elif command.startswith("NR") and command[2:] in ("0", "1", "2"):
            self.nr = int(command[2:]) != 0
            if self.pending_nr:
                self.pending_nr = False
                self.toggle_noise()
            self.on_state("nr", self.nr)
        elif command.startswith("AC") and len(command) == 5 and command[2:].isdigit():
            state = 2 if command[-1] != "0" else int(command[-2] != "0")
            self.on_state("tuner", state)
        elif command.startswith("DD0") and command[3:].isdigit() and self.original_scope is None:
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
            self.edges = ((second - first // 2, second + first // 2) if data[2] == "0"
                          else (first, second))
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
        levels = bytes(max(0, PROFILE["scope_max"] - v) * 160 // PROFILE["scope_max"] for v in raw)
        self.on_scope(start, end, levels)

    def toggle_noise(self):
        if self.nr is None:
            self.pending_nr = not self.pending_nr
            self.send("NR")
        else:
            self.nr = not self.nr
            self.send(f"NR{int(self.nr)}")

    def action(self, action, value):
        if action == "mode" and value in MODE_FROM_ICOM:
            self.send(f"OM0{MODE_FROM_ICOM[value]}")
        elif action == "nr":
            self.toggle_noise()
        elif action == "tuner":
            self.send("AC111")

    def close(self):
        self.send("##VP0")
        if self.original_scope is not None:
            self.send("DD0" + self.original_scope)


def listen(host, port, username, password, stop, on_status, on_audio,
           on_frequency, on_scope, take_tune, take_commands, on_state, audio_port, admin):
    for value in (username, password):
        if not 1 <= len(value) <= 99 or any(not 32 <= ord(c) <= 126 or c == ";" for c in value):
            raise RadioError("KNS login requires 1–99 ASCII characters without semicolons")
    on_status("Finding radio")
    address = socket.gethostbyname(host)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as audio:
        audio.bind(("", audio_port))
        audio.setblocking(False)
        with socket.create_connection((address, port), timeout=5) as control:
            def send(command):
                control.sendall(command.encode("ascii") + b";\n")

            cat = Cat(send, username, password, admin, on_frequency, on_scope, on_state)
            pcm = Pcm()
            buffer = bytearray()
            last_seq = None
            deadline = time.monotonic() + 8
            poll = 0
            on_status("Signing in")
            send("##CN")
            try:
                while not stop.is_set():
                    now = time.monotonic()
                    if now >= deadline:
                        raise ConnectionLost("KNS audio timed out; check UDP audio port and radio settings")
                    if cat.authenticated and now >= poll:
                        for query in ("FA", "OM0", "NR", "AC", "DD4", "##DD2"):
                            send(query)
                        poll = now + 0.25
                    if cat.authenticated:
                        for action, value in take_commands():
                            cat.action(action, value)
                        target = take_tune()
                        if target is not None and 30_000 <= target <= 74_800_000:
                            send(f"FA{target:011d}")
                    readable, _, _ = select.select((control, audio), (), (), 0.05)
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
                            if last_seq is not None and not 0 < (seq - last_seq) % 65536 < 32768:
                                continue
                            last_seq = seq
                            deadline = now + 6
                            on_status("Receiving audio")
                            on_audio(pcm.convert(samples))
            finally:
                if cat.authenticated:
                    with suppress(OSError):
                        cat.close()
