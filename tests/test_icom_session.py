"""Real UDP setup regressions, using an in-process radio with no RF hardware."""

import selectors
import socket
import struct
import threading
import time
import unittest
from radio import HEADER, listen
from civ import encode_frequency


class StreamNegotiation(unittest.TestCase):
    def run_radio(
        self,
        refusal,
        audio_delay=0,
        tx_enabled=False,
        recover_civ=False,
        repair_range=False,
    ):
        stop = threading.Event()
        errors, statuses, frames, state, requests = [], [], [], {}, []
        sockets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(3)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
        control, cat, audio = sockets
        seqs = {s: 0 for s in sockets}
        clients = {}
        ready = set()
        ready_after = 0
        audio_at = 0
        pcm_seq = 0
        audio_idles = []
        suspended = reopened = False
        open_count = 0
        inject_range = repair_complete = False
        lost_packets = {}
        repaired = set()

        def reply(sock, address, local, kind, body=b""):
            seq = seqs[sock]
            seqs[sock] += int(kind == 0)
            sock.sendto(
                HEADER.pack(16 + len(body), kind, seq, 7, local) + body, address
            )

        def server():
            nonlocal audio_at, pcm_seq, ready_after, suspended, reopened, open_count, inject_range, repair_complete
            try:
                with selectors.DefaultSelector() as selector:
                    for sock in sockets:
                        selector.register(sock, selectors.EVENT_READ)
                    while not stop.is_set():
                        for key, _ in selector.select(0.005):
                            sock = key.fileobj
                            data, addr = sock.recvfrom(65535)
                            size, kind, seq, local, remote = HEADER.unpack_from(data)
                            clients[sock] = (addr, local)
                            if sock is cat and kind == 0 and inject_range:
                                if seq in lost_packets and data == lost_packets[seq]:
                                    repaired.add(seq)
                                    if len(repaired) == 5:
                                        repair_complete = True
                                        inject_range = False
                                elif len(lost_packets) < 5:
                                    lost_packets[seq] = data
                                    if len(lost_packets) == 5:
                                        first, last = min(lost_packets), max(
                                            lost_packets
                                        )
                                        self.assertEqual(last - first, 4)
                                        reply(
                                            sock,
                                            addr,
                                            local,
                                            1,
                                            struct.pack("<HH", first, last),
                                        )
                                continue
                            if kind == 3:
                                reply(sock, addr, local, 4)
                            elif kind == 6:
                                reply(sock, addr, local, 6)
                                ready.add(sock)
                                if sock is audio:
                                    ready_after = time.monotonic() + audio_delay
                            elif kind == 5:
                                reply(sock, addr, local, 5)
                            elif sock is audio and kind == 0 and size == 16:
                                audio_idles.append(seq)
                            elif sock is control and kind == 0 and size == 128:
                                body = bytearray(80)
                                body[10:12] = data[26:28]
                                body[12:16] = b"abcd"
                                reply(sock, addr, local, 0, body)
                            elif (
                                sock is control
                                and kind == 0
                                and size == 64
                                and data[21] == 2
                            ):
                                body = bytearray(152)
                                body[50:66] = bytes(range(16))
                                name = b"IC-7300MK2"
                                body[66 : 66 + len(name)] = name
                                body[132] = 0xB6
                                reply(sock, addr, local, 0, body)
                            elif sock is control and kind == 0 and size == 144:
                                requests.append(data[22:24])
                                self.assertEqual(
                                    struct.unpack_from(">I", data, 132)[0],
                                    300 if tx_enabled else 0,
                                )
                                rates = struct.unpack_from(">II", data, 116)
                                expected = 12000 if tx_enabled else 48000
                                self.assertEqual(
                                    rates, (expected, expected if tx_enabled else 0)
                                )
                                body = bytearray(64)
                                if len(requests) == 1:
                                    body[32:36] = refusal.to_bytes(4, "big")
                                else:
                                    struct.pack_into(
                                        ">H2xH",
                                        body,
                                        50,
                                        cat.getsockname()[1],
                                        audio.getsockname()[1],
                                    )
                                reply(sock, addr, local, 0, body)
                            elif (
                                sock is cat
                                and kind == 0
                                and size == 22
                                and data[16] == 0xC0
                            ):
                                open_count += 1
                                if suspended and open_count > 1:
                                    suspended = False
                                    reopened = True
                            elif (
                                sock is cat
                                and kind == 0
                                and size > 26
                                and data[16] == 0xC1
                            ):
                                if suspended:
                                    continue
                                cmd = data[25:-1]
                                payload = (
                                    b"\x03" + encode_frequency(7074000)
                                    if cmd == b"\x03"
                                    else b"\x04\x01\x01" if cmd == b"\x04" else b"\xfb"
                                )
                                frame = b"\xfe\xfe\xe0\xb6" + payload + b"\xfd"
                                reply(
                                    sock,
                                    addr,
                                    local,
                                    0,
                                    b"\xc1"
                                    + struct.pack("<H", len(frame))
                                    + bytes(2)
                                    + frame,
                                )
                        if (
                            audio in ready
                            and len(requests) > 1
                            and time.monotonic() > max(audio_at, ready_after)
                        ):
                            addr, local = clients[audio]
                            pcm = bytes(480 if tx_enabled else 1920)
                            body = (
                                b"\x80\x00"
                                + struct.pack(">H", pcm_seq)
                                + bytes(2)
                                + struct.pack(">H", len(pcm))
                                + pcm
                            )
                            reply(audio, addr, local, 0, body)
                            pcm_seq += 1
                            audio_at = time.monotonic() + 0.02
            except Exception as error:
                errors.append(error)
                stop.set()

        worker = threading.Thread(target=server, daemon=True)
        worker.start()

        def sample(pcm):
            nonlocal suspended, inject_range
            frames.append(pcm)
            if state.get("frequency") and "mode" in state:
                if repair_range:
                    if repair_complete:
                        stop.set()
                    elif not lost_packets:
                        inject_range = True
                elif recover_civ:
                    if reopened and state.get("civ_recovered_at") is not None:
                        stop.set()
                    elif not reopened:
                        suspended = True
                else:
                    stop.set()

        try:
            listen(
                "127.0.0.1",
                control.getsockname()[1],
                "user",
                "password",
                stop,
                statuses.append,
                sample,
                lambda v: state.update(frequency=v),
                on_state=state.__setitem__,
                tx_enabled=tx_enabled,
            )
        finally:
            stop.set()
            worker.join(2)
            for sock in sockets:
                sock.close()
        self.assertFalse(errors, errors)
        self.assertTrue(frames)
        self.assertEqual(
            audio_idles, [], "Audio transport injected tracked empty packets"
        )
        self.assertEqual(state.get("frequency"), 7074000)
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0], requests[1])
        self.assertIn("Radio busy; waiting to open stream", statuses)
        if repair_range:
            self.assertTrue(repair_complete)
            self.assertEqual(len(repaired), 5)
        if recover_civ:
            self.assertGreaterEqual(open_count, 2)
            self.assertIsNotNone(state.get("civ_recovered_at"))
            self.assertFalse(state["civ_recovering"])

    def test_zero_port_busy_reply_retries_fresh_transaction(self):
        self.run_radio(0)

    def test_explicit_refusal_can_recover_without_relogin(self):
        self.run_radio(0xFFFFFFFF)

    def test_first_audio_may_arrive_after_old_three_second_limit(self):
        self.run_radio(0, audio_delay=3.2)

    def test_ft8_negotiates_native_twelve_khz_both_directions(self):
        self.run_radio(0, tx_enabled=True)

    def test_stalled_civ_recovers_without_relogin_or_reopening_audio(self):
        self.run_radio(0, tx_enabled=True, recover_civ=True)

    def test_compact_five_packet_range_is_fully_repaired(self):
        self.run_radio(0, tx_enabled=True, repair_range=True)
