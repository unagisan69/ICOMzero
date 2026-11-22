"""Deadline-bound network audio pacing. The transport thread owns all PTT I/O."""

from dataclasses import dataclass, field
import threading
import time


@dataclass
class Job:
    pcm: bytes
    start: float
    dial: int
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    success: bool = False
    error: str = ""
    message: str = ""
    started: threading.Event = field(default_factory=threading.Event)


class Pacer:
    def __init__(
        self,
        take_job,
        ptt,
        packet,
        ready,
        keyed,
        status,
        rate=48000,
        protection=lambda: None,
        off_confirmed=None,
        packet_ms=10,
    ):
        self.protection = protection
        self.off_confirmed = off_confirmed or (lambda: not self.keyed())
        self.take_job = take_job
        self.ptt = ptt
        self.packet = packet
        self.ready = ready
        self.keyed = keyed
        self.status = status
        self.rate = rate
        self.packet_ms = packet_ms
        self.job = None
        self.position = 0
        self.armed = False
        self.arm_mono = 0
        self.audio_start_mono = 0
        self.last_wall = None
        self.last_mono = None
        self.off_until = 0
        self.off_next = 0
        self.finishing = None

    def end(self, success, error="", wall=None, mono=None):
        wall = time.time() if wall is None else wall
        mono = time.monotonic() if mono is None else mono
        current = self.job
        self.ptt(False)
        self.job = None
        self.armed = False
        self.position = 0
        self.off_until = mono + 3
        self.off_next = mono + 0.1
        if current:
            if not self.off_confirmed():
                self.finishing = (current, success, error, mono)
            else:
                current.success = success
                current.error = error
                current.done.set()
        self.status("RX" if success else error)

    def tick(self, wall=None, mono=None):
        wall = time.time() if wall is None else wall
        mono = time.monotonic() if mono is None else mono
        jump = (
            self.last_wall is not None
            and abs((wall - self.last_wall) - (mono - self.last_mono)) > 0.15
        )
        self.last_wall, self.last_mono = wall, mono
        if self.off_until and self.off_confirmed():
            self.off_until = 0
        if mono < self.off_until and mono >= self.off_next:
            self.ptt(False)
            self.off_next = mono + 0.1
        if self.finishing:
            finished, success, error, since = self.finishing
            if self.off_confirmed():
                finished.success = success
                finished.error = error
                finished.done.set()
                self.finishing = None
                self.off_until = 0
            else:
                if mono - since > 2 and not finished.done.is_set():
                    finished.error = "PTT-off not confirmed"
                    finished.done.set()
                    self.status(finished.error)
                if mono >= self.off_next:
                    self.ptt(False)
                    self.off_next = mono + 0.1
                return
        if self.job is None:
            self.job = self.take_job()
            if self.job:
                self.position = 0
                self.armed = False
                self.off_until = 0
        job = self.job
        if job is None:
            return
        if jump or job.cancel.is_set() or not self.ready(job.dial):
            self.end(False, "Transmit cancelled", wall, mono)
            return
        if self.armed:
            reason = self.protection()
            if reason:
                self.end(False, reason, wall, mono)
                return
        if self.armed and mono - self.arm_mono > 14:
            self.end(False, "Transmit watchdog stopped", wall, mono)
            return
        if not self.armed:
            if wall > job.start + 0.15:
                self.end(False, "Missed transmit period", wall, mono)
                return
            if wall < job.start - 0.5:
                return
            self.ptt(True)
            self.armed = True
            self.arm_mono = mono
            self.audio_start_mono = mono + (job.start - wall)
            self.status("TX")
        if wall < job.start:
            return
        if not self.keyed():
            if job.started.is_set():
                self.end(False, "Radio returned to RX during TX", wall, mono)
            elif wall > job.start + 0.15:
                self.end(False, "PTT not confirmed", wall, mono)
            return
        # Never send an accumulated burst after a CPU stall.
        expected = min(
            len(job.pcm), max(0, int((mono - self.audio_start_mono) * self.rate) * 2)
        )
        if expected - self.position > int(self.rate * 0.12) * 2:
            self.end(False, "Audio pacing stalled", wall, mono)
            return
        chunk = self.rate * self.packet_ms // 1000 * 2
        while self.position < len(job.pcm) and self.position <= expected:
            partial = job.pcm[self.position : self.position + chunk]
            job.started.set()
            self.packet(partial)
            self.position += len(partial)
        if (
            self.position >= len(job.pcm)
            and mono >= self.audio_start_mono + len(job.pcm) / (self.rate * 2) + 0.2
        ):
            self.end(True, wall=wall, mono=mono)

    def close(self, reason="Disconnected"):
        if self.job or self.armed:
            self.end(False, reason)
        else:
            self.ptt(False)
        if self.finishing:
            job, _, _, _ = self.finishing
            job.error = reason
            job.done.set()
            self.finishing = None
