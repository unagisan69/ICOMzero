"""Short speaker-only notifications; generated FT8 TX PCM is a separate path."""

import array
import math
import os
import queue
import subprocess
import threading
import time

from ft8log import load_settings


def tone(kind, rate=48000):
    notes = (
        ((660, 0.09), (990, 0.13))
        if kind == "qso"
        else ((660, 0.08), (440, 0.08), (330, 0.12))
    )
    samples = array.array("h")
    for hz, duration in notes:
        count = int(rate * duration)
        ramp = max(1, int(rate * 0.008))
        for i in range(count):
            gain = min(1, i / ramp, (count - 1 - i) / ramp)
            samples.append(round(4200 * gain * math.sin(2 * math.pi * hz * i / rate)))
        samples.extend([0] * int(rate * 0.025))
    if __import__("sys").byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class Sounds:
    def __init__(self, rx_ready):
        self.rx_ready = rx_ready
        self.closed = threading.Event()
        self.events = queue.Queue(maxsize=4)
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.thread.start()

    def play(self, kind):
        if kind == "qso" and not load_settings().get("qso_beep", True):
            return
        try:
            self.events.put_nowait(kind)
        except queue.Full:
            pass

    def work(self):
        while not self.closed.is_set():
            try:
                kind = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            until = time.monotonic() + 3
            while not self.rx_ready() and time.monotonic() < until:
                if self.closed.wait(0.05):
                    return
            if self.closed.is_set() or not self.rx_ready():
                continue
            try:
                subprocess.run(
                    [
                        "aplay",
                        "-q",
                        "-D",
                        "pulse",
                        "-t",
                        "raw",
                        "-f",
                        "S16_LE",
                        "-c",
                        "1",
                        "-r",
                        "48000",
                    ],
                    input=tone(kind),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                    env={
                        **os.environ,
                        "PULSE_SINK": "alsa_output.platform-sound.stereo-fallback",
                    },
                )
            except (OSError, subprocess.SubprocessError):
                pass

    def close(self):
        self.closed.set()
        self.thread.join(timeout=0.3)
