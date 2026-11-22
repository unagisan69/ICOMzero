"""Native Icom LAN audio transport, based on wfview's protocol implementation."""

from collections import OrderedDict
from contextlib import ExitStack, suppress
from secrets import randbits
import selectors
import socket
import json
import os
from pathlib import Path
import struct
import time
import sys

from civ import Civ
from models import PROFILES, find_icom

HEADER = struct.Struct("<IHHII")
RATE = 48000
FT8_RATE = 12000
FT8_TX_BUFFER_MS = 300
STREAM_RECOVERY_SECONDS = 90
STREAM_RETRY_SECONDS = (2, 4, 8)
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


def connection_diagnostic(reason, phase):
    """Bounded private setup errors, excluding credentials and packet payloads."""
    try:
        root = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "icomzero"
        )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "connection-diagnostics.jsonl"
        if path.exists() and path.stat().st_size > 131072:
            path.replace(root / "connection-diagnostics.previous.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(
                json.dumps(
                    {
                        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "phase": phase,
                        "error": str(reason),
                    }
                )
                + "\n"
            )
    except OSError:
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


def repair_sequences(data):
    """Icom multi-packet repair payloads contain inclusive start/end pairs."""
    if len(data) == HEADER.size:
        return (HEADER.unpack_from(data)[2],)
    requested = {}
    for offset in range(HEADER.size, len(data) - 3, 4):
        first, last = struct.unpack_from("<HH", data, offset)
        count = ((last - first) & 0xFFFF) + 1
        # A session retains at most 512 packets. Reject malformed/huge ranges
        # rather than turning a small request into an unbounded packet burst.
        if count > 512 or len(requested) + count > 512:
            continue
        for step in range(count):
            requested[(first + step) & 0xFFFF] = None
    return tuple(requested)


class Channel:
    def __init__(self, sock, port):
        self.sock = sock
        octets = socket.inet_aton(sock.getsockname()[0])
        self.local = int.from_bytes(octets[2:], "big") << 16 | port
        self.remote = 0
        self.ready = False
        # Sequence zero belongs to discovery; both reference clients start at one.
        self.seq = 1
        self.history = OrderedDict()
        self.received = OrderedDict()
        self.missing = {}
        self.rx_seq = None
        self.closed = False
        self.last_received = None
        self.track_data = True
        self.idle_enabled = True
        self.ping_seq = 0
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
            # An idle means no tracked payload has been sent for 100 ms.
            self.next_idle = time.monotonic() + 0.1
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
            self.send(7, self.ping_seq, body=struct.pack("<BI", 0, stamp))
            self.next_ping = now + 0.5
        if self.idle_enabled and self.remote and now >= self.next_idle:
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
        self.last_received = time.monotonic()
        if size == 16 and kind == 5:
            self.closed = True
        if size == 16 and kind == 6:
            self.ready = True
        if size == 21 and kind == 7:
            if data[16] == 0:
                self.send(7, seq, b"\1" + data[17:])
            elif data[16] == 1 and seq == self.ping_seq:
                self.ping_seq = (self.ping_seq + 1) & 0xFFFF
        if kind == 1:
            requested = repair_sequences(data)
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

    def drain(self, budget=None):
        # Bound both count and CPU time so bursts cannot starve TX or meters.
        started = time.monotonic()
        for _ in range(64):
            try:
                packet = self.receive()
            except BlockingIOError:
                return
            if packet is not None:
                yield packet
            if budget is not None and time.monotonic() - started >= budget:
                break


def route_address(address, port):
    """Ask the kernel for the current source address; UDP connect sends no data."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((address, port))
        return probe.getsockname()[0]


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
    on_audio_gap=lambda: None,
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
            on_audio_gap,
        )
    if model != "Auto (Icom)" and model not in PROFILES:
        raise RadioError("Choose a supported network radio")
    stream_rate = FT8_RATE if tx_enabled else RATE
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

        def connect(sock, remote_port, track_data=True, idle_enabled=True):
            local_port = sock.getsockname()[1]
            sock.connect((address, remote_port))
            sock.setblocking(False)
            channel = Channel(sock, local_port)
            channel.track_data = track_data
            channel.idle_enabled = idle_enabled
            selector.register(sock, selectors.EVENT_READ, channel)
            return channel

        control = connect(bind(), port)
        local_address = control.sock.getsockname()[0]
        network_check_at = time.monotonic() + 0.5
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
        stream_attempts = 0
        stream_retry_at = 0
        stream_wait_until = None

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

        telemetry_at = 0
        loop_at = time.monotonic()
        try:
            while not stop.is_set():
                now = time.monotonic()
                gap = now - loop_at
                loop_at = now
                if gap > 0.08:
                    on_state("transport_gap_ms", round(gap * 1000, 1))
                    on_state("transport_gap_at", now)
                if now >= telemetry_at:
                    for name, channel in (
                        ("control", control),
                        ("cat", cat),
                        ("audio", audio),
                    ):
                        if channel is not None:
                            on_state(name + "_rx_at", channel.last_received)
                    if civ is not None:
                        on_state("civ_reply_at", civ.last_control)
                    telemetry_at = now + 0.25
                if now >= network_check_at:
                    if route_address(address, port) != local_address:
                        raise ConnectionLost(
                            "Local network address changed; reconnecting"
                        )
                    network_check_at = now + 0.5
                if now > deadline:
                    raise ConnectionLost(
                        "Audio stream timed out"
                        if last_pcm is not None
                        else (
                            "Audio channel handshake timed out"
                            if audio
                            else (
                                "RX stream setup timed out"
                                if stream_attempts
                                else "Radio login/setup timed out"
                            )
                        )
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
                    if (civ.started_at is None and now - civ.created > 8) or (
                        civ.started_at is not None
                        and now - civ.started_at > 5
                        and (civ.frequency is None or civ.mode is None)
                    ):
                        raise ConnectionLost("Radio control did not become ready")
                    if civ.last_control is not None and now - civ.last_control > 5:
                        raise ConnectionLost("Radio control timed out")
                if now >= renewal:
                    send_token(5)
                    renewal = now + 60
                if (
                    authenticated
                    and identities
                    and not stream_requested
                    and now >= stream_retry_at
                ):
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
                        tx_enabled and profile["transmit"] and "tx_source" in profile
                    )
                    body[96:100] = bytes((1, int(can_tx), 4, 4 if can_tx else 0))
                    struct.pack_into(
                        ">IIIII",
                        body,
                        100,
                        stream_rate,
                        stream_rate if can_tx else 0,
                        cat_sock.getsockname()[1],
                        audio_sock.getsockname()[1],
                        FT8_TX_BUFFER_MS if can_tx else 0,
                    )
                    body[120] = 1
                    control.send(0, body=body, tracked=True)
                    on_status("Opening RX stream")
                    stream_requested = True
                    stream_attempts += 1
                    if stream_wait_until is None:
                        stream_wait_until = now + STREAM_RECOVERY_SECONDS
                    phase_packet = control.history[(control.seq - 1) & 0xFFFF]
                    phase_retry = now + 0.8
                    phase_attempts = 0
                    deadline = now + 8
                for key, _ in selector.select(0.01):
                    channel = key.data
                    for kind, data in channel.drain(budget=0.003):
                        size = len(data)
                        if size == 16 and kind == 5:
                            name = (
                                "audio"
                                if channel is audio
                                else ("CAT" if channel is cat else "control")
                            )
                            raise ConnectionLost(f"Radio closed {name} channel")
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
                                if (
                                    last_pcm is not None
                                    and (sequence - last_pcm) % 65536 != 1
                                ):
                                    on_audio_gap()
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
                            deadline = now + 8
                        if size == 16 and kind == 5:
                            raise ConnectionLost("Radio disconnected")
                        if kind != 0:
                            continue
                        if (
                            size == 64
                            and authenticated
                            and data[20] == 2
                            and data[21] == 5
                        ):
                            if data[48:52] != b"\0" * 4:
                                raise ConnectionLost("Radio session expired")
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
                        if size == 80 and stream_requested:
                            error = int.from_bytes(data[48:52], "big")
                            ports = struct.unpack_from(">H2xH", data, 66)
                            if data[64] == 1 or error or not all(ports):
                                if audio is not None:
                                    raise ConnectionLost(
                                        "Radio disconnected the stream"
                                    )
                                if now >= stream_wait_until:
                                    raise ConnectionLost(
                                        f"Radio refused stream (0x{error:08x}, ports {ports[0]}/{ports[1]})"
                                    )
                                # An earlier session can take time to release. Use
                                # a fresh stream transaction, not a replay of the
                                # already-refused packet or a new login storm.
                                stream_requested = False
                                stream_retry_at = (
                                    now
                                    + STREAM_RETRY_SECONDS[
                                        min(
                                            stream_attempts - 1,
                                            len(STREAM_RETRY_SECONDS) - 1,
                                        )
                                    ]
                                )
                                phase_packet = None
                                deadline = min(stream_wait_until, stream_retry_at + 8)
                                on_status("Radio busy; waiting to open stream")
                                continue
                            if audio is None:
                                cat_port, audio_port = struct.unpack_from(
                                    ">H2xH", data, 66
                                )
                                if cat_port and audio_port:
                                    cat = connect(cat_sock, cat_port)
                                    audio = connect(
                                        audio_sock,
                                        audio_port,
                                        track_data=False,
                                        idle_enabled=False,
                                    )
                                    phase_packet = None
                                    deadline = now + 8
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
                                            rate=stream_rate,
                                            packet_ms=20,
                                            drain_seconds=(FT8_TX_BUFFER_MS + 50)
                                            / 1000,
                                            protection=civ.protection.check,
                                            off_confirmed=lambda: not civ.ptt_state
                                            and not civ.ptt_pending,
                                        )
        finally:
            ending = sys.exc_info()[1]
            if ending is not None:
                detail = (
                    str(ending)
                    if isinstance(ending, (RadioError, OSError))
                    else type(ending).__name__
                )
                reason = "Radio connection lost: " + detail
                on_state("connection_error", reason)
            else:
                reason = (
                    "Transmit stopped" if stop.is_set() else "Radio connection ended"
                )
            if pacer:
                with suppress(OSError):
                    pacer.close(reason)
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
