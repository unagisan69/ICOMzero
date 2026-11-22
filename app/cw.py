"""Receive-only, single-tone CW decoding with adaptive Morse timing."""

from collections import deque
import math
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
        self.prefix = text
        self.preview = ""
        self.word_marks = []
        self.word_gaps = []
        self.pending_gap = None
        self.dirty = True
        self.unit = 0.06
        self.learned = False
        self.history = deque(maxlen=64)
        self.transitions = 0
        self.buffer = []
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

    @property
    def text(self):
        return (self.prefix + self.preview)[-1024:]

    def gap_means(self):
        gaps = [
            d for mark, d in self.history if not mark and 0.015 <= d <= 4.5 * self.unit
        ]
        short = [d for d in gaps if d < 1.8 * self.unit]
        long = [d for d in gaps if d >= 1.8 * self.unit]
        intra = float(np.median(short)) if len(short) >= 4 else self.unit
        letter = float(np.median(long)) if len(long) >= 2 else 3 * intra
        return intra, letter

    def word_gap(self):
        _, letter = self.gap_means()
        return max(3.5 * self.unit, min(5 * self.unit, 1.9 * letter))

    def decode_word(self):
        # Compare Morse-valid segmentations across a whole word. Uneven hand
        # spacing must not permanently turn two letters into an invalid symbol.
        # Costs use measured gaps only: no language dictionary or callsign guesses.
        if not self.word_marks:
            return ""
        short, long = self.gap_means()
        code = ["-" if d >= 2 * self.unit else "." for d in self.word_marks]
        # Keep already-valid letter boundaries. Reconsider ambiguous timing only
        # when the hard split produces a symbol outside the Morse alphabet.
        hard = []
        start = 0
        for index, gap in enumerate(self.word_gaps):
            if gap >= 2 * self.unit:
                hard.append(LETTERS.get("".join(code[start : index + 1])))
                start = index + 1
        hard.append(LETTERS.get("".join(code[start:])))
        if all(letter is not None for letter in hard):
            return "".join(hard)
        count = len(code)
        best = [(float("inf"), "") for _ in range(count + 1)]
        best[0] = (0.0, "")

        def gap_cost(gap, between):
            mean = long if between else short
            return min(9.0, (math.log(gap / mean) / 0.5) ** 2)

        for end in range(1, count + 1):
            for start in range(max(0, end - 7), end):
                letter = LETTERS.get("".join(code[start:end]))
                if letter is None:
                    continue
                cost = best[start][0] + 0.4
                if start:
                    cost += gap_cost(self.word_gaps[start - 1], True)
                for index in range(start, end - 1):
                    cost += gap_cost(self.word_gaps[index], False)
                if cost < best[end][0]:
                    best[end] = (cost, best[start][1] + letter)
        return best[-1][1] if best[-1][0] / count < 3 else "?"

    def append(self, value):
        if value == " ":
            if self.word_marks:
                self.prefix += self.decode_word()
            self.word_marks = []
            self.word_gaps = []
            self.pending_gap = None
            self.preview = ""
            if self.prefix and not self.prefix.endswith(" "):
                self.prefix += " "
        else:
            self.prefix += value
        self.prefix = self.prefix[-1024:]

    def flush(self):
        if self.dirty:
            self.preview = self.decode_word()
            self.dirty = False

    def consume(self, mark, duration):
        if mark:
            if 0.018 <= duration <= 0.8:
                if self.word_marks:
                    self.word_gaps.append(self.pending_gap or self.unit)
                self.pending_gap = None
                self.word_marks.append(duration)
                self.dirty = True
                if len(self.word_marks) >= 64:
                    self.append(" ")
            else:
                self.append(" ")
        else:
            if duration >= self.word_gap():
                self.append(" ")
            elif self.word_marks:
                self.pending_gap = duration
                if duration >= 2 * self.unit:
                    self.flush()

    def transition(self, down, stamp):
        duration = stamp - self.changed
        was_down = self.down
        self.down, self.changed = down, stamp
        if was_down or self.last_mark is not None:
            if was_down:
                self.last_mark = stamp
            self.history.append((was_down, duration))
            self.transitions += 1
            if self.learned:
                if self.transitions % 4 == 0:
                    self.unit = 0.8 * self.unit + 0.2 * self.estimate()
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
        if not self.down and not self.candidate and self.last_mark is not None:
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
                if quiet >= self.word_gap():
                    self.append(" ")


class Core:
    """20 ms spectral windows and 5 ms key timing; mono signed 16-bit PCM."""

    def __init__(self, rate=48000, text="", tone=None):
        if rate not in (12000, 48000):
            raise ValueError("CW audio must be 12 or 48 kHz")
        if tone is not None and not 200 <= tone <= 2800:
            raise ValueError("CW tone must be 200–2800 Hz")
        self.factor = rate // 12000
        self.raw = np.empty(0, dtype=np.float32)
        self.audio = np.empty(0, dtype=np.float32)
        self.window = np.hanning(240)
        self.bins = np.fft.rfftfreq(240, 1 / 12000)
        self.band = np.flatnonzero((self.bins >= 200) & (self.bins <= 2800))
        if tone is not None:
            self.band = self.band[abs(self.bins[self.band] - tone) <= 50]
        self.clock = 0.0
        self.fixed_tone = tone
        self.tone_profile = np.zeros(len(self.bins))
        self.pitch = None
        self.signal_at = -10.0
        self.candidate_pitch = None
        self.candidate_at = 0
        # Two seconds of spectra for a bounded noise floor and acquisition lookback.
        self.frames = deque(maxlen=400)
        self.floor = None
        self.floor_tick = 0
        self.timing = Timing(text)

    def measures(self, power, center):
        carrier = float(power[center - 1 : center + 2].sum())
        neighbors = np.concatenate(
            (power[max(1, center - 8) : center - 2], power[center + 3 : center + 9])
        )
        noise = max(
            float(np.percentile(neighbors, 60)),
            float(np.median(power[max(1, center - 3) : center + 4])),
            1e-10,
        )
        return carrier, noise

    def detect(self, power, rms, stamp):
        center = int(round(self.pitch / 50.0))
        carrier, noise = self.measures(power, center)
        # Averaging line shape rejects narrow receiver-noise bursts after a station stops.
        self.tone_profile = 0.95 * self.tone_profile + 0.05 * power
        profile_concentration = self.tone_profile[center - 1 : center + 2].sum() / max(
            float(self.tone_profile[max(1, center - 3) : center + 4].sum()), 1e-10
        )
        concentration = carrier / max(
            float(power[max(1, center - 3) : center + 4].sum()), 1e-10
        )
        keyed = (
            carrier / noise > (10 if self.timing.down else 16)
            and concentration > (0.65 if self.timing.down else 0.8)
            and profile_concentration > 0.8
            and rms > 0.0001
        )
        if keyed:
            self.signal_at = stamp
            near = np.arange(center - 1, center + 2)
            k = int(near[np.argmax(power[near])])
            a, b, c = np.log(np.maximum(power[k - 1 : k + 2], 1e-20))
            offset = (
                float(np.clip(0.5 * (a - c) / (a - 2 * b + c), -0.5, 0.5))
                if a - 2 * b + c
                else 0
            )
            target = (k + offset) * 50.0
            coherent = (
                carrier
                / max(float(power[max(1, center - 3) : center + 4].sum()), 1e-10)
                > 0.8
            )
            in_selection = (
                self.fixed_tone is None or abs(target - self.fixed_tone) <= 75
            )
            if abs(target - self.pitch) <= 30 and coherent and in_selection:
                self.pitch += 0.02 * (target - self.pitch)
        self.timing.feed(bool(keyed), stamp)

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
            self.audio = self.audio[60:]
            power = abs(np.fft.rfft((frame - frame.mean()) * self.window)) ** 2
            rms = float(np.sqrt(np.mean(frame * frame)))
            self.clock += 0.005
            self.frames.append((power, rms, self.clock))
            if self.pitch is None or self.clock - self.signal_at > 2:
                self.floor_tick += 1
                if len(self.frames) < 30:
                    continue
                if self.floor is None or self.floor_tick >= 20:
                    self.floor = np.percentile(
                        np.array([p for p, _, _ in self.frames]), 10, axis=0
                    )
                    self.floor_tick = 0
                # Prefer keyed energy over a steady carrier elsewhere in the passband.
                score = np.maximum(power - 3 * self.floor, 0)
                peak = int(self.band[np.argmax(score[self.band])])
                carrier, noise = self.measures(power, peak)
                a, b, c = np.log(np.maximum(power[peak - 1 : peak + 2], 1e-20))
                offset = (
                    float(np.clip(0.5 * (a - c) / (a - 2 * b + c), -0.5, 0.5))
                    if a - 2 * b + c
                    else 0
                )
                peak_hz = (peak + offset) * 50
                concentration = carrier / max(
                    float(power[max(1, peak - 3) : peak + 4].sum()), 1e-10
                )
                valid = (
                    score[peak] > 0
                    and concentration > 0.85
                    and carrier / noise > 18
                    and rms > 0.0001
                )
                if valid:
                    if (
                        self.candidate_pitch is None
                        or abs(peak_hz - self.candidate_pitch) > 25
                    ):
                        self.candidate_pitch = peak_hz
                        self.candidate_at = self.clock
                    if self.clock - self.candidate_at >= 0.035:
                        self.pitch = float(self.candidate_pitch)
                        self.tone_profile.fill(0)
                        self.timing = Timing(
                            self.timing.text.rstrip() + " " if self.timing.text else ""
                        )
                        # Confirmation delays the lock, but must not cut off its first mark.
                        for old_power, old_rms, stamp in self.frames:
                            if stamp >= self.candidate_at - 0.04:
                                self.detect(old_power, old_rms, stamp)
                        self.candidate_pitch = None
                        continue
                else:
                    self.candidate_pitch = None
                # The old channel still flushes its final character while waiting.
                if self.pitch is not None:
                    self.detect(power, rms, self.clock)
            else:
                self.candidate_pitch = None
                self.detect(power, rms, self.clock)

    def snapshot(self):
        return {
            "text": self.timing.text,
            "pitch": round(self.pitch) if self.pitch is not None else None,
            "wpm": round(1.2 / self.timing.unit) if self.timing.learned else None,
            "key": self.timing.down,
            "status": (
                "Listening for CW"
                if self.clock - self.signal_at > 2
                else (
                    "Decoding"
                    if self.timing.learned
                    else (
                        "Learning speed"
                        if self.pitch is not None
                        else "Listening for CW"
                    )
                )
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
        self.tone = None
        self.generation = 0
        self.state = Core(rate).snapshot()
        self.dropped = 0
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.thread.start()

    def configure(self, active, context=None, tone=None):
        with self.lock:
            if (bool(active), context, tone) == (self.active, self.context, self.tone):
                return
            self.active, self.context, self.tone = bool(active), context, tone
            self.generation += 1
            self.state = Core(self.rate, tone=tone).snapshot()

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
                core = Core(self.rate, tone=self.tone)
                generation = current
                last = None
            if last is not None and stamp - last > 0.15:
                core = Core(
                    self.rate,
                    core.timing.text.rstrip() + " " if core.timing.text else "",
                    tone=self.tone,
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
