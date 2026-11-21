"""Native Icom LAN audio transport, based on wfview's protocol implementation."""

from collections import OrderedDict
from contextlib import ExitStack, suppress
from secrets import randbits
import selectors
import socket
import struct
import time

from civ import Civ
from models import PROFILES, find_icom

HEADER = struct.Struct("<IHHII")
RATE = 48000
MODEL = "Network radio"
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


class ConnectionLost(RadioError):
    """A transient session failure that can be retried without user input."""


class Channel:
    def __init__(self, sock, port):
        self.sock = sock
        octets = socket.inet_aton(sock.getsockname()[0])
        self.local = int.from_bytes(octets[2:], "big") << 16 | port
        self.remote = 0
        self.ready = False
        self.seq = 0
        self.history = OrderedDict()
        self.received = OrderedDict()
        self.missing = {}
        self.rx_seq = None
        self.closed = False
        self.track_data = True
        self.next_repair = 0
        self.next_ping = self.next_idle = self.next_probe = 0

    def send(self, kind, seq=0, body=b"", tracked=False):
        if tracked:
            seq = self.seq
            self.seq = (seq + 1) & 0xFFFF
        data = HEADER.pack(16 + len(body), kind, seq, self.local, self.remote) + body
        if tracked:
            self.history[seq] = data
            if len(self.history) > 512:
                self.history.popitem(last=False)
        with suppress(BlockingIOError):
            self.sock.send(data)

    def tick(self, now):
        if self.missing and now >= self.next_repair:
            requested = []
            for seq, attempts in tuple(self.missing.items()):
                if attempts >= 4:
                    del self.missing[seq]
                else:
                    requested.append(seq)
                    self.missing[seq] += 1
            if len(requested) == 1:
                self.send(1, requested[0])
            elif requested:
                self.send(
                    1, body=b"".join(struct.pack("<HH", seq, seq) for seq in requested)
                )
            self.next_repair = now + 0.08
        if not self.ready and now >= self.next_probe:
            if self.remote:
                self.send(6, 1)
            else:
                self.send(3)
            self.next_probe = now + 0.5
        if self.remote and now >= self.next_ping:
            stamp = int(now * 1000) & 0xFFFFFFFF
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
        if size == 16 and kind == 5:
            self.closed = True
        if size == 16 and kind == 6:
            self.ready = True
        if size == 21 and kind == 7 and data[16] == 0:
            self.send(7, seq, b"\1" + data[17:])
        if kind == 1:
            requested = (
                (seq,)
                if size == 16
                else (
                    entry[0]
                    for entry in struct.iter_unpack("<H", data[16 : size - size % 2])
                )
            )
            for requested_seq in requested:
                if requested_seq in self.history:
                    with suppress(BlockingIOError):
                        self.sock.send(self.history[requested_seq])
                else:
                    self.send(0, requested_seq)
            return None
        if kind == 0 and self.track_data and not (size == 16 and seq == 0):
            # Some radio packet families reuse an outer sequence. Only identical datagrams are duplicates.
            placeholder = seq in self.received and self.received[seq] != data
            if seq in self.received and not placeholder:
                return None
            if self.rx_seq is None:
                gap = seq if seq <= 50 else 0
                self.missing.update((n, 0) for n in range(gap))
                self.rx_seq = seq
            else:
                distance = (seq - self.rx_seq) & 0xFFFF
                if 0 < distance < 32768:
                    if distance <= 50:
                        for step in range(1, distance):
                            self.missing.setdefault((self.rx_seq + step) & 0xFFFF, 0)
                    else:
                        self.missing.clear()
                    self.rx_seq = seq
                elif seq not in self.missing and not placeholder:
                    return None
            self.missing.pop(seq, None)
            self.received[seq] = data
            if len(self.received) > 512:
                self.received.popitem(last=False)
        return kind, data

    def drain(self):
        # A bounded batch prevents audio/scope bursts from starving keepalives.
        for _ in range(64):
            try:
                packet = self.receive()
            except BlockingIOError:
                return
            if packet is not None:
                yield packet


def listen(
    host,
    port,
    username,
    password,
    stop,
    on_status,
    on_audio,
    on_frequency=lambda _: None,
    on_scope=lambda *_: None,
    take_tune=lambda: None,
    take_commands=lambda: (),
    on_state=lambda *_: None,
    model="Auto (Icom)",
    audio_port=60001,
    admin=False,
    tx_enabled=False,
    take_tx=lambda: None,
):
    if model == "TS-890S":
        from kenwood import listen as listen_kenwood

        return listen_kenwood(
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
            tx_enabled,
            take_tx,
        )
    if model != "Auto (Icom)" and model not in PROFILES:
        raise RadioError("Choose a supported network radio")
    user = encode(username)
    secret = encode(password)
    with ExitStack() as stack:
        on_status("Finding radio")
        address = socket.gethostbyname(host)
        selector = stack.enter_context(selectors.DefaultSelector())

        def bind():
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.bind(("", 0))
            return sock

        def connect(sock, remote_port, track_data=True):
            local_port = sock.getsockname()[1]
            sock.connect((address, remote_port))
            sock.setblocking(False)
            channel = Channel(sock, local_port)
            channel.track_data = track_data
            selector.register(sock, selectors.EVENT_READ, channel)
            return channel

        control = connect(bind(), port)
        cat_sock, audio_sock = bind(), bind()
        cat = audio = civ = pacer = None
        audio_tx_seq = 0
        request_id = randbits(16)
        token = b"\0" * 4
        auth_seq = 0
        authenticated = login_sent = stream_requested = False
        identities = {}
        deadline = time.monotonic() + 10
        renewal = float("inf")
        last_pcm = None
        phase_packet = None
        phase_retry = 0
        phase_attempts = 0

        def request(size, kind):
            nonlocal auth_seq
            body = bytearray(size - 16)
            struct.pack_into(">IBBH", body, 0, size - 16, 1, kind, auth_seq)
            struct.pack_into("<H", body, 10, request_id)
            body[12:16] = token
            auth_seq = (auth_seq + 1) & 0xFFFF
            return body

        def send_token(kind):
            body = request(64, kind)
            body[20:22] = b"\x07\x98"
            control.send(0, body=body, tracked=True)

        try:
            while not stop.is_set():
                now = time.monotonic()
                if now > deadline:
                    raise ConnectionLost(
                        "Audio stream timed out" if audio else "Radio setup timed out"
                    )
                for channel in (control, cat, audio):
                    if channel:
                        channel.tick(now)
                if (
                    phase_packet is not None
                    and now >= phase_retry
                    and phase_attempts < 2
                ):
                    # Repeat the same transaction; if the peer cannot replay it, reopen the session.
                    with suppress(BlockingIOError):
                        control.sock.send(phase_packet)
                    phase_attempts += 1
                    phase_retry = now + 0.8
                if pacer:
                    pacer.tick()
                    if pacer.armed or (
                        civ and (civ.tuning_requested or civ.tuner_state == 2)
                    ):
                        deadline = now + 10
                if civ:
                    civ.tick(now)
                    if now - civ.created > 5 and (
                        civ.frequency is None or civ.mode is None
                    ):
                        raise ConnectionLost("Radio control did not become ready")
                    if civ.last_control is not None and now - civ.last_control > 5:
                        raise ConnectionLost("Radio control timed out")
                if now >= renewal:
                    send_token(5)
                    renewal = now + 60
                for key, _ in selector.select(0.01):
                    channel = key.data
                    for kind, data in channel.drain():
                        size = len(data)
                        if channel is audio:
                            if kind == 0 and size > 24 and (size - 24) % 2 == 0:
                                declared = struct.unpack_from(">H", data, 22)[0]
                                if declared != size - 24:
                                    continue
                                sequence = struct.unpack_from(">H", data, 18)[0]
                                if (
                                    last_pcm is not None
                                    and not 0 < (sequence - last_pcm) % 65536 < 32768
                                ):
                                    continue
                                if last_pcm is None:
                                    on_status("Receiving audio")
                                last_pcm = sequence
                                deadline = time.monotonic() + 10
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
                            phase_packet = control.history[(control.seq - 1) & 0xFFFF]
                            phase_retry = now + 0.8
                            phase_attempts = 0
                            deadline = now + 3
                        if size == 16 and kind == 5:
                            raise ConnectionLost("Radio disconnected")
                        if kind != 0:
                            continue
                        if size == 96 and login_sent and not authenticated:
                            if struct.unpack_from("<H", data, 26)[0] != request_id:
                                continue
                            error = int.from_bytes(data[48:52], "big")
                            if error:
                                raise RadioError("Radio rejected the login")
                            if struct.unpack_from("<H", data, 26)[0] == request_id:
                                token = data[28:32]
                                authenticated = True
                                phase_packet = None
                                send_token(2)
                                renewal = now + 60
                        if size >= 168 and (size - 66) % 102 == 0:
                            for offset in range(66, size, 102):
                                name = data[offset + 16 : offset + 48].split(b"\0", 1)[
                                    0
                                ]
                                radio_address = data[offset + 82]
                                profile = find_icom(
                                    name.decode("ascii", "replace"),
                                    radio_address,
                                    model,
                                )
                                if profile:
                                    identity = data[offset : offset + 16]
                                    if identity[7:9] == b"\x10\x80":
                                        identity = (
                                            b"\0" * 7
                                            + identity[7:9]
                                            + b"\0"
                                            + identity[10:]
                                        )
                                    identities[identity] = (
                                        name,
                                        radio_address,
                                        profile,
                                    )
                            if not identities:
                                raise RadioError(
                                    "Radio model not recognized; choose it in Setup"
                                )
                            if len(identities) != 1:
                                raise RadioError(
                                    "More than one radio found; choose a model in Setup"
                                )
                        if authenticated and identities and not stream_requested:
                            identity, (name, civ_address, profile) = next(
                                iter(identities.items())
                            )
                            on_state("model", profile["model"])
                            on_state("profile", profile)
                            body = request(144, 3)
                            body[16:32] = identity
                            body[48 : 48 + len(name)] = name
                            body[80:96] = user
                            # Mono signed 16-bit PCM; TX is negotiated only for FT8-capable transmitters.
                            can_tx = (
                                tx_enabled
                                and profile["transmit"]
                                and "tx_source" in profile
                            )
                            body[96:100] = bytes(
                                (1, int(can_tx), 4, 4 if can_tx else 0)
                            )
                            struct.pack_into(
                                ">IIIII",
                                body,
                                100,
                                RATE,
                                RATE if can_tx else 0,
                                cat_sock.getsockname()[1],
                                audio_sock.getsockname()[1],
                                150 if can_tx else 0,
                            )
                            body[120] = 1
                            control.send(0, body=body, tracked=True)
                            on_status("Opening RX stream")
                            stream_requested = True
                            phase_packet = control.history[(control.seq - 1) & 0xFFFF]
                            phase_retry = now + 0.8
                            phase_attempts = 0
                            deadline = now + 3
                        if (
                            size == 64
                            and authenticated
                            and data[20] == 2
                            and data[21] == 5
                        ):
                            if data[48:52] != b"\0" * 4:
                                raise ConnectionLost("Radio session expired")
                        if size == 80 and stream_requested:
                            if data[64] == 1:
                                raise ConnectionLost("Radio disconnected")
                            if data[48:52] != b"\0" * 4:
                                raise ConnectionLost("Radio refused RX stream")
                            if audio is None:
                                cat_port, audio_port = struct.unpack_from(
                                    ">H2xH", data, 66
                                )
                                if cat_port and audio_port:
                                    cat = connect(cat_sock, cat_port)
                                    audio = connect(
                                        audio_sock, audio_port, track_data=False
                                    )
                                    phase_packet = None
                                    civ = Civ(
                                        cat,
                                        civ_address or profile["civ"],
                                        on_frequency,
                                        on_scope,
                                        take_tune,
                                        take_commands,
                                        on_state,
                                        profile,
                                        tx_enabled,
                                    )
                                    if civ.tx_enabled:
                                        from ft8tx import Pacer

                                        def send_pcm(pcm):
                                            nonlocal audio_tx_seq
                                            body = (
                                                struct.pack("<H", 0x80)
                                                + struct.pack(">H", audio_tx_seq)
                                                + b"\0\0"
                                                + struct.pack(">H", len(pcm))
                                                + pcm
                                            )
                                            audio.send(0, body=body, tracked=True)
                                            while len(audio.history) > 20:
                                                audio.history.popitem(last=False)
                                            audio_tx_seq = (audio_tx_seq + 1) & 65535

                                        pacer = Pacer(
                                            take_tx,
                                            civ.ptt,
                                            send_pcm,
                                            civ.tx_ready,
                                            lambda: civ.ptt_state,
                                            lambda value: on_state("tx_status", value),
                                            protection=civ.protection.check,
                                        )
        finally:
            if pacer:
                with suppress(OSError):
                    pacer.close()
            if civ:
                with suppress(OSError):
                    civ.close()
            if authenticated:
                with suppress(OSError):
                    send_token(1)
            channels = [
                channel
                for channel in (audio, cat, control)
                if channel and channel.remote
            ]
            until = time.monotonic() + 0.7
            next_bye = 0
            while channels and time.monotonic() < until:
                now = time.monotonic()
                if now >= next_bye:
                    for channel in channels:
                        with suppress(OSError):
                            channel.send(5)
                    next_bye = now + 0.2
                for key, _ in selector.select(0.02):
                    with suppress(OSError):
                        for _ in key.data.drain():
                            pass
                if all(channel.closed for channel in channels):
                    break
