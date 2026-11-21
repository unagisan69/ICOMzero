"""Receive-only FT8 capture. Network PCM is captured before speaker attenuation."""
from collections import deque
from pathlib import Path
import queue
import subprocess
import threading
import time

import numpy as np

BANDS = (("160m", 1840000), ("80m", 3573000), ("60m", 5357000),
         ("40m", 7074000), ("30m", 10136000), ("20m", 14074000),
         ("17m", 18100000), ("15m", 21074000), ("12m", 24915000),
         ("10m", 28074000), ("6m", 50313000), ("2m", 144174000))


def decoder_path():
    installed = Path(__file__).with_name("icomzero-ft8-decode")
    return installed if installed.exists() else Path(__file__).resolve().parent.parent / "build/icomzero-ft8-decode"


def decode(pcm, executable=None):
    result = subprocess.run([str(executable or decoder_path())], input=pcm,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12, check=True)
    messages = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        fields = line.split("\t", 2)
        if len(fields) == 3:
            messages.append((float(fields[0]), round(float(fields[1])), fields[2]))
    return messages


class Decoder:
    def __init__(self):
        self.messages = deque(maxlen=200)
        self.status = "Waiting for radio"
        self.progress = 0
        self.last_result = ""
        self.lock = threading.Lock()
        self.jobs = queue.Queue(maxsize=1)
        self.closed = threading.Event()
        self.generation = 0
        self.active = False
        self.anchor = None
        self.count = 0
        self.slot = None
        self.parts = []
        self.complete = False
        # 129-tap low-pass rejects frequencies above the 12 kHz Nyquist limit.
        n = np.arange(129) - 64
        self.kernel = np.sinc(2 * 5000 / 48000 * n) * np.hamming(129)
        self.kernel /= self.kernel.sum()
        self.tail = np.zeros(128)
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.thread.start()

    def activate(self, active=True, clear=False):
        with self.lock:
            if clear:
                self.messages.clear()
            self.generation += 1
            self.active = active
            self.anchor = self.slot = None
            self.parts = []
            self.progress = 0
            self.last_result = ""
            self.status = "Waiting for complete 15s cycle" if active else "Waiting for radio"

    def feed(self, pcm, now=None):
        now = time.time() if now is None else now
        samples = np.frombuffer(pcm, dtype="<i2")
        with self.lock:
            if not self.active or not len(samples):
                return
            duration = len(samples) / 48000
            if self.anchor is None or abs(now - (self.anchor + (self.count + len(samples)) / 48000)) > .5:
                self.anchor = now - duration
                self.count = 0
                self.slot = None
                self.parts = []
                self.tail = np.zeros(128)
            start = self.anchor + self.count / 48000
            filtered = np.convolve(np.concatenate((self.tail, samples)), self.kernel, mode="valid")
            self.tail = np.concatenate((self.tail, samples))[-128:]
            phase = (-self.count) % 4
            reduced = np.clip(filtered[phase::4], -32768, 32767).astype("<i2")
            times = start + (phase + np.arange(len(reduced)) * 4) / 48000
            self.count += len(samples)
            for slot in np.unique((times // 15).astype(np.int64)):
                selected = reduced[(times >= slot * 15) & (times < (slot + 1) * 15)]
                if self.slot != slot:
                    if self.slot is not None and self.complete and self.parts:
                        data = b"".join(self.parts)
                        if len(data) >= 179900 * 2:
                            try:
                                self.jobs.put_nowait((self.generation, self.slot, data))
                            except queue.Full:
                                self.status = "Decoder busy; skipped cycle"
                    self.slot = int(slot)
                    self.parts = []
                    self.complete = start <= slot * 15 + .02
                if self.complete:
                    self.parts.append(selected.tobytes())
                    self.progress = min(1, sum(map(len, self.parts)) / 360000)
                    self.status = self.last_result or "Receiving 15s cycle"

    def work(self):
        while not self.closed.is_set():
            try:
                generation, slot, pcm = self.jobs.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                found = decode(pcm)
                with self.lock:
                    if generation == self.generation and self.active:
                        stamp = time.strftime("%H:%M:%S", time.gmtime(slot * 15))
                        self.messages.extend((stamp, hz, text) for _, hz, text in found)
                        self.last_result = f"Last cycle: {len(found)} decoded"
                        self.status = self.last_result
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                with self.lock:
                    if generation == self.generation:
                        self.status = "Decoder error: " + type(error).__name__

    def snapshot(self):
        with self.lock:
            return list(self.messages), self.status, self.progress

    def close(self):
        self.activate(False)
        self.closed.set()
        self.thread.join(timeout=13)
