"""Persistent desktop sessions: presentation tabs do not own radio lifetimes."""

from contextlib import ExitStack
import threading
import time

from cw import Decoder as CWDecoder
from ft8 import BANDS, Decoder as FT8Decoder, History
from ft8station import Station
from ft8log import load_settings
from models import DEFAULT_PROFILE
from shutdown import stop_receiver


class Sessions:
    def __init__(
        self, app, book, *, receiver=None, cw=None, decoder=None, station=None
    ):
        self.app, self.book = app, book
        self.make_receiver = receiver or app["Receiver"]
        self.make_cw = cw or CWDecoder
        self.make_decoder = decoder or FT8Decoder
        self.make_station = station or Station
        self.rx = self.cw = self.decoder = self.station = None
        self.mode = None
        self.desired_view = None
        self.switching = False
        self.error = ""
        self.worker = None
        self.closed = False
        self.cw_enabled = False
        self.cw_tone = None
        self.history = History()
        self.previous = None
        self.revision = None
        self.configured = self.ready = self.pending_tune = False
        self.band_required = False
        self.dial = None
        self.first_entry = True
        self.lock = threading.RLock()

    def select_view(self, view):
        # Log and Settings are presentation-only. They never stop a session.
        if view in ("radio", "ft8"):
            self.desired_view = view
        if (
            view in ("radio", "ft8")
            and self.mode
            and view != self.mode
            and not self.switching
        ):
            self.switch(view)

    def switch(self, mode=None):
        with self.lock:
            if self.switching or self.closed:
                return False
            values = self.app["load"]() if mode else None
            settings = self.app["connection"](values) if mode else None
            self.desired_view = mode
            self.switching = True
            self.error = ""
            self.worker = threading.Thread(
                target=self._transition, args=(mode, settings), daemon=True
            )
            self.worker.start()
            return True

    def _transition(self, mode, settings):
        try:
            self._stop()
            if mode is None or self.closed:
                return
            rx = self.make_receiver()
            if mode == "radio":
                cw = self.make_cw(rate=self.app["RATE"])
                rx.audio_observer = cw.feed
                rx.audio_gap_observer = cw.discontinuity
                self.cw = cw
            else:
                rate = (
                    self.app["RATE"]
                    if self.app["read_config"]().get("Radio") == "TS-890S"
                    else self.app["FT8_RATE"]
                )
                self.decoder = self.make_decoder(rate=rate)
                rx.audio_observer = self.decoder.feed
                rx.play_audio = False
                rx.tx_enabled = True
                self.station = self.make_station(rx, self.decoder, self.book)
            self.rx, self.mode = rx, mode
            self.previous = None
            self.configured = self.ready = self.pending_tune = self.band_required = (
                False
            )
            self.dial = None
            self.first_entry = True
            self.history = History()
            self.revision = rx.session_revision
            rx.start(settings)
        except Exception as error:
            self.error = (
                str(error)
                if isinstance(error, ValueError)
                else "Session error: " + type(error).__name__
            )
            # Clean partial construction too; do not leave a worker behind.
            try:
                self._stop()
            except Exception:
                pass
        finally:
            self.switching = False

    def _stop(self):
        rx = self.rx

        def restore():
            if (
                rx
                and self.mode == "ft8"
                and rx.busy()
                and self.previous
                and rx.radio_state.get("ptt") is False
                and not rx.radio_state.get("ptt_pending")
                and rx.radio_state.get("tuner") != 2
            ):
                rx.tune(self.previous[0])
                rx.command("mode", self.previous[1])
                if (
                    self.previous[2] is not None
                    and rx.radio_state.get("nr") != self.previous[2]
                ):
                    rx.command("nr")
                time.sleep(0.25)

        with ExitStack() as cleanup:
            if self.cw:
                cleanup.callback(self.cw.close)
            if self.decoder:
                cleanup.callback(self.decoder.close)
            if rx:
                cleanup.callback(stop_receiver, rx, before_disconnect=restore)
            if self.decoder:
                cleanup.callback(self.decoder.activate, False)
            if self.station:
                cleanup.callback(self.station.close)
        self.rx = self.cw = self.decoder = self.station = None
        self.mode = None

    def tick(self):
        if self.switching or not self.rx:
            return
        if self.desired_view and self.mode and self.desired_view != self.mode:
            self.switch(self.desired_view)
            return
        rx = self.rx
        if self.mode == "radio":
            mode = rx.radio_state.get("mode")
            self.cw.configure(
                self.cw_enabled
                and rx.busy()
                and not rx.radio_state.get("ptt")
                and rx.radio_state.get("tuner") != 2,
                (rx.session_revision, rx.frequency, mode),
                tone=self.cw_tone,
            )
            return
        profile = rx.radio_state.get("profile", DEFAULT_PROFILE)
        if self.revision != rx.session_revision:
            self.stop_tx()
            self.decoder.activate(False, clear=True)
            self.configured = self.ready = self.pending_tune = False
            self.band_required = False
            self.dial = None
            self.history = History()
            self.revision = rx.session_revision
        bands = self.bands()
        if (
            rx.frequency is not None
            and "mode" in rx.radio_state
            and (not profile["nr"] or "nr" in rx.radio_state)
            and not self.configured
            and not self.band_required
        ):
            if self.previous is None:
                self.previous = (
                    rx.frequency,
                    rx.radio_state["mode"],
                    rx.radio_state.get("nr"),
                )
            if any(hz == rx.frequency for _, hz in bands):
                self.dial = rx.frequency
                (
                    rx.command("ft8_receive")
                    if profile.get("ft8_data_mode")
                    else rx.command("mode", 1)
                )
                if rx.radio_state.get("nr"):
                    rx.command("nr")
                self.configured = True
                self.pending_tune = self.first_entry
                self.first_entry = False
            else:
                self.band_required = True
        prepared = (
            self.configured
            and rx.busy()
            and rx.frequency == self.dial
            and rx.radio_state.get("mode") == 1
            and (
                not profile.get("ft8_data_mode")
                or (
                    rx.radio_state.get("data_mode") == profile["ft8_data_mode"]
                    and all(
                        rx.radio_state.get(name) == 0
                        for name in profile.get("tx_controls", {})
                    )
                )
            )
        )
        if (
            self.pending_tune
            and prepared
            and not rx.radio_state.get("ptt")
            and not rx.radio_state.get("ptt_pending")
        ):
            self.station.action("tune", dial=self.dial)
            self.pending_tune = False
        if prepared and not rx.radio_state.get("ptt") and not self.ready:
            self.decoder.activate()
            self.ready = True
        if self.ready and not rx.busy():
            self.decoder.activate(False)
            self.ready = False
        messages, _, _ = self.decoder.snapshot()
        settings = load_settings()
        band = next((name for name, hz in bands if hz == self.dial), "")
        worked = (
            self.book.worked_calls(band, settings)
            if settings.get("hide_worked")
            else ()
        )
        self.history.update(
            messages, settings["callsign"], worked, self.station.current_partner
        )

    def bands(self):
        profile = (
            self.rx.radio_state.get("profile", DEFAULT_PROFILE)
            if self.rx
            else DEFAULT_PROFILE
        )
        return [
            (name, hz)
            for name, hz in BANDS
            if any(low <= hz <= high for _, low, high in profile["bands"])
        ]

    def set_band(self, hz):
        if not self.rx or self.switching:
            return
        if self.mode == "radio":
            self.rx.change_band(hz)
            return
        if hz not in [value for _, value in self.bands()]:
            raise ValueError("Choose a supported FT8 band")
        self.stop_tx()
        self.decoder.activate(False, clear=True)
        self.dial = hz
        self.ready = False
        self.configured = True
        self.band_required = False
        self.pending_tune = True
        self.first_entry = False
        self.rx.tune(hz)
        profile = self.rx.radio_state.get("profile", DEFAULT_PROFILE)
        (
            self.rx.command("ft8_receive")
            if profile.get("ft8_data_mode")
            else self.rx.command("mode", 1)
        )

    def action(self, action, message=None):
        if not self.station or self.switching:
            raise ValueError("Connect FT8 first")
        if action not in ("stop", "clear_fault") and (
            not self.configured or self.band_required
        ):
            raise ValueError("Choose an FT8 band first")
        self.station.action(action, message, dial=self.dial or 7074000)

    def stop_tx(self):
        if self.station:
            self.station.action("stop")
        elif self.rx:
            self.rx.stop_tx()
            self.rx.command("tuner_stop")

    @property
    def armed(self):
        return bool(
            self.station
            and (
                self.station.active
                or self.station.auto_active
                or self.station.tuning
                or self.station.sequence.enabled
            )
        )

    def close(self):
        self.closed = True
        if self.worker and self.worker.is_alive():
            self.worker.join()
        self._stop()
