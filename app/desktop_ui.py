"""Native desktop widgets and independent, persistent application tabs."""

from datetime import datetime, timezone
import hashlib
import json
import threading
import time
import numpy as np
from PIL import Image

from contacts import MODES, manual_contact
from countries import activation_cq, message_country, short_country
from desktop_session import Sessions
from ft8log import (
    Logbook,
    load_settings,
    save_settings,
    validate_station,
    report_text,
    test_service,
)
from ft8qso import contact_draft, finalize_contact
from models import DEFAULT_PROFILE, RADIO_CHOICES, presets
from profiles import FIELDS, STEPS, ProfileStore
from update_session import UpdateSession

BG = "#101a21"
PANEL = "#172731"
CARD = "#20343e"
TEXT = "#e4eff2"
MUTED = "#94aab3"
GREEN = "#70edbb"
RED = "#f07979"


class Spectrum:
    """Native-width data pixels only; all labels are Tk text, never enlarged."""

    def __init__(self):
        self.image = None
        self.key = None
        self.edges = None
        self.levels = None
        stops = np.array(
            (
                (5, 10, 29),
                (20, 40, 119),
                (14, 118, 168),
                (69, 209, 148),
                (255, 191, 69),
                (255, 246, 207),
            )
        )
        points = np.linspace(0, 160, len(stops))
        self.palette = np.stack(
            [np.interp(np.arange(256), points, stops[:, c]) for c in range(3)], axis=1
        ).astype("uint8")

    def update(self, scope, frequency, width, height, zoom):
        if not scope or width < 20 or height < 20:
            return False
        _, lo, hi, bins = scope
        if hi <= lo or not bins:
            return False
        span = max(1, (hi - lo) // zoom)
        center = frequency if frequency is not None else (lo + hi) // 2
        start = max(lo, min(hi - span, center - span // 2))
        end = start + span
        key = (scope[0], start, end, width, height)
        if key == self.key:
            return False
        edges = (start, end)
        if (
            self.image is None
            or self.image.size != (width, height)
            or self.edges != edges
        ):
            self.image = Image.new("RGB", (width, height), "#080f20")
        positions = np.linspace(
            (start - lo) * len(bins) / (hi - lo),
            (end - lo) * len(bins) / (hi - lo),
            width,
            endpoint=False,
        )
        self.levels = np.interp(
            positions, np.arange(len(bins)), np.frombuffer(bytes(bins), dtype=np.uint8)
        )
        row = self.palette[np.clip(self.levels.astype(int), 0, 255)]
        self.image.paste(self.image.crop((0, 0, width, height - 1)), (0, 1))
        self.image.paste(Image.fromarray(row[None, :, :]), (0, 0))
        self.key = key
        self.edges = edges
        return True


class DesktopApp:
    def __init__(self, app, fullscreen=False, *, book=None, sessions=None):
        import tkinter as tk
        from tkinter import ttk, messagebox
        from PIL import ImageTk

        self.tk, self.ttk, self.messagebox, self.ImageTk = tk, ttk, messagebox, ImageTk
        self.app = app
        self.root = tk.Tk(className="ICOMzero")
        self.root.title("ICOMzero · " + app["VERSION"])
        self.root.geometry(
            f"{min(1240,self.root.winfo_screenwidth()-40)}x{min(680,self.root.winfo_screenheight()-50)}"
        )
        self.root.minsize(900, 560)
        self.root.configure(bg=BG)
        self.root.attributes("-fullscreen", fullscreen)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.store = ProfileStore(app["CONFIG"])
        self.book = book or Logbook()
        self.sessions = sessions or Sessions(app, self.book)
        self.update = UpdateSession()
        self.closing = False
        self.stop_worker = None
        self.zoom = 1
        self.spectrum = Spectrum()
        self.scope_photo = None
        self.decode_rows = {}
        self.log_rows = {}
        self.log_offset = 0
        self.log_total = 0
        self.last_log = 0
        self.last_decodes = None
        self.cw_revision = None
        self.saved_revision = 0
        self.toast_until = 0
        self.last_tune = 0
        self.held = {}
        self.releases = {}
        self.refreshing = False
        self.last_tick_error = None
        self.last_summary = 0
        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")
        self.style.configure(
            ".", font=("DejaVu Sans", 11), background=BG, foreground=TEXT
        )
        self.style.configure("TFrame", background=BG)
        self.style.configure("Card.TFrame", background=PANEL)
        self.style.configure("TLabel", background=BG, foreground=TEXT)
        self.style.configure("Muted.TLabel", foreground=MUTED)
        self.style.configure(
            "TButton", background=CARD, foreground=TEXT, padding=(10, 6)
        )
        self.style.map(
            "TButton",
            background=[("active", "#31505b")],
            foreground=[("disabled", "#657781")],
        )
        self.style.configure("Accent.TButton", foreground=GREEN)
        self.style.configure("Danger.TButton", foreground=RED)
        self.style.configure(
            "TEntry", fieldbackground="#223640", foreground=TEXT, padding=5
        )
        self.style.configure(
            "TCombobox",
            fieldbackground=CARD,
            background=CARD,
            foreground=TEXT,
            padding=4,
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", CARD)],
            foreground=[("readonly", TEXT)],
        )
        self.style.configure("TNotebook", background=BG, borderwidth=0)
        self.style.configure(
            "TNotebook.Tab", padding=(22, 10), font=("DejaVu Sans", 11)
        )
        self.style.map(
            "TNotebook.Tab",
            background=[("selected", CARD)],
            foreground=[("selected", GREEN)],
        )
        self.style.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            rowheight=21,
            font=("DejaVu Sans", 10),
            borderwidth=0,
        )
        self.style.configure(
            "Treeview.Heading",
            background=CARD,
            foreground=MUTED,
            font=("DejaVu Sans", 10, "bold"),
            padding=4,
        )
        self.style.map(
            "Treeview",
            background=[("selected", "#315666")],
            foreground=[("selected", "#ffffff")],
        )
        self.style.configure(
            "Horizontal.TProgressbar", background=GREEN, troughcolor=CARD
        )
        self.style.configure(
            "TX.Horizontal.TProgressbar", background=RED, troughcolor=CARD
        )
        self.style.configure(
            "TButton",
            bordercolor="#34505e",
            lightcolor="#34505e",
            darkcolor="#34505e",
            relief="flat",
            borderwidth=1,
        )
        self.style.configure(
            "Accent.TButton",
            background="#1c4039",
            bordercolor="#2d6c56",
            lightcolor="#2d6c56",
            darkcolor="#2d6c56",
        )
        self.style.configure(
            "Danger.TButton",
            background="#3b2b30",
            bordercolor="#644049",
            lightcolor="#644049",
            darkcolor="#644049",
        )
        self.style.configure("TNotebook", bordercolor=BG, lightcolor=BG, darkcolor=BG)
        self.style.configure(
            "TNotebook.Tab",
            background=PANEL,
            bordercolor=PANEL,
            lightcolor=PANEL,
            darkcolor=PANEL,
            padding=(22, 8),
        )
        self.style.configure(
            "Treeview.Heading",
            bordercolor=CARD,
            lightcolor=CARD,
            darkcolor=CARD,
            relief="flat",
        )
        self.style.configure(
            "TEntry", bordercolor="#35505e", lightcolor="#35505e", darkcolor="#35505e"
        )
        self.style.configure(
            "TCombobox",
            bordercolor="#35505e",
            lightcolor="#35505e",
            darkcolor="#35505e",
            arrowcolor=MUTED,
        )
        self.style.configure(
            "Vertical.TScrollbar",
            background="#304b58",
            troughcolor=PANEL,
            bordercolor=PANEL,
            lightcolor=PANEL,
            darkcolor=PANEL,
            arrowcolor=MUTED,
        )
        self.style.configure(
            "Horizontal.TProgressbar", bordercolor=CARD, lightcolor=CARD, darkcolor=CARD
        )
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.header = ttk.Frame(self.root, padding=(16, 10))
        self.header.pack(fill="x")
        ttk.Label(self.header, text="ICOMzero", font=("DejaVu Sans", 17, "bold")).pack(
            side="left"
        )
        ttk.Label(self.header, text="  " + app["VERSION"], style="Muted.TLabel").pack(
            side="left"
        )
        self.global_stop = ttk.Button(
            self.header,
            text="■ Stop TX",
            style="Danger.TButton",
            command=lambda: self.safe(self.sessions.stop_tx),
        )
        self.global_stop.pack(side="right")
        self.session_label = ttk.Label(
            self.header, text="No radio session", style="Muted.TLabel"
        )
        self.session_label.pack(side="right", padx=18)
        footer = ttk.Frame(self.root, padding=(16, 7))
        footer.pack(side="bottom", fill="x")
        self.notice = ttk.Label(
            footer,
            text="Ctrl+1–4 tabs · Ctrl+Space stop TX · Ctrl+Q quit",
            style="Muted.TLabel",
        )
        self.notice.pack(side="left")
        self.state_label = ttk.Label(footer, text="Ready", style="Muted.TLabel")
        self.state_label.pack(side="right")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=12)
        self.frames = {}
        for key, title in (
            ("radio", "Radio"),
            ("ft8", "FT8"),
            ("log", "Log"),
            ("settings", "Settings"),
        ):
            frame = ttk.Frame(self.tabs, padding=12)
            self.frames[key] = frame
            self.tabs.add(frame, text=title)
        self.build_radio()
        self.build_ft8()
        self.build_log()
        self.build_settings()
        self.tabs.bind("<<NotebookTabChanged>>", self.tab_changed)
        self.tag = "ICOMzeroKeys" + str(id(self))
        self.root.bind_class(self.tag, "<KeyPress>", self.key_press)
        self.root.bind_class(self.tag, "<KeyRelease>", self.key_release)
        self.decorate(self.root)
        self.root.bind("<FocusOut>", lambda _: self.root.after_idle(self.check_focus))
        self.next_tick = self.root.after(80, self.tick)
        self.radio_canvas.focus_set()
        if self.store.read().get("check_updates", True):
            self.update.check(startup=True)

    def decorate(self, widget):
        widget.bindtags(
            (self.tag,) + tuple(t for t in widget.bindtags() if t != self.tag)
        )
        for child in widget.winfo_children():
            self.decorate(child)

    def label(self, parent, text, **kwargs):
        return self.ttk.Label(parent, text=text, **kwargs)

    def button(self, parent, text, command, **kwargs):
        return self.ttk.Button(
            parent, text=text, command=lambda: self.safe(command), **kwargs
        )

    def safe(self, command):
        try:
            return command()
        except Exception as error:
            self.notify(
                (
                    str(error)
                    if isinstance(
                        error, (ValueError, self.app.get("RadioError", ValueError))
                    )
                    else type(error).__name__
                ),
                error=True,
            )

    def notify(self, text, error=False):
        self.notice.configure(text=text, foreground=RED if error else GREEN)
        self.toast_until = time.monotonic() + 5

    def current(self):
        selected = self.tabs.select()
        return next(
            (name for name, frame in self.frames.items() if str(frame) == selected),
            "radio",
        )

    def tab_changed(self, _=None):
        view = self.current()
        self.safe(lambda: self.sessions.select_view(view))
        {
            "radio": self.radio_canvas,
            "ft8": self.decode_tree,
            "log": self.log_tree,
            "settings": self.settings_tabs,
        }[view].focus_set()
        if view == "log":
            self.refresh_log(force=True)

    def connect(self, mode):
        if self.sessions.mode == mode and self.sessions.rx and self.sessions.rx.busy():
            self.sessions.switch(None)
        else:
            self.sessions.switch(mode)

    def build_radio(self):
        tk, ttk = self.tk, self.ttk
        frame = self.frames["radio"]
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, 10))
        self.frequency = self.label(
            top, "—.——— MHz", font=("DejaVu Sans Mono", 23, "bold")
        )
        self.frequency.pack(side="left")
        self.radio_mode = tk.StringVar(value="USB")
        self.mode_combo = ttk.Combobox(
            top,
            textvariable=self.radio_mode,
            values=("LSB", "USB", "AM", "CW", "RTTY", "FM", "CW-R", "RTTY-R"),
            width=7,
            state="readonly",
        )
        self.mode_combo.pack(side="left", padx=14)
        self.mode_combo.bind("<<ComboboxSelected>>", lambda _: self.safe(self.set_mode))
        self.step = tk.StringVar(value=str(self.store.config().get("step", 1000)))
        self.step_combo = ttk.Combobox(
            top, textvariable=self.step, values=STEPS, width=6, state="readonly"
        )
        self.step_combo.pack(side="left")
        self.step_combo.bind("<<ComboboxSelected>>", lambda _: self.safe(self.set_step))
        self.label(top, "Hz step", style="Muted.TLabel").pack(side="left", padx=6)
        self.radio_connect = self.button(
            top, "Connect Radio", lambda: self.connect("radio"), style="Accent.TButton"
        )
        self.radio_connect.pack(side="right")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 8))
        self.radio_band = tk.StringVar()
        self.radio_band_combo = ttk.Combobox(
            bar,
            textvariable=self.radio_band,
            values=[name for name, _ in presets(DEFAULT_PROFILE)],
            width=9,
            state="readonly",
        )
        self.radio_band_combo.pack(side="left")
        self.radio_band_combo.bind(
            "<<ComboboxSelected>>", lambda _: self.safe(self.change_radio_band)
        )
        self.button(bar, "− Tune", lambda: self.tune(-1)).pack(side="left", padx=5)
        self.button(bar, "+ Tune", lambda: self.tune(1)).pack(side="left")
        self.button(bar, "Zoom −", lambda: self.change_zoom(-1)).pack(
            side="left", padx=(20, 5)
        )
        self.zoom_label = self.label(bar, "1×")
        self.zoom_label.pack(side="left")
        self.button(bar, "Zoom +", lambda: self.change_zoom(1)).pack(
            side="left", padx=5
        )
        self.cw_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar, text="CW decode", variable=self.cw_on, command=self.cw_changed
        ).pack(side="left", padx=12)
        self.button(bar, "NR", lambda: self.radio_command("nr")).pack(side="left")
        self.button(bar, "Tune antenna", lambda: self.radio_command("tuner")).pack(
            side="left", padx=6
        )
        self.radio_canvas = tk.Canvas(
            frame, bg="#080f20", highlightthickness=0, takefocus=True, height=320
        )
        self.radio_canvas.pack(fill="both", expand=True)
        self.radio_canvas.bind(
            "<Configure>", lambda _: setattr(self.spectrum, "key", None)
        )
        cwbar = ttk.Frame(frame)
        cwbar.pack(fill="x", pady=(7, 0))
        self.cw_label = self.label(cwbar, "CW decoder off", style="Muted.TLabel")
        self.cw_label.pack(side="left")
        self.cw_tone = tk.StringVar(value="Auto")
        tone = ttk.Combobox(
            cwbar,
            textvariable=self.cw_tone,
            values=["Auto"] + list(range(200, 2801, 100)),
            width=7,
            state="readonly",
        )
        tone.pack(side="left", padx=12)
        tone.bind("<<ComboboxSelected>>", lambda _: self.cw_changed())
        self.cw_text = tk.Text(
            frame,
            height=3,
            font=("DejaVu Sans Mono", 11),
            bg=PANEL,
            fg=TEXT,
            relief="flat",
            wrap="word",
            state="disabled",
            takefocus=True,
        )
        self.cw_text.pack(fill="x", pady=(4, 5))
        audio = ttk.Frame(frame)
        audio.pack(fill="x")
        self.volume = tk.DoubleVar(value=self.store.config().get("volume", 60))
        self.muted = tk.BooleanVar(value=self.store.config().get("muted", False))
        ttk.Checkbutton(
            audio, text="Mute", variable=self.muted, command=self.set_audio
        ).pack(side="left")
        self.label(audio, "Volume", style="Muted.TLabel").pack(
            side="left", padx=(15, 6)
        )
        ttk.Scale(
            audio,
            from_=0,
            to=100,
            variable=self.volume,
            command=lambda _: self.set_audio(),
        ).pack(side="left", fill="x", expand=True)
        self.audio_level = self.label(audio, f"{int(self.volume.get())}%")
        self.audio_level.pack(side="left", padx=10)
        self.audio_save = None

    def radio_command(self, action):
        if self.sessions.mode != "radio" or not self.sessions.rx:
            raise ValueError("Connect Radio first")
        self.sessions.rx.command(action)

    def set_mode(self):
        modes = {
            "LSB": 0,
            "USB": 1,
            "AM": 2,
            "CW": 3,
            "RTTY": 4,
            "FM": 5,
            "CW-R": 7,
            "RTTY-R": 8,
        }
        if self.sessions.mode == "radio" and self.sessions.rx:
            self.sessions.rx.command("mode", modes[self.radio_mode.get()])
            if self.radio_mode.get() in ("CW", "CW-R"):
                self.cw_on.set(True)
                self.cw_changed()

    def set_step(self):
        value = int(self.step.get())
        self.store.update(self.store.active()["id"], step=value)
        if self.sessions.rx:
            self.sessions.rx.step = value

    def change_radio_band(self):
        rx = self.sessions.rx
        if self.sessions.mode != "radio" or not rx:
            raise ValueError("Connect Radio first")
        bands = presets(
            rx.radio_state.get("profile", DEFAULT_PROFILE), cw=self.cw_on.get()
        )
        hz = next(hz for name, hz in bands if name == self.radio_band.get())
        self.sessions.set_band(hz)

    def tune(self, direction):
        if (
            self.sessions.mode == "radio"
            and self.sessions.rx
            and not self.sessions.switching
        ):
            self.sessions.rx.tune(steps=direction)

    def change_zoom(self, direction):
        self.zoom = min(8, self.zoom * 2) if direction > 0 else max(1, self.zoom // 2)
        self.zoom_label.configure(text=f"{self.zoom}×")
        self.spectrum.key = None

    def cw_changed(self):
        self.sessions.cw_enabled = self.cw_on.get()
        self.sessions.cw_tone = (
            None if self.cw_tone.get() == "Auto" else int(self.cw_tone.get())
        )

    def set_audio(self):
        level = int(self.volume.get())
        self.audio_level.configure(text=f"{level}%")
        rx = self.sessions.rx
        if rx:
            rx.volume = level
            rx.muted = self.muted.get()
        if self.audio_save:
            self.root.after_cancel(self.audio_save)
        self.audio_save = self.root.after(400, self.save_audio)

    def save_audio(self):
        self.audio_save = None
        self.app["save"](self.app["load"](), int(self.volume.get()), self.muted.get())

    def build_ft8(self):
        tk, ttk = self.tk, self.ttk
        frame = self.frames["ft8"]
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 10))
        self.ft8_connect = self.button(
            bar, "Connect FT8", lambda: self.connect("ft8"), style="Accent.TButton"
        )
        self.ft8_connect.pack(side="left")
        self.ft8_band = tk.StringVar(value="40m")
        self.ft8_bands = ttk.Combobox(
            bar,
            textvariable=self.ft8_band,
            values=[name for name, _ in self.sessions.bands()],
            state="readonly",
            width=8,
        )
        self.ft8_bands.pack(side="left", padx=10)
        self.ft8_bands.bind(
            "<<ComboboxSelected>>", lambda _: self.safe(self.change_ft8_band)
        )
        for label, action in (
            ("Auto CQ", "auto_cq"),
            ("Call CQ", "cq"),
            ("POTA Hunt", "pota_hunt"),
            ("Tune antenna", "tune"),
        ):
            self.button(bar, label, lambda action=action: self.ft8_action(action)).pack(
                side="left", padx=3
            )
        self.button(bar, "Stop TX", self.sessions.stop_tx, style="Danger.TButton").pack(
            side="right"
        )
        self.ft8_info = self.label(
            frame,
            "Connect to receive. Antenna tuning may briefly transmit after band selection.",
            style="Muted.TLabel",
        )
        self.ft8_info.pack(fill="x", pady=(0, 8))
        self.decode_tree = self.table(
            frame,
            ("utc", "hz", "message", "snr", "country"),
            ("UTC", "Audio Hz", "Message", "dB", "Country / state"),
            (85, 85, 510, 55, 170),
        )
        self.decode_tree.tag_configure(
            "own", background="#594325", foreground="#ffe3b0"
        )
        self.decode_tree.tag_configure("tx", background="#173e5a", foreground="#9edfff")
        self.decode_tree.tag_configure(
            "activation", background="#174d36", foreground="#a4eed0"
        )
        self.decode_tree.tag_configure("alternate", background="#1b303b")
        self.decode_tree.bind(
            "<Return>", lambda _: self.safe(lambda: self.ft8_action("reply")) or "break"
        )
        self.decode_tree.bind("<Down>", self.decode_down)
        self.decode_tree.bind("<Up>", self.decode_up)
        self.decode_tree.bind("<Control-End>", lambda _: self.follow_decodes())
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(8, 0))
        self.button(footer, "Reply / continue", lambda: self.ft8_action("reply")).pack(
            side="left"
        )
        self.button(footer, "Record selected QSO", self.record_decode).pack(
            side="left", padx=6
        )
        self.button(footer, "Follow newest", self.follow_decodes).pack(side="left")
        self.button(
            footer, "Clear warning", lambda: self.sessions.action("clear_fault")
        ).pack(side="right")
        self.ft8_progress = ttk.Progressbar(frame, maximum=1)
        self.ft8_progress.pack(fill="x", pady=(9, 0))

    def table(self, parent, columns, titles, widths):
        box = self.ttk.Frame(parent)
        box.pack(fill="both", expand=True)
        tree = self.ttk.Treeview(
            box, columns=columns, show="headings", selectmode="browse"
        )
        scroll = self.ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for key, title, width in zip(columns, titles, widths):
            tree.heading(key, text=title)
            tree.column(
                key,
                width=width,
                minwidth=min(width, 70),
                stretch=key in ("message", "call"),
            )
        return tree

    def change_ft8_band(self):
        hz = next(
            hz for name, hz in self.sessions.bands() if name == self.ft8_band.get()
        )
        self.sessions.set_band(hz)

    def selected_decode(self):
        selected = self.decode_tree.selection()
        return self.decode_rows.get(selected[0]) if selected else None

    def ft8_action(self, action):
        message = self.selected_decode() if action == "reply" else None
        if action == "reply" and message is None:
            raise ValueError("Select a received decode to reply")
        if (
            action in ("auto_cq", "pota_hunt")
            and self.sessions.station
            and self.sessions.station.auto_active
        ):
            self.sessions.stop_tx()
            return
        self.sessions.action(action, message)
        if action == "reply":
            self.follow_decodes()

    def follow_decodes(self):
        self.decode_tree.selection_remove(*self.decode_tree.selection())
        children = self.decode_tree.get_children()
        if children:
            self.decode_tree.see(children[-1])
            self.decode_tree.focus(children[-1])
        return "break"

    def decode_down(self, _):
        children = self.decode_tree.get_children()
        selected = self.decode_tree.selection()
        if children and selected and selected[0] == children[-1]:
            return self.follow_decodes()

    def decode_up(self, _):
        children = self.decode_tree.get_children()
        if children and not self.decode_tree.selection():
            self.decode_tree.selection_set(children[-1])
            self.decode_tree.focus(children[-1])
            self.decode_tree.see(children[-1])
            return "break"

    def record_decode(self):
        message = self.selected_decode()
        if not message:
            raise ValueError("Select a decode first")
        draft = contact_draft(
            load_settings(),
            self.sessions.decoder.snapshot()[0],
            message,
            self.sessions.dial,
        )
        self.manual_dialog(draft)

    def build_log(self):
        tk, ttk = self.tk, self.ttk
        frame = self.frames["log"]
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 10))
        self.button(
            bar, "+ New entry", self.manual_dialog, style="Accent.TButton"
        ).pack(side="left")
        self.search = tk.StringVar()
        search = ttk.Entry(bar, textvariable=self.search, width=24)
        search.pack(side="left", padx=(16, 5))
        self.label(bar, "Search callsign", style="Muted.TLabel").pack(side="left")
        self.search.trace_add("write", lambda *_: self.search_log())
        self.log_service = tk.StringVar(value="Wavelog")
        ttk.Combobox(
            bar,
            textvariable=self.log_service,
            values=("Wavelog", "QRZ"),
            state="readonly",
            width=10,
        ).pack(side="right")
        self.button(bar, "Sync log", self.sync_log).pack(side="right", padx=8)
        self.log_tree = self.table(
            frame,
            ("date", "call", "band", "mode", "freq", "sent", "received"),
            ("UTC date / time", "Callsign", "Band", "Mode", "MHz", "Sent", "Received"),
            (195, 190, 80, 90, 110, 80, 90),
        )
        self.log_tree.bind("<Return>", lambda _: self.show_contact())
        self.log_tree.bind("<Double-1>", lambda _: self.show_contact())
        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", pady=(10, 0))
        self.button(bottom, "Previous", lambda: self.page_log(-1)).pack(side="left")
        self.button(bottom, "Next", lambda: self.page_log(1)).pack(side="left", padx=6)
        self.log_count = self.label(bottom, "0 contacts", style="Muted.TLabel")
        self.log_count.pack(side="left", padx=12)
        self.button(bottom, "Retry uploads", self.book.sync).pack(side="right")
        self.log_status = self.label(frame, "", style="Muted.TLabel")
        self.log_status.pack(fill="x", pady=(8, 0))
        self.refresh_log(force=True)

    def search_log(self):
        self.log_offset = 0
        self.last_log = 0

    def page_log(self, direction):
        self.log_offset = max(
            0,
            min(
                max(0, (self.log_total - 1) // 100 * 100),
                self.log_offset + direction * 100,
            ),
        )
        self.refresh_log(force=True)

    def refresh_log(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_log < 1:
            return
        self.last_log = now
        rows, total = self.book.page(self.log_offset, 100, self.search.get())
        key = (rows, total, self.log_offset)
        if not force and key == getattr(self, "log_key", None):
            return
        self.log_key = key
        self.log_total = total
        selected = self.log_tree.selection()
        self.log_tree.delete(*self.log_tree.get_children())
        self.log_rows = {}
        for q in rows:
            iid = q["id"]
            self.log_rows[iid] = q
            self.log_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    datetime.fromtimestamp(q["start"], timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    q["call"],
                    q["band"],
                    q.get("mode", "FT8"),
                    f"{q['dial']/1e6:g}" if q["dial"] else "—",
                    report_text(q.get("sent")),
                    report_text(q.get("received")),
                ),
            )
        if selected and selected[0] in self.log_rows:
            self.log_tree.selection_set(selected[0])
        self.log_count.configure(
            text=f"{total:,} contacts  ·  {self.log_offset+1 if total else 0}–{min(total,self.log_offset+100)}"
        )

    def sync_log(self):
        service = self.log_service.get().lower()
        self.book.sync_contacts(service, load_settings())
        self.notify(f"{service.title()} sync started; the radio session continues")

    def show_contact(self):
        selected = self.log_tree.selection()
        if not selected:
            return "break"
        q = self.log_rows[selected[0]]
        dialog = self.dialog(q["call"])
        data = [
            (
                "UTC",
                datetime.fromtimestamp(q["start"], timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            ),
            ("Station", q["own_call"]),
            ("Band / mode", q["band"] + " / " + q.get("mode", "FT8")),
            ("Frequency", f"{q['dial']/1e6:g} MHz" if q["dial"] else "Not recorded"),
            ("Grid", q.get("grid", "")),
            (
                "Reports",
                f"Sent {report_text(q.get('sent'))} · Received {report_text(q.get('received'))}",
            ),
            (
                "Uploads",
                " · ".join(
                    f"{k}: {v}" for k, v in self.book.delivery_states(q["id"]).items()
                )
                or "Onboard only",
            ),
            ("Notes", q.get("comment", "")),
        ]
        body = self.ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        for row, (label, value) in enumerate(data):
            self.label(body, label, style="Muted.TLabel").grid(
                row=row, column=0, sticky="nw", padx=(0, 24), pady=6
            )
            self.label(body, str(value), wraplength=440).grid(
                row=row, column=1, sticky="w", pady=6
            )
        self.button(body, "Close", dialog.destroy).grid(
            row=len(data), column=1, sticky="e", pady=(18, 0)
        )
        self.decorate(dialog)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        return "break"

    def dialog(self, title):
        dialog = self.tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        return dialog

    def manual_dialog(self, draft=None):
        settings = load_settings()
        if not settings.get("callsign"):
            raise ValueError("Set your callsign in Settings first")
        now = (
            datetime.now(timezone.utc)
            if draft is None
            else datetime.fromtimestamp(draft["start"], timezone.utc)
        )
        initial = dict(
            call="",
            grid="",
            date=now.strftime("%Y-%m-%d"),
            time=now.strftime("%H:%M:%S"),
            frequency="7.074",
            mode="FT8",
            sent="",
            received="",
            comment="",
        )
        if draft:
            initial.update(
                call=draft["call"],
                grid=draft.get("grid", ""),
                frequency=f"{draft['dial']/1e6:g}",
                sent="" if draft["sent"] is None else str(draft["sent"]),
                received="" if draft["received"] is None else str(draft["received"]),
            )
        dialog = self.dialog("Record QSO" if draft else "New log entry")
        body = self.ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        variables = {k: self.tk.StringVar(value=v) for k, v in initial.items()}
        fields = (
            ("Callsign", "call"),
            ("Grid (optional)", "grid"),
            ("UTC date", "date"),
            ("UTC time", "time"),
            ("Frequency MHz", "frequency"),
            ("Mode", "mode"),
            ("Sent report", "sent"),
            ("Received report", "received"),
            ("Notes", "comment"),
        )
        first = None
        for row, (label, key) in enumerate(fields):
            self.label(body, label).grid(
                row=row, column=0, sticky="w", padx=(0, 18), pady=5
            )
            if key == "mode":
                widget = self.ttk.Combobox(
                    body,
                    textvariable=variables[key],
                    values=MODES,
                    state="disabled" if draft else "readonly",
                    width=33,
                )
            else:
                widget = self.ttk.Entry(
                    body,
                    textvariable=variables[key],
                    width=36,
                    state=(
                        "readonly"
                        if draft and key in ("date", "time", "frequency")
                        else "normal"
                    ),
                )
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            if first is None:
                first = widget
        error = self.label(body, "", foreground=RED, wraplength=450)
        error.grid(row=9, column=0, columnspan=2, sticky="w", pady=8)

        def save():
            try:
                form = {k: v.get() for k, v in variables.items()}
                q = (
                    finalize_contact(
                        {
                            **draft,
                            **{
                                k: form[k]
                                for k in ("call", "grid", "sent", "received", "comment")
                            },
                        }
                    )
                    if draft
                    else manual_contact(form, load_settings())
                )
                added = self.book.add(q)
                self.notify(("Saved " if added else "Already logged ") + q["call"])
                dialog.destroy()
                self.refresh_log(force=True)
            except (ValueError, OSError) as exc:
                error.configure(text=str(exc))

        actions = self.ttk.Frame(body)
        actions.grid(row=10, column=0, columnspan=2, sticky="e")
        self.button(actions, "Cancel", dialog.destroy).pack(side="left", padx=6)
        self.button(actions, "Save contact", save, style="Accent.TButton").pack(
            side="left"
        )
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        self.decorate(dialog)
        first.focus_set()

    def build_settings(self):
        tk, ttk = self.tk, self.ttk
        frame = self.frames["settings"]
        self.settings_tabs = ttk.Notebook(frame)
        self.settings_tabs.pack(fill="both", expand=True)
        radio = ttk.Frame(self.settings_tabs, padding=16)
        station = ttk.Frame(self.settings_tabs, padding=16)
        logging = ttk.Frame(self.settings_tabs, padding=16)
        for child, title in (
            (radio, "Radio profiles"),
            (station, "Station & FT8"),
            (logging, "Logging & updates"),
        ):
            self.settings_tabs.add(child, text=title)
        top = ttk.Frame(radio)
        top.pack(fill="x")
        self.profile_choice = tk.StringVar()
        self.profile_combo = ttk.Combobox(
            top, textvariable=self.profile_choice, width=35, state="readonly"
        )
        self.profile_combo.pack(side="left")
        self.profile_combo.bind("<<ComboboxSelected>>", lambda _: self.load_profile())
        self.button(top, "New", lambda: self.new_profile(False)).pack(
            side="left", padx=8
        )
        self.button(top, "Clone", lambda: self.new_profile(True)).pack(side="left")
        self.profile_fields = {key: tk.StringVar() for key in ("Name",) + FIELDS}
        form = ttk.Frame(radio)
        form.pack(fill="x", pady=14)
        for row, (key, var) in enumerate(self.profile_fields.items()):
            self.label(form, key, style="Muted.TLabel").grid(
                row=row, column=0, sticky="w", pady=4, padx=(0, 24)
            )
            if key in ("Radio", "Login"):
                widget = ttk.Combobox(
                    form,
                    textvariable=var,
                    values=RADIO_CHOICES if key == "Radio" else ("User", "Admin"),
                    width=36,
                    state="readonly",
                )
            else:
                widget = ttk.Entry(
                    form,
                    textvariable=var,
                    width=39,
                    show="•" if key == "Password" else "",
                )
            widget.grid(row=row, column=1, sticky="w", pady=4)
        buttons = ttk.Frame(radio)
        buttons.pack(fill="x")
        self.save_profile_button = self.button(
            buttons, "Save connection", self.save_profile, style="Accent.TButton"
        )
        self.save_profile_button.pack(side="left")
        self.use_profile_button = self.button(buttons, "Use profile", self.use_profile)
        self.use_profile_button.pack(side="left", padx=8)
        self.delete_profile_button = self.button(buttons, "Delete", self.delete_profile)
        self.delete_profile_button.pack(side="left")
        self.label(
            radio,
            "Connection changes require disconnecting. Viewing this tab never interrupts a session.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=12)
        self.refresh_profiles()
        settings = load_settings()
        self.station_vars = {}
        fields = (
            ("Call sign", "callsign"),
            ("Maidenhead grid", "grid"),
            ("TX audio Hz", "tx_hz"),
            ("TX level %", "tx_level"),
            ("TX power W", "tx_power_w"),
            ("CQ period", "cq_period"),
        )
        for row, (label, key) in enumerate(fields):
            self.label(station, label, style="Muted.TLabel").grid(
                row=row, column=0, sticky="w", pady=7, padx=(0, 24)
            )
            var = tk.StringVar(value=str(settings[key]))
            self.station_vars[key] = var
            widget = (
                ttk.Combobox(
                    station,
                    textvariable=var,
                    values=("Even", "Odd"),
                    state="readonly",
                    width=29,
                )
                if key == "cq_period"
                else ttk.Entry(station, textvariable=var, width=32)
            )
            widget.grid(row=row, column=1, sticky="w", pady=7)
        for row, key, label in (
            (6, "qso_beep", "QSO saved beep"),
            (7, "hide_worked", "Hide previous FT8 contacts on current band"),
        ):
            var = tk.BooleanVar(value=settings[key])
            self.station_vars[key] = var
            ttk.Checkbutton(station, text=label, variable=var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=6
            )
        self.save_station_button = self.button(
            station, "Save station settings", self.save_station, style="Accent.TButton"
        )
        self.save_station_button.grid(
            row=8, column=0, columnspan=2, sticky="w", pady=15
        )
        self.label(
            station,
            "Stop TX before changing identity or transmit parameters. Drafts stay here while you switch tabs.",
            style="Muted.TLabel",
            wraplength=780,
        ).grid(row=9, column=0, columnspan=3, sticky="w")
        self.logging_vars = {}
        fields = (
            ("Wavelog API URL", "wavelog_url"),
            ("Wavelog API key", "wavelog_key"),
            ("Wavelog Station ID", "wavelog_station"),
            ("QRZ API key", "qrz_key"),
        )
        for row, (label, key) in enumerate(fields):
            self.label(logging, label, style="Muted.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, 20), pady=5
            )
            var = tk.StringVar(value=settings[key])
            self.logging_vars[key] = var
            ttk.Entry(
                logging,
                textvariable=var,
                width=52,
                show="•" if key.endswith("_key") else "",
            ).grid(row=row, column=1, sticky="w", pady=5)
        toggles = ttk.Frame(logging)
        toggles.grid(row=4, column=0, columnspan=2, sticky="w", pady=7)
        for key, label in (
            ("wavelog_enabled", "Wavelog enabled"),
            ("qrz_enabled", "QRZ enabled"),
        ):
            var = tk.BooleanVar(value=settings[key])
            self.logging_vars[key] = var
            ttk.Checkbutton(toggles, text=label, variable=var).pack(
                side="left", padx=(0, 20)
            )
        actions = ttk.Frame(logging)
        actions.grid(row=5, column=0, columnspan=2, sticky="w", pady=8)
        self.button(
            actions, "Save logging settings", self.save_logging, style="Accent.TButton"
        ).pack(side="left")
        self.button(
            actions, "Test Wavelog", lambda: self.test_provider("wavelog")
        ).pack(side="left", padx=8)
        self.button(actions, "Test QRZ", lambda: self.test_provider("qrz")).pack(
            side="left"
        )
        self.provider_status = self.label(logging, "", style="Muted.TLabel")
        self.provider_status.grid(row=6, column=0, columnspan=2, sticky="w")
        updates = ttk.Frame(logging)
        updates.grid(row=0, column=2, rowspan=11, sticky="nw", padx=(30, 0))
        self.label(
            updates, "Application updates", font=("DejaVu Sans", 12, "bold")
        ).pack(anchor="w", pady=(0, 12))
        updatebar = ttk.Frame(updates)
        updatebar.pack(fill="x")
        self.button(updatebar, "Check for updates", self.update.check).pack(
            fill="x", pady=4
        )
        self.download_button = self.button(updatebar, "Download", self.update.download)
        self.download_button.pack(fill="x", pady=4)
        self.install_button = self.button(
            updatebar, "Install / restart", self.install_update
        )
        self.install_button.pack(fill="x", pady=4)
        self.check_launch = tk.BooleanVar(
            value=self.store.read().get("check_updates", True)
        )
        ttk.Checkbutton(
            updates,
            text="Check for updates at launch",
            variable=self.check_launch,
            command=lambda: self.store.preference(
                "check_updates", self.check_launch.get()
            ),
        ).pack(anchor="w", pady=10)
        self.update_label = self.label(
            updates, "", style="Muted.TLabel", wraplength=260
        )
        self.update_label.pack(anchor="w", pady=8)
        self.update_progress = ttk.Progressbar(updates, maximum=1, length=260)
        self.update_progress.pack(fill="x")

    def refresh_profiles(self, select=None):
        data = self.store.read()
        self.profiles = {r["name"]: r for r in data["radios"]}
        self.profile_combo.configure(values=list(self.profiles))
        chosen = next(
            (r for r in data["radios"] if r["id"] == select), self.store.active(data)
        )
        self.profile_choice.set(chosen["name"])
        self.load_profile()

    def load_profile(self):
        radio = self.profiles[self.profile_choice.get()]
        self.edit_profile_id = radio["id"]
        self.profile_fields["Name"].set(radio["name"])
        for key in FIELDS:
            self.profile_fields[key].set(radio["connection"][key])

    def require_disconnected(self):
        if self.sessions.switching or self.sessions.mode is not None:
            raise ValueError(
                "Disconnect the radio session before changing its connection"
            )

    def new_profile(self, clone):
        new = self.store.add(self.edit_profile_id if clone else None)
        self.refresh_profiles(new)
        self.notify("Profile draft created; save it, then use it while disconnected")

    def save_profile(self):
        self.require_disconnected()
        values = [
            (
                self.profile_fields[key].get().strip()
                if key != "Password"
                else self.profile_fields[key].get()
            )
            for key in FIELDS
        ]
        self.app["connection"](values)
        self.store.update(
            self.edit_profile_id,
            name=self.profile_fields["Name"].get(),
            connection=dict(zip(FIELDS, values)),
        )
        self.refresh_profiles(self.edit_profile_id)
        self.notify("Radio profile saved")

    def use_profile(self):
        self.require_disconnected()
        self.store.select(self.edit_profile_id)
        self.notify("Active radio profile changed")

    def delete_profile(self):
        self.require_disconnected()
        if self.messagebox.askyesno(
            "Delete radio profile", "Delete this connection profile?", parent=self.root
        ):
            self.store.delete(self.edit_profile_id)
            self.refresh_profiles()

    def save_station(self):
        if self.sessions.armed:
            raise ValueError("Stop TX and tuning before saving station settings")
        settings = load_settings()
        settings.update({k: v.get() for k, v in self.station_vars.items()})
        settings["callsign"] = settings["callsign"].strip().upper()
        settings["grid"] = settings["grid"].strip().upper()
        validate_station(settings)
        save_settings(settings)
        self.notify("Station settings saved")

    def save_logging(self):
        if self.book.contact_sync.busy:
            raise ValueError("Wait for log sync before changing accounts")
        settings = load_settings()
        settings.update(
            {
                k: v.get().strip() if isinstance(v.get(), str) else v.get()
                for k, v in self.logging_vars.items()
            }
        )
        if settings["wavelog_url"] and not settings["wavelog_url"].startswith(
            "https://"
        ):
            raise ValueError("Wavelog URL must use HTTPS")
        save_settings(settings)
        self.notify("Logging settings saved; current radio session continues")

    def test_provider(self, service):
        self.provider_status.configure(text="Checking " + service.title() + "…")
        values = load_settings()
        self.provider_result = None

        def work():
            try:
                self.provider_result = test_service(service, values)
            except Exception as error:
                self.provider_result = (
                    type(error).__name__ + " · check settings/subscription"
                )

        threading.Thread(target=work, daemon=True).start()

    def install_update(self):
        if self.update.state == "done":
            self.restart = True
            self.close()
            return
        self.require_disconnected()
        from tkinter import simpledialog

        password = simpledialog.askstring(
            "Install update",
            "uConsole administrator password:",
            show="•",
            parent=self.root,
        )
        if password is not None:
            self.update.install(password)

    def draw_spectrum(self):
        rx = self.sessions.rx
        if not rx or self.sessions.mode != "radio":
            return
        canvas = self.radio_canvas
        width = max(20, canvas.winfo_width() - 32)
        height = max(30, canvas.winfo_height() - 125)
        scope = rx.scope if rx.scope and time.monotonic() - rx.scope[0] < 2 else None
        if scope is None:
            if getattr(self, "scope_waiting", False):
                return
            self.scope_waiting = True
            canvas.delete("all")
            canvas.create_text(
                30,
                35,
                text=rx.status,
                anchor="nw",
                fill=MUTED,
                font=("DejaVu Sans", 11),
            )
            return
        self.scope_waiting = False
        if not self.spectrum.update(
            scope, rx.displayed_frequency(), width, height, self.zoom
        ):
            return
        canvas.delete("all")
        for frac in (0, 0.25, 0.5, 0.75, 1):
            x = 16 + frac * (width - 1)
            canvas.create_line(x, 8, x, 94, fill="#29414e")
        for y in (24, 48, 72, 94):
            canvas.create_line(16, y, 16 + width, y, fill="#20323e")
        levels = self.spectrum.levels
        coords = [
            value
            for x, level in enumerate(levels)
            for value in (16 + x, 94 - min(float(level), 160) * 0.52)
        ]
        canvas.create_line(*coords, fill=GREEN, width=1.3)
        self.scope_photo = self.ImageTk.PhotoImage(
            self.spectrum.image, master=self.root
        )
        canvas.create_image(16, 100, image=self.scope_photo, anchor="nw")
        start, end = self.spectrum.edges
        for frac, anchor in ((0, "w"), (0.5, "center"), (1, "e")):
            canvas.create_text(
                16 + frac * (width - 1),
                108 + height,
                text=f"{(start+frac*(end-start))/1e6:.4f} MHz",
                anchor=anchor,
                fill=MUTED,
                font=("DejaVu Sans", 10),
            )
        freq = rx.displayed_frequency()
        if freq and start <= freq <= end:
            x = 16 + (freq - start) * (width - 1) / (end - start)
            canvas.create_line(x, 8, x, 100 + height, fill="#edbf79", width=1)

    def refresh_cw(self):
        if not self.sessions.cw:
            return
        from cw import Scrollback

        if not hasattr(self, "cw_history"):
            self.cw_history = Scrollback(columns=120, rows=5)
        state = self.sessions.cw.snapshot()
        self.cw_history.update(
            state.get("text", ""),
            (id(self.sessions.cw), state.get("revision")),
            state.get("offset", 0),
        )
        text = self.cw_history.text
        if text != getattr(self, "cw_contents", None):
            self.cw_contents = text
            bottom = self.cw_text.yview()[1] >= 0.98
            position = self.cw_text.yview()[0]
            self.cw_text.configure(state="normal")
            self.cw_text.delete("1.0", "end")
            self.cw_text.insert("1.0", text)
            self.cw_text.configure(state="disabled")
            self.cw_text.see("end") if bottom else self.cw_text.yview_moveto(position)
        label = (
            f"CW · {state.get('pitch') or '—'} Hz · {state.get('wpm') or '—'} WPM"
            if self.cw_on.get()
            else "CW decoder off"
        )
        self.cw_label.configure(text=label)

    def refresh_decodes(self):
        s = self.sessions
        if not s.decoder:
            return
        messages = s.history.messages
        key = tuple((m.key, m.snr) for m in messages)
        if key != self.last_decodes:
            self.last_decodes = key
            selected = self.decode_tree.selection()
            following = not selected and self.decode_tree.yview()[1] >= 0.98
            position = self.decode_tree.yview()[0]
            wanted = []
            new = {}
            settings = load_settings()
            own = settings["callsign"]
            for i, m in enumerate(messages):
                iid = "d" + hashlib.sha1(repr(m.key).encode()).hexdigest()
                wanted.append(iid)
                new[iid] = m
                tag = (
                    "tx"
                    if m.local_tx
                    else (
                        "own"
                        if m.involves(own)
                        else (
                            "activation"
                            if activation_cq(m)
                            else ("alternate" if i % 2 else "")
                        )
                    )
                )
                values = (
                    datetime.fromtimestamp(m.slot * 15, timezone.utc).strftime(
                        "%H:%M:%S"
                    ),
                    m.hz,
                    m.text,
                    "TX" if m.local_tx else f"{m.snr:+d}",
                    short_country(message_country(m)),
                )
                if self.decode_tree.exists(iid):
                    self.decode_tree.item(iid, values=values, tags=(tag,))
                else:
                    self.decode_tree.insert(
                        "", "end", iid=iid, values=values, tags=(tag,)
                    )
            for iid in set(self.decode_rows) - set(new):
                if self.decode_tree.exists(iid):
                    self.decode_tree.delete(iid)
            for i, iid in enumerate(wanted):
                self.decode_tree.move(iid, "", i)
            self.decode_rows = new
            if selected and selected[0] in new:
                self.decode_tree.selection_set(selected[0])
                self.decode_tree.yview_moveto(position)
            elif following:
                self.follow_decodes()
            else:
                self.decode_tree.yview_moveto(position)
        _, status, progress = s.decoder.snapshot()
        self.ft8_progress.configure(
            value=progress,
            style=(
                "TX.Horizontal.TProgressbar"
                if s.rx.radio_state.get("ptt")
                else "Horizontal.TProgressbar"
            ),
        )
        if s.band_required:
            info = "Choose a designated FT8 band above before receive setup or tuning"
        elif s.station:
            info = (
                s.station.status + "  ·  " + s.station.clock_status + "  ·  " + status
            )
        else:
            info = status
        self.ft8_info.configure(text=info)
        if s.dial:
            name = next((name for name, hz in s.bands() if hz == s.dial), "")
            if name:
                self.ft8_band.set(name)
        self.ft8_bands.configure(values=[name for name, _ in s.bands()])
        if s.station and s.station.qso_revision != self.saved_revision:
            self.saved_revision = s.station.qso_revision
            if s.station.saved_qso:
                self.notify("✓ QSO saved · " + s.station.saved_qso["call"])

    def tick(self):
        try:
            if self.closing:
                return
            s = self.sessions
            s.tick()
            rx = s.rx
            if s.switching:
                label = "Switching radio session…"
            elif rx:
                label = s.mode.upper() + " · " + rx.status
            else:
                label = "No radio session · " + self.store.active()["name"]
            self.session_label.configure(text=label)
            self.radio_connect.configure(
                text="Disconnect" if s.mode == "radio" else "Connect Radio",
                state="disabled" if s.switching else "normal",
            )
            self.ft8_connect.configure(
                text="Disconnect" if s.mode == "ft8" else "Connect FT8",
                state="disabled" if s.switching else "normal",
            )
            disconnected = not s.switching and s.mode is None
            for button in (
                self.save_profile_button,
                self.use_profile_button,
                self.delete_profile_button,
            ):
                button.configure(state="normal" if disconnected else "disabled")
            self.save_station_button.configure(
                state="disabled" if s.armed else "normal"
            )
            fault = rx.radio_state.get("tx_fault") if rx else None
            if fault:
                self.notice.configure(text="TX stopped · " + str(fault), foreground=RED)
            elif s.error:
                self.notice.configure(text=s.error, foreground=RED)
            elif time.monotonic() > self.toast_until:
                self.notice.configure(
                    text="Ctrl+1–4 tabs · Ctrl+Space stop TX · Ctrl+Q quit",
                    foreground=MUTED,
                )
            if rx and not s.switching:
                hz = rx.displayed_frequency()
                if s.mode == "radio":
                    self.frequency.configure(
                        text=f"{hz/1e6:.6f} MHz" if hz else "Connecting…"
                    )
                    self.radio_mode.set(
                        {
                            0: "LSB",
                            1: "USB",
                            2: "AM",
                            3: "CW",
                            4: "RTTY",
                            5: "FM",
                            7: "CW-R",
                            8: "RTTY-R",
                        }.get(rx.radio_state.get("mode"), "USB")
                    )
                    bands = presets(
                        rx.radio_state.get("profile", DEFAULT_PROFILE),
                        cw=self.cw_on.get(),
                    )
                    self.radio_band_combo.configure(values=[name for name, _ in bands])
                    self.refresh_cw()
                    self.draw_spectrum()
                elif s.mode == "ft8":
                    self.refresh_decodes()
                state = rx.radio_state
                if state.get("ptt") or s.armed:
                    now = time.monotonic()
                    swr = (
                        state.get("swr")
                        if now - state.get("swr_at", 0) <= 1.2
                        else None
                    )
                    watts = (
                        state.get("watts_display", state.get("watts"))
                        if now - state.get("watts_at", 0) <= 1.2
                        else None
                    )
                    self.state_label.configure(
                        text=(
                            ("TX" if state.get("ptt") else "● Armed")
                            + f"  SWR {swr:.1f}"
                            if swr is not None
                            else ("TX" if state.get("ptt") else "● Armed")
                        ),
                        foreground=RED,
                    )
                    if watts is not None:
                        self.state_label.configure(
                            text=self.state_label.cget("text") + f"  {watts:.0f} W"
                        )
                else:
                    self.state_label.configure(text="Receive only", foreground=MUTED)
            else:
                self.state_label.configure(
                    text="Switching…" if s.switching else "Ready", foreground=MUTED
                )
            if self.current() == "log":
                self.refresh_log()
            if time.monotonic() - self.last_summary > 1:
                self.last_summary = time.monotonic()
                self.log_status.configure(
                    text=self.book.contact_sync.status
                    + "   ·   "
                    + " · ".join(self.book.summary())
                )
            if getattr(self, "provider_result", None) is not None:
                self.provider_status.configure(text=self.provider_result)
            state = self.update.state
            self.update_label.configure(
                text="Update: "
                + state
                + (" · " + self.update.error if self.update.error else "")
            )
            self.download_button.configure(
                state="normal" if state == "available" else "disabled"
            )
            self.install_button.configure(
                text="Restart app" if state == "done" else "Install update",
                state=(
                    "normal"
                    if state == "done" or (state == "ready" and disconnected)
                    else "disabled"
                ),
            )
            self.update_progress.configure(
                value=(
                    self.update.received / self.update.total if self.update.total else 0
                )
            )
        except Exception as error:
            self.last_tick_error = type(error).__name__
            self.notify("Interface update: " + type(error).__name__, error=True)
        finally:
            if not self.closing:
                self.next_tick = self.root.after(80, self.tick)

    def check_focus(self):
        if self.root.focus_displayof() is None:
            for callback in self.releases.values():
                self.root.after_cancel(callback)
            self.releases.clear()
            self.held.clear()

    def key_release(self, event):
        identity = event.keycode
        if identity in self.releases:
            self.root.after_cancel(self.releases.pop(identity))

        def finish():
            self.releases.pop(identity, None)
            self.held.pop(identity, None)

        self.releases[identity] = self.root.after(12, finish)

    def key_press(self, event):
        identity = event.keycode
        if identity in self.releases:
            self.root.after_cancel(self.releases.pop(identity))
        repeated = identity in self.held
        prior = self.held.get(identity)
        self.held[identity] = prior or "ordinary"
        control = bool(event.state & 4)
        if (
            event.keysym == "F11"
            or (control and event.keysym == "Return")
            or prior == "fullscreen"
        ):
            if not repeated:
                self.root.attributes(
                    "-fullscreen", not self.root.attributes("-fullscreen")
                )
            self.held[identity] = "fullscreen"
            return "break"
        if control and event.keysym.lower() == "q":
            self.close()
            return "break"
        if control and event.keysym == "space":
            self.safe(self.sessions.stop_tx)
            return "break"
        if control and event.keysym in ("1", "2", "3", "4"):
            self.tabs.select(int(event.keysym) - 1)
            return "break"
        if control and event.keysym in ("Tab", "ISO_Left_Tab"):
            if not repeated:
                self.tabs.select(
                    (self.tabs.index("current") + (-1 if event.state & 1 else 1)) % 4
                )
            return "break"
        if self.closing:
            return "break"
        kind = event.widget.winfo_class()
        if kind in (
            "Entry",
            "TEntry",
            "Text",
            "TCombobox",
            "Spinbox",
            "TSpinbox",
            "TNotebook",
        ):
            return None
        view = self.current()
        if (
            view == "ft8"
            and event.keysym == "Return"
            and self.sessions.rx
            and self.sessions.rx.radio_state.get("tx_fault")
        ):
            self.safe(lambda: self.sessions.action("clear_fault"))
            return "break"
        if kind in ("TButton", "Button") and event.keysym == "Return":
            if not repeated:
                event.widget.invoke()
            return "break"
        if view == "ft8" and event.keysym == "Return" and repeated:
            return "break"
        if view != "radio":
            return None
        if control and event.keysym in ("Up", "Down"):
            if not repeated:
                self.change_zoom(1 if event.keysym == "Up" else -1)
            return "break"
        if control and event.keysym in ("Left", "Right"):
            self.volume.set(
                min(
                    100,
                    max(0, self.volume.get() + (5 if event.keysym == "Right" else -5)),
                )
            )
            self.muted.set(False)
            self.set_audio()
            return "break"
        if control and event.keysym.lower() == "m":
            if not repeated:
                self.muted.set(not self.muted.get())
                self.set_audio()
            return "break"
        if event.keysym in ("Left", "Right"):
            now = time.monotonic()
            if now - self.last_tune > 0.085:
                self.tune(-1 if event.keysym == "Left" else 1)
                self.last_tune = now
            return "break"
        if event.keysym in ("Up", "Down") and self.cw_on.get():
            self.cw_text.yview_scroll(-1 if event.keysym == "Up" else 1, "units")
            return "break"
        if not control and not repeated:
            key = event.keysym.lower()
            if key == "w":
                self.cw_on.set(not self.cw_on.get())
                self.cw_changed()
                return "break"
            if key == "n":
                self.safe(lambda: self.radio_command("nr"))
                return "break"
            if key == "t":
                self.safe(lambda: self.radio_command("tuner"))
                return "break"
            if key == "m":
                self.radio_mode.set("USB" if self.radio_mode.get() == "LSB" else "LSB")
                self.safe(self.set_mode)
                return "break"
            if key == "b":
                self.radio_band_combo.focus_set()
                return "break"
            if key == "s":
                self.tabs.select(self.frames["settings"])
                return "break"
        if event.keysym == "Return":
            if not repeated:
                self.safe(lambda: self.connect("radio"))
            return "break"
        return None

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.audio_save:
            self.root.after_cancel(self.audio_save)
            self.save_audio()
        self.session_label.configure(text="Stopping session…")

        def stop():
            try:
                self.sessions.close()
            except Exception:
                self.sessions.stop_tx()

        self.stop_worker = threading.Thread(target=stop, daemon=True)
        self.stop_worker.start()
        self.root.after(80, self.finish_close)

    def finish_close(self):
        if self.stop_worker.is_alive() or self.update.state == "installing":
            self.root.after(80, self.finish_close)
            return
        self.update.cancel()
        self.book.close()
        self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.close()
            self.root.mainloop()
        if getattr(self, "restart", False):
            import os, sys

            os.execv(
                sys.executable, [sys.executable, self.app["__file__"], "--desktop"]
            )
