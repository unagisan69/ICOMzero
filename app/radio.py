"""Receive-only Icom LAN transport, based on wfview's protocol implementation."""

from collections import OrderedDict
from contextlib import ExitStack, suppress
from secrets import randbits
import selectors
import socket
import struct
import time

from civ import Civ

HEADER = struct.Struct("<IHHII")
RATE = 48000
MODEL = "IC-7300MK2"
CLIENT = b"ICOMzero"
# Icom's positional substitution table for printable ASCII credentials.
PASSCODE = bytes.fromhex(
    "47 5d 4c 42 66 20 23 46 4e 57 45 3d 67 76 60 41 62 39 59 2d 68 7e "
    "7c 65 7d 49 29 72 73 78 21 6e 5a 5e 4a 3e 71 2c 2a 54 3c 3a 63 4f "
    "43 75 27 79 5b 35 70 48 6b 56 6f 34 32 6c 30 61 6d 7b 2f 4b 64 38 "
    "2b 2e 50 40 3f 55 33 37 25 77 24 26 74 6a 28 53 4d 69 22 5c 44 31 "
    "36 58 3b 7a 51 5f 52"
)


class RadioError(Exception):
    pass


def encode(value):
    if not 1 <= len(value) <= 16 or any(not 32 <= ord(c) <= 126 for c in value):
        raise RadioError("Login fields require 1–16 ASCII characters")
    result = bytearray()
    for index, char in enumerate(value):
        shifted = ord(char) + index
        result.append(PASSCODE[shifted - 32 if shifted < 127 else shifted % 127])
    return bytes(result).ljust(16, b"\0")


class Channel:
    def __init__(self, sock, port):
        self.sock = sock
        octets = socket.inet_aton(sock.getsockname()[0])
        self.local = int.from_bytes(octets[2:], "big") << 16 | port
        self.remote = 0
        self.ready = False
        self.seq = 0
        self.history = OrderedDict()
        self.next_ping = self.next_idle = self.next_probe = 0

    def send(self, kind, seq=0, body=b"", tracked=False):
        if tracked:
            seq = self.seq
            self.seq = (seq + 1) & 0xffff
        data = HEADER.pack(16 + len(body), kind, seq, self.local, self.remote) + body
        if tracked:
            self.history[seq] = data
            if len(self.history) > 128:
                self.history.popitem(last=False)
        with suppress(BlockingIOError):
            self.sock.send(data)

    def tick(self, now):
        if not self.ready and now >= self.next_probe:
            if self.remote:
                self.send(6, 1)
            else:
                self.send(3)
            self.next_probe = now + 0.5
        if self.remote and now >= self.next_ping:
            stamp = int(now * 1000) & 0xffffffff
            self.send(7, body=struct.pack("<BI", 0, stamp))
            self.next_ping = now + 0.5
        if self.remote and now >= self.next_idle:
            self.send(0, tracked=True)
            self.next_idle = now + 0.1

    def receive(self):
        data = self.sock.recv(65535)
        if len(data) < 16:
            return None
        size, kind, seq, sender, receiver = HEADER.unpack_from(data)
        if size != len(data) or receiver not in (0, self.local):
            return None
        if size == 16 and kind == 4:
            self.remote = sender
            self.send(6, 1)
        if self.remote and sender != self.remote:
            return None
        if size == 16 and kind == 6:
            self.ready = True
        if size == 21 and kind == 7 and data[16] == 0:
            self.send(7, seq, b"\1" + data[17:])
        if kind == 1:
            requested = (seq,) if size == 16 else (
                entry[0] for entry in struct.iter_unpack("<H", data[16:size - size % 2])
            )
            for requested_seq in requested:
                if requested_seq in self.history:
                    with suppress(BlockingIOError):
                        self.sock.send(self.history[requested_seq])
            return None
        return kind, data


def listen(host, port, username, password, stop, on_status, on_audio,
           on_frequency=lambda _: None, on_scope=lambda *_: None, take_tune=lambda: None,
           take_commands=lambda: (), on_state=lambda *_: None):
    user = encode(username)
    secret = encode(password)
    with ExitStack() as stack:
        on_status("Finding radio")
        address = socket.gethostbyname(host)
        selector = stack.enter_context(selectors.DefaultSelector())

        def bind():
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            sock.bind(("", 0))
            return sock

        def connect(sock, remote_port):
            local_port = sock.getsockname()[1]
            sock.connect((address, remote_port))
            sock.setblocking(False)
            channel = Channel(sock, local_port)
            selector.register(sock, selectors.EVENT_READ, channel)
            return channel

        control = connect(bind(), port)
        cat_sock, audio_sock = bind(), bind()
        cat = audio = civ = None
        request_id = randbits(16)
        token = b"\0" * 4
        auth_seq = 0
        authenticated = login_sent = stream_requested = False
        identities = {}
        deadline = time.monotonic() + 6
        renewal = float("inf")
        last_pcm = None

        def request(size, kind):
            nonlocal auth_seq
            body = bytearray(size - 16)
            struct.pack_into(">IBBH", body, 0, size - 16, 1, kind, auth_seq)
            struct.pack_into("<H", body, 10, request_id)
            body[12:16] = token
            auth_seq = (auth_seq + 1) & 0xffff
            return body

        def send_token(kind):
            body = request(64, kind)
            body[20:22] = b"\x07\x98"
            control.send(0, body=body, tracked=True)

        try:
            while not stop.is_set():
                now = time.monotonic()
                if now > deadline:
                    raise RadioError("Audio stream timed out" if audio else "Radio setup timed out")
                for channel in (control, cat, audio):
                    if channel:
                        channel.tick(now)
                if civ:
                    civ.tick(now)
                if now >= renewal:
                    send_token(5)
                    renewal = now + 60
                for key, _ in selector.select(0.05):
                    channel = key.data
                    packet = channel.receive()
                    if packet is None:
                        continue
                    kind, data = packet
                    size = len(data)
                    if channel is audio:
                        if kind == 0 and size > 24 and (size - 24) % 2 == 0:
                            declared = struct.unpack_from(">H", data, 22)[0]
                            if declared != size - 24:
                                continue
                            sequence = struct.unpack_from(">H", data, 18)[0]
                            if last_pcm is not None and not 0 < (sequence - last_pcm) % 65536 < 32768:
                                continue
                            if last_pcm is None:
                                on_status("Receiving audio")
                            last_pcm = sequence
                            deadline = time.monotonic() + 6
                            on_audio(data[24:])
                        continue
                    if channel is cat:
                        if kind == 0:
                            civ.receive(data)
                        continue
                    if kind == 6 and size == 16 and not login_sent:
                        on_status("Signing in")
                        body = request(128, 0)
                        body[48:64], body[64:80], body[80:88] = user, secret, CLIENT
                        control.send(0, body=body, tracked=True)
                        login_sent = True
                    if kind != 0:
                        continue
                    if size == 96 and login_sent and not authenticated:
                        error = int.from_bytes(data[48:52], "big")
                        if error:
                            raise RadioError("Radio rejected the login")
                        if struct.unpack_from("<H", data, 26)[0] == request_id:
                            token = data[28:32]
                            authenticated = True
                            send_token(2)
                            renewal = now + 60
                    if size >= 168 and (size - 66) % 102 == 0:
                        for offset in range(66, size, 102):
                            name = data[offset + 16:offset + 48].split(b"\0", 1)[0]
                            model = name.upper().replace(b"-", b"").replace(b" ", b"")
                            if model == b"IC7300MK2":
                                identity = data[offset:offset + 16]
                                if identity[7:9] == b"\x10\x80":
                                    identity = b"\0" * 7 + identity[7:9] + b"\0" + identity[10:]
                                identities[identity] = (name, data[offset + 82])
                        if not identities:
                            raise RadioError("Expected an IC-7300MK2")
                    if authenticated and identities and not stream_requested:
                        identity, (name, civ_address) = next(iter(identities.items()))
                        body = request(144, 3)
                        body[16:32] = identity
                        body[48:48 + len(name)] = name
                        body[80:96] = user
                        # RX enabled, TX disabled, mono signed 16-bit PCM.
                        body[96:100] = b"\x01\x00\x04\x00"
                        struct.pack_into(">IIIII", body, 100, RATE, 0,
                                         cat_sock.getsockname()[1], audio_sock.getsockname()[1], 0)
                        body[120] = 1
                        control.send(0, body=body, tracked=True)
                        on_status("Opening RX stream")
                        stream_requested = True
                        deadline = now + 6
                    if size == 80 and stream_requested:
                        if data[64] == 1:
                            raise RadioError("Radio disconnected")
                        if data[48:52] != b"\0" * 4:
                            raise RadioError("Radio refused RX stream")
                        if audio is None:
                            cat_port, audio_port = struct.unpack_from(">H2xH", data, 66)
                            if cat_port and audio_port:
                                cat = connect(cat_sock, cat_port)
                                audio = connect(audio_sock, audio_port)
                                civ = Civ(cat, civ_address or 0xb6, on_frequency, on_scope, take_tune,
                                          take_commands, on_state)
        finally:
            if civ:
                with suppress(OSError):
                    civ.close()
            if authenticated:
                with suppress(OSError):
                    send_token(1)
            for channel in (audio, cat, control):
                if channel and channel.remote:
                    with suppress(OSError):
                        channel.send(5)
