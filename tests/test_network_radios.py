import contextlib
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from civ import Civ, Scope, decode_frequency, encode_frequency
from kenwood import Cat, Pcm, rtp_payload
from models import PROFILES, find_icom, presets
from radio import listen


class Profiles(unittest.TestCase):
    def test_native_catalog_is_limited_to_network_radios(self):
        self.assertEqual(set(PROFILES), {
            "IC-705", "IC-7300MK2", "IC-7610", "IC-7760", "IC-785x",
            "IC-905", "IC-9700", "IC-R8600", "TS-890S",
        })
        self.assertEqual(find_icom("IC-7851", 0x8e)["model"], "IC-785x")
        self.assertIsNone(find_icom("IC-7300", 0x94))
        self.assertFalse(PROFILES["IC-R8600"]["transmit"])
        self.assertFalse(PROFILES["IC-9700"]["tuner"])

    def test_band_presets_follow_radio_frequency_ranges(self):
        self.assertIn(("2m", 144_200_000), presets(PROFILES["IC-9700"]))
        self.assertNotIn(("20m", 14_200_000), presets(PROFILES["IC-9700"]))
        self.assertIn(("3cm", 10_368_200_000), presets(PROFILES["IC-905"]))
        self.assertNotIn(("3cm", 10_368_200_000), presets(PROFILES["IC-7300MK2"]))

    def test_ten_ghz_frequency_uses_six_bcd_bytes(self):
        self.assertEqual(encode_frequency(10_368_200_000), bytes.fromhex("000020680301"))
        self.assertEqual(decode_frequency(bytes.fromhex("000020680301")), 10_368_200_000)
        self.assertIsNone(decode_frequency(b"\xfa"))

    def test_combined_and_split_scope_frames_for_all_icom_models(self):
        for name, profile in PROFILES.items():
            if profile["manufacturer"] != 0:
                continue
            with self.subTest(model=name):
                width = profile["scope_bins"]
                count = profile["scope_parts"]
                pixels = bytes(index % 140 for index in range(width))
                info = b"\x00" + encode_frequency(14_200_000) + encode_frequency(100_000) + b"\0"
                result = Scope(profile).parse(b"\x01\x01" + info + pixels)
                self.assertEqual(result, (14_100_000, 14_300_000, pixels))
                scope = Scope(profile)
                bcd = lambda value: ((value // 10) << 4) | value % 10
                self.assertIsNone(scope.parse(bytes((1, bcd(count))) + info))
                for part in range(2, count + 1):
                    start = (part - 2) * 50
                    result = scope.parse(bytes((bcd(part), bcd(count))) + pixels[start:start + 50])
                self.assertEqual(result, (14_100_000, 14_300_000, pixels))

    def test_scope_drops_missing_and_out_of_range_sections(self):
        scope = Scope(PROFILES["IC-7610"])
        info = b"\x01\x15\x00" + encode_frequency(14_200_000) + encode_frequency(100_000) + b"\0"
        scope.parse(info)
        self.assertIsNone(scope.parse(b"\x03\x15" + bytes(50)))
        self.assertIsNone(scope.parse(b"\x15\x15" + bytes(39)))
        self.assertIsNone(scope.parse(info[:-1] + b"\1"))

    def test_905_six_byte_scope_range(self):
        scope = Scope(PROFILES["IC-905"])
        pixels = bytes(475)
        info = b"\x01\x01\x00" + bytes.fromhex("000020680301") + bytes.fromhex("000010000000") + b"\0"
        self.assertEqual(scope.parse(info + pixels), (10_368_100_000, 10_368_300_000, pixels))

    def test_receive_only_radios_never_send_tuner_commands(self):
        class Channel:
            ready = True
            def __init__(self):
                self.sent = []
            def send(self, *args, **kwargs):
                self.sent.append(kwargs.get("body", b""))
        for name in ("IC-R8600", "IC-905", "IC-9700"):
            channel = Channel()
            civ = Civ(channel, PROFILES[name]["civ"], lambda _: None, lambda *_: None,
                      lambda: None, lambda: (("tuner", None),), profile=PROFILES[name])
            civ.open = True
            civ.tick(1)
            self.assertFalse(any(b"\x1c\x01\x02" in packet for packet in channel.sent))


class Kenwood(unittest.TestCase):
    def test_kns_login_and_control_encoding(self):
        sent, state, scopes = [], {}, []
        cat = Cat(sent.append, "alice", "secret", False, lambda _: None,
                  lambda *args: scopes.append(args), lambda k, v: state.update({k: v}))
        cat.receive("##CN1")
        self.assertEqual(sent, ["##ID10506alicesecret"])
        cat.receive("##ID1")
        cat.receive("OM02")
        self.assertEqual(state["mode"], 1)
        cat.receive("NR2")
        cat.action("nr", None)
        self.assertEqual(sent[-1], "NR0")
        cat.action("tuner", None)
        self.assertEqual(sent[-1], "AC111")
        cat.receive("DD00")
        self.assertEqual(sent[-1], "DD01")
        cat.close()
        self.assertEqual(sent[-2:], ["##VP0", "DD00"])

    def test_scope_range_and_native_hex_waveform(self):
        sweeps = []
        cat = Cat(lambda _: None, "u", "p", False, lambda _: None,
                  lambda *args: sweeps.append(args), lambda *_: None)
        cat.receive("DD4" + "00" + "0" + f"{200_000:011d}{14_200_000:011d}" + "0")
        cat.receive("##DD2" + "46" * 640)
        self.assertEqual(sweeps, [(14_100_000, 14_300_000, bytes([80]) * 640)])

    def test_rtp_headers_padding_and_pcm_conversion(self):
        payload = struct.pack("<4h", 0, 300, -300, 0)
        packet = bytes.fromhex("8060ffff0000000000000000") + payload
        self.assertEqual(rtp_payload(packet), (65535, payload))
        self.assertEqual(len(Pcm().convert(payload)), len(payload) * 3)
        self.assertIsNone(rtp_payload(b"\0" * 12))
        self.assertIsNone(rtp_payload(bytes.fromhex("906000010000000000000000") + b"\0"))

    def test_native_tcp_rtp_session_without_a_bridge(self):
        errors, audio, scopes, states = [], [], [], {}
        stop = threading.Event()
        with socket.socket() as server, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            reserve.bind(("127.0.0.1", 0))
            audio_port = reserve.getsockname()[1]
            reserve.close()

            def fake_radio():
                try:
                    with server.accept()[0] as connection, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                        connection.settimeout(3)
                        data = b""
                        streaming = False
                        while not stop.is_set():
                            data += connection.recv(4096)
                            while b";" in data:
                                query, _, data = data.partition(b";")
                                query = query.strip().decode()
                                reply = {
                                    "##CN": "##CN1", "##ID10101up": "##ID1",
                                    "DD0": "DD00", "FA": "FA00014200000", "OM0": "OM02",
                                    "NR": "NR0", "AC": "AC110",
                                    "DD4": "DD4000" + f"{200_000:011d}{14_200_000:011d}" + "0",
                                    "##DD2": "##DD2" + "46" * 640,
                                }.get(query)
                                if query == "##VP1":
                                    streaming = True
                                if reply:
                                    connection.sendall((reply + ";").encode())
                            if streaming:
                                packet = bytes.fromhex("806000010000000000000000") + struct.pack("<320h", *([100] * 320))
                                udp.sendto(packet, ("127.0.0.1", audio_port))
                except (OSError, ValueError) as error:
                    if not stop.is_set():
                        errors.append(error)

            thread = threading.Thread(target=fake_radio, daemon=True)
            thread.start()
            def samples(pcm):
                audio.append(pcm)
                if scopes:
                    stop.set()
            def sweep(*args):
                scopes.append(args)
                if audio:
                    stop.set()
            timer = threading.Timer(4, stop.set)
            timer.start()
            try:
                listen("127.0.0.1", port, "u", "p", stop, lambda _: None, samples,
                       lambda hz: states.update(frequency=hz), sweep,
                       on_state=lambda k, v: states.update({k: v}), model="TS-890S", audio_port=audio_port)
            finally:
                timer.cancel()
                stop.set()
                thread.join(1)
            self.assertFalse(errors)
            self.assertTrue(audio)
            self.assertEqual(len(audio[0]), 1920)
            self.assertTrue(scopes)
            self.assertEqual(states["model"], "TS-890S")
            self.assertEqual(states["frequency"], 14_200_000)


if __name__ == "__main__":
    unittest.main()
