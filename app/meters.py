"""Model-calibrated telemetry and latched RF protection in the transport thread."""

import time


def interpolate(points, value):
    points = sorted(points)
    if not points or value is None:
        return None
    if value <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if value <= x1:
            return y0 + (value - x0) * (y1 - y0) / (x1 - x0)
    return points[-1][1]


class Protection:
    def __init__(self, profile, publish=lambda *_: None):
        self.curves = profile.get("meters", {})
        self.publish = publish
        self.samples = {}
        self.started = None
        self.tuning = False
        self.alc_since = None
        self.fault = None

    def begin(self, tuning=False, now=None):
        self.started = time.monotonic() if now is None else now
        self.tuning = tuning
        self.samples.clear()
        self.alc_since = None
        for name in ("swr", "alc", "watts"):
            self.publish(name, None)
            self.publish(name + "_at", None)

    def update(self, name, raw, now=None):
        now = time.monotonic() if now is None else now
        points = self.curves.get(name, [])
        if not points:
            return
        if raw is None or not 0 <= raw <= 255:
            self.trip("Invalid " + name.upper() + " telemetry")
            return
        value = interpolate(points, raw)
        self.samples[name] = (value, now)
        self.publish(name, value)
        self.publish(name + "_at", now)

    def trip(self, reason):
        if not self.fault:
            self.fault = reason
            self.publish("tx_fault", reason)
        return self.fault

    def clear(self):
        self.fault = None
        self.started = None
        self.alc_since = None
        self.samples.clear()
        self.publish("tx_fault", None)

    def check(self, now=None):
        now = time.monotonic() if now is None else now
        if self.fault or self.started is None:
            return self.fault
        swr = self.samples.get("swr")
        if swr and swr[1] >= self.started and now - swr[1] <= 1.2 and swr[0] > 2.0:
            return self.trip(f"SWR {swr[0]:.2f} > 2.0 — TX stopped")
        # Tuner carriers are generated/regulated inside the radio, not by our
        # audio encoder. The external-audio ALC guard applies to FT8 frames.
        alc = self.samples.get("alc")
        if not self.tuning and alc and alc[1] >= self.started and now - alc[1] <= 1.2:
            if alc[0] >= 1.0:
                return self.trip("ALC too high — reduce TX audio")
            if alc[0] > 0.2:
                if self.alc_since is None:
                    self.alc_since = now
                if now - self.alc_since >= 0.3:
                    return self.trip("ALC sustained — reduce TX audio")
            else:
                self.alc_since = None
        if now - self.started > 1.2:
            for name in (("swr",) if self.tuning else ("swr", "alc")):
                sample = self.samples.get(name)
                if not sample or sample[1] < self.started or now - sample[1] > 1.2:
                    return self.trip(name.upper() + " telemetry lost — TX stopped")
        return None
