"""Receive-only, single-tone CW decoding with adaptive Morse timing."""

from collections import deque
import queue
import threading
import time
import numpy as np

MORSE = dict(
    zip(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        ".- -... -.-. -.. . ..-. --. .... .. .--- -.- .-.. -- -. --- .--. --.- .-. ... - ..- ...- .-- -..- -.-- --.. ----- .---- ..--- ...-- ....- ..... -.... --... ---.. ----.".split(),
    )
)
MORSE.update(
    {
        ".": ".-.-.-",
        ",": "--..--",
        "?": "..--..",
        "/": "-..-.",
        "=": "-...-",
        "+": ".-.-.",
        "-": "-....-",
        "@": ".--.-.",
        "(": "-.--.",
        ")": "-.--.-",
        ":": "---...",
        "'": ".----.",
        "!": "-.-.--",
        "&": ".-...",
        '"': ".-..-.",
        "_": "..--.-",
        "<SK>": "...-.-",
    }
)
LETTERS = {value: key for key, value in MORSE.items()}


class Timing:
    """Learn one/three-unit marks and one/three/seven-unit gaps."""

    def __init__(self, text=""):
        self.text = text
        self.unit = 0.06
        self.learned = False
        self.history = deque(maxlen=64)
        self.buffer = []
        self.character = []
        self.down = False
        self.changed = 0.0
        self.candidate = False
        self.candidate_at = 0.0
        self.last_mark = None

    def estimate(self):
        runs = [
            (mark, duration)
            for mark, duration in self.history
            if 0.02 <= duration <= (0.8 if mark else 1.4)
        ]
        if not runs:
            return self.unit
        candidates = {self.unit}
        for mark, duration in runs:
            candidates.update(
                round(duration / ratio, 4)
                for ratio in ((1, 3) if mark else (1, 3, 7))
                if 0.029 <= duration / ratio <= 0.2
            )

        units = np.array(sorted(candidates))
        lengths = np.array([duration for _, duration in runs])
        marks = np.array([mark for mark, _ in runs])
        errors = np.abs(
            np.log(
                lengths[None, :, None]
                / (units[:, None, None] * np.array([1, 3, 7])[None, None, :])
            )
        )
        errors[:, marks, 2] = np.inf
        weights = np.where(marks, 2, 1)
        costs = (np.minimum(errors.min(axis=2), 1.5) * weights).sum(
            axis=1
        ) / weights.sum()
        costs += 0.025 * np.abs(np.log(units / self.unit))
        return float(units[int(np.argmin(costs))])

    def append(self, value):
        if value == " " and (not self.text or self.text.endswith(" ")):
            return
        self.text = (self.text + value)[-1024:]

    def flush(self):
        if self.character:
            code = "".join(
                "-" if duration >= 2 * self.unit else "." for duration in self.character
            )
            self.append(LETTERS.get(code, "?"))
            self.character = []

    def consume(self, mark, duration):
        if mark:
            if 0.018 <= duration <= 0.8:
                self.character.append(duration)
            else:
                self.character = []
        else:
            if duration >= 2 * self.unit:
                self.flush()
            if duration >= 5 * self.unit:
                self.append(" ")

    def transition(self, down, stamp):
        duration = stamp - self.changed
        was_down = self.down
        self.down, self.changed = down, stamp
        if was_down or self.last_mark is not None:
            if was_down:
                self.last_mark = stamp
            self.history.append((was_down, duration))
            if self.learned:
                self.unit = 0.85 * self.unit + 0.15 * self.estimate()
                self.consume(was_down, duration)
            else:
                self.buffer.append((was_down, duration))
                self.buffer = self.buffer[-64:]
                valid_marks = sum(
                    mark and 0.018 <= length <= 0.8 for mark, length in self.buffer
                )
                if len(self.buffer) >= 6 and valid_marks >= 3:
                    self.unit = self.estimate()
                    self.learned = True
                    for mark, length in self.buffer:
                        self.consume(mark, length)
                    self.buffer = []
        if down and self.last_mark is None:
            self.last_mark = stamp

    def feed(self, down, stamp):
        if down != self.candidate:
            self.candidate, self.candidate_at = down, stamp
        if down != self.down and stamp - self.candidate_at >= 0.009:
            self.transition(down, self.candidate_at)
        if not self.down and self.last_mark is not None:
            quiet = stamp - self.changed
            if (
                not self.learned
                and quiet >= 1.5
                and any(mark and 0.018 <= length <= 0.8 for mark, length in self.buffer)
            ):
                self.unit = self.estimate()
                self.learned = True
                for mark, length in self.buffer:
                    self.consume(mark, length)
                self.buffer = []
            if self.learned:
                if quiet >= 2.2 * self.unit:
                    self.flush()
                if quiet >= 5 * self.unit:
                    self.append(" ")


class Core:
    """20 ms spectral windows, 10 ms timing; accepts mono signed 16-bit PCM."""

    def __init__(self, rate=48000, text=""):
        if rate not in (12000, 48000):
            raise ValueError("CW audio must be 12 or 48 kHz")
        self.factor = rate // 12000
        self.raw = np.empty(0, dtype=np.float32)
        self.audio = np.empty(0, dtype=np.float32)
        self.window = np.hanning(240)
        self.bins = np.fft.rfftfreq(240, 1 / 12000)
        self.band = np.flatnonzero((self.bins >= 300) & (self.bins <= 1400))
        self.clock = 0.0
        self.pitch = None
        self.signal_at = -10.0
        self.level = 0.0
        self.timing = Timing(text)

    def feed(self, pcm):
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        self.raw = np.concatenate((self.raw, samples))
        count = len(self.raw) // self.factor * self.factor
        if not count:
            return
        samples = self.raw[:count].reshape(-1, self.factor).mean(axis=1)
        self.raw = self.raw[count:]
        self.audio = np.concatenate((self.audio, samples))
        while len(self.audio) >= 240:
            frame = self.audio[:240]
            self.audio = self.audio[120:]
            power = np.abs(np.fft.rfft((frame - frame.mean()) * self.window)) ** 2
            peak = int(self.band[np.argmax(power[self.band])])
            noise = max(float(np.median(power[self.band])), 1e-10)
            total = max(float(power[self.band].sum()), 1e-10)
            rms = float(np.sqrt(np.mean(frame * frame)))
            if self.pitch is None or self.clock - self.signal_at > 2:
                if (
                    power[peak] / noise > 30
                    and power[max(0, peak - 1) : peak + 2].sum() / total > 0.85
                    and rms > 0.0001
                ):
                    self.pitch = float(self.bins[peak])
            keyed = False
            if self.pitch is not None:
                center = int(round(self.pitch / 50))
                carrier = float(power[max(0, center - 1) : center + 2].sum())
                self.level = max(carrier, self.level * 0.995)
                keyed = (
                    carrier / noise > (10 if self.timing.down else 16)
                    and carrier > self.level * (0.025 if self.timing.down else 0.05)
                    and carrier / total > (0.65 if self.timing.down else 0.8)
                    and rms > 0.0001
                )
                if keyed:
                    self.signal_at = self.clock
                    if abs(self.bins[peak] - self.pitch) <= 75:
                        self.pitch = 0.95 * self.pitch + 0.05 * float(self.bins[peak])
            self.clock += 0.01
            self.timing.feed(bool(keyed), self.clock)

    def snapshot(self):
        return {
            "text": self.timing.text,
            "pitch": round(self.pitch) if self.pitch is not None else None,
            "wpm": round(1.2 / self.timing.unit) if self.timing.learned else None,
            "key": self.timing.down,
            "status": (
                "Decoding"
                if self.timing.learned
                else "Learning speed" if self.pitch is not None else "Listening for CW"
            ),
        }


class Decoder:
    """Bounded non-blocking audio observer. No radio commands or playback."""

    def __init__(self, rate=48000):
        self.rate = rate
        self.lock = threading.Lock()
        self.jobs = queue.Queue(maxsize=16)
        self.stop = threading.Event()
        self.active = False
        self.context = None
        self.generation = 0
        self.state = Core(rate).snapshot()
        self.dropped = 0
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.thread.start()

    def configure(self, active, context=None):
        with self.lock:
            if (bool(active), context) == (self.active, self.context):
                return
            self.active, self.context = bool(active), context
            self.generation += 1
            self.state = Core(self.rate).snapshot()

    def feed(self, pcm):
        with self.lock:
            if not self.active:
                return
            generation = self.generation
        try:
            self.jobs.put_nowait((generation, time.monotonic(), pcm))
        except queue.Full:
            with self.lock:
                self.dropped += 1
                self.generation += 1
            # A dropped interval must not join two Morse elements.

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def work(self):
        generation = -1
        core = Core(self.rate)
        last = None
        while not self.stop.is_set():
            try:
                current, stamp, pcm = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            with self.lock:
                if current != self.generation or not self.active:
                    continue
            if current != generation:
                core = Core(self.rate)
                generation = current
                last = None
            if last is not None and stamp - last > 0.15:
                core = Core(
                    self.rate,
                    core.timing.text.rstrip() + " " if core.timing.text else "",
                )
            last = stamp
            core.feed(pcm)
            state = core.snapshot()
            with self.lock:
                if current == self.generation and self.active:
                    self.state = state

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=1)
