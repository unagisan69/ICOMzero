"""Regressions for packet loss, delayed tuning replies and UI feedback."""
from collections import deque
from pathlib import Path
import struct
import os
import runpy
from unittest.mock import patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from civ import Civ, encode_frequency
from radio import Channel, HEADER


class Socket:
    def __init__(self):
        self.incoming = deque()
        self.sent = []

    def getsockname(self):
        return "192.0.2.10", 40000

    def send(self, data):
        self.sent.append(data)

    def recv(self, _):
        if not self.incoming:
            raise BlockingIOError
        return self.incoming.popleft()


class PacketRecovery(unittest.TestCase):
    def setUp(self):
        self.socket = Socket()
        self.channel = Channel(self.socket, 40000)
        self.channel.remote = 7

    def packet(self, seq, payload=b"payload"):
        self.socket.incoming.append(HEADER.pack(16 + len(payload), 0, seq, 7, self.channel.local) + payload)

    def test_lost_first_login_reply_is_requested_and_delivered_once(self):
        self.packet(1)
        self.channel.receive()
        self.channel.tick(1)
        requests = [HEADER.unpack_from(p) for p in self.socket.sent if HEADER.unpack_from(p)[1] == 1]
        self.assertEqual(requests[0][2], 0)
        self.packet(0, b"login")
        self.assertEqual(self.channel.receive()[1][16:], b"login")
        self.packet(0, b"login")
        self.assertIsNone(self.channel.receive())
        self.assertFalse(self.channel.missing)

    def test_untracked_zero_idle_cannot_hide_login_sequence_zero(self):
        self.packet(0, b"")
        self.channel.receive()
        self.packet(0, b"login")
        self.assertEqual(self.channel.receive()[1][16:], b"login")

    def test_missing_packets_are_repaired_across_sequence_wrap(self):
        self.packet(65534)
        self.channel.receive()
        self.packet(1)
        self.channel.receive()
        self.assertEqual(set(self.channel.missing), {65535, 0})
        self.channel.tick(1)
        request = next(p for p in self.socket.sent if HEADER.unpack_from(p)[1] == 1)
        self.assertEqual(set(v[0] for v in struct.iter_unpack("<H", request[16:])), {65535, 0})
        for seq in (65535, 0):
            self.packet(seq)
            self.assertIsNotNone(self.channel.receive())
        self.assertFalse(self.channel.missing)

    def test_placeholder_idle_cannot_hide_a_delayed_control_response(self):
        self.packet(1, b"")
        self.channel.receive()
        self.packet(1, b"login")
        self.assertEqual(self.channel.receive()[1][16:], b"login")

    def test_audio_uses_its_own_sequence_after_outer_sequence_restarts(self):
        self.channel.track_data = False
        self.packet(55, b"audio0")
        self.packet(0, b"audio1")
        self.packet(1, b"audio2")
        self.assertEqual([p[1][16:] for p in self.channel.drain()], [b"audio0", b"audio1", b"audio2"])
        self.assertFalse(self.channel.missing)

    def test_burst_is_drained_and_missing_history_gets_idle_reply(self):
        for seq in range(40):
            self.packet(seq)
        self.assertEqual(len(list(self.channel.drain())), 40)
        self.socket.incoming.append(HEADER.pack(16, 1, 999, 7, self.channel.local))
        self.assertIsNone(self.channel.receive())
        self.assertEqual(HEADER.unpack_from(self.socket.sent[-1])[1:3], (0, 999))


class Tuning(unittest.TestCase):
    def test_tuning_preempts_polls_retries_loss_and_coalesces_new_target(self):
        class Stream:
            ready = True
            def __init__(self):
                self.sent = []
            def send(self, *_, **kwargs):
                self.sent.append(kwargs["body"][9:-1])
        stream = Stream()
        targets = deque((7_091_000,))
        civ = Civ(stream, 0xb6, lambda _: None, lambda *_: None,
                  lambda: targets.popleft() if targets else None)
        civ.open = True
        civ.tick(1)
        self.assertEqual(stream.sent[-1], b"\x05" + encode_frequency(7_091_000))
        civ.tick(1.01)
        self.assertEqual(len(stream.sent), 1)
        civ.tick(1.03)
        self.assertEqual(stream.sent[-1], b"\x03")
        targets.append(7_093_000)
        civ.tick(1.06)
        self.assertEqual(stream.sent[-1], b"\x05" + encode_frequency(7_093_000))
        civ.tick(1.09)
        civ.tick(1.27)
        self.assertEqual(stream.sent[-1], b"\x05" + encode_frequency(7_093_000))
        frame = bytes.fromhex("fefee0b603") + encode_frequency(7_093_000) + b"\xfd"
        packet = bytes(16) + b"\xc1" + struct.pack("<H", len(frame)) + bytes(2) + frame
        civ.receive(packet)
        self.assertIsNone(civ.tune_target)


class InterfaceRecovery(unittest.TestCase):
    def test_requested_frequency_stays_visible_until_radio_acknowledges(self):
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "app/icomzero"))
        receiver = module["Receiver"]()
        receiver.thread = type("Running", (), {"is_alive": lambda self: True})()
        receiver.frequency = 7_090_000
        receiver.tune(steps=1)
        self.assertEqual(receiver.displayed_frequency(), 7_091_000)
        receiver.frequency_changed(7_090_000)
        self.assertEqual(receiver.displayed_frequency(), 7_091_000)
        receiver.frequency_changed(7_091_000)
        self.assertIsNone(receiver.target)

    def test_transient_session_failure_reconnects_on_one_start(self):
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "app/icomzero"))
        attempts, statuses, captured = [], [], []
        read, write = os.pipe()
        class Player:
            def __init__(self, *_, **__):
                self.stdin = os.fdopen(write, "wb", buffering=0)
            def poll(self):
                return None
            def wait(self, timeout=None):
                return 0
        def listen(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise module["ConnectionLost"]("lost handshake")
            args[6](bytes((1, 2)) * 100)
            args[4].set()
        try:
            with patch.dict(module["Receiver"].run.__globals__, {"listen": listen}), patch("subprocess.Popen", Player):
                receiver = module["Receiver"](statuses.append)
                receiver.muted = True
                receiver.audio_observer = captured.append
                receiver.start(("192.0.2.1", 50001, "user", "password"))
                receiver.thread.join(2)
            self.assertFalse(receiver.busy())
            self.assertEqual(len(attempts), 2)
            self.assertIn("Reconnecting (1/3)", statuses)
            self.assertEqual(receiver.written, 200)
            self.assertEqual(captured, [bytes((1, 2)) * 100])
            self.assertEqual(os.read(read, 200), bytes(200))
            self.assertEqual(receiver.status, "Disconnected")
        finally:
            os.close(read)


class ConnectionFailureDetails(unittest.TestCase):
    def test_final_error_preserves_reason_and_private_diagnostic(self):
        import tempfile
        import json

        module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )

        def unavailable(*args, **kwargs):
            raise module["ConnectionLost"]("Audio channel handshake timed out")

        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": root}
        ), patch.dict(module["Receiver"].run.__globals__, {"listen": unavailable}):
            receiver = module["Receiver"]()
            receiver.stop.wait = lambda _: False
            receiver.run(("192.0.2.1", 50001, "alice", "secret"))
            self.assertEqual(
                receiver.status, "Connection failed: Audio channel handshake timed out"
            )
            path = Path(root) / "icomzero/connection-diagnostics.jsonl"
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(records), 4)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("secret", path.read_text())


if __name__ == "__main__":
    unittest.main()
