"""Keyboard-first Linux desktop adapter for the shared ICOMzero screens.

Tk lives on the main thread. The existing bounded UI polls pump its event loop;
radio/decoder workers never touch Tk or the keyboard queue.
"""

from collections import deque
import time
from PIL import Image

TEXT_BASE = 0x100000
SPECIAL = {
    "F1": 0x7000,
    "F2": 0x7001,
    "F3": 0x7002,
    "F4": 0x7003,
    "Escape": 1,
    "Return": 28,
    "KP_Enter": 28,
    "Tab": 15,
    "ISO_Left_Tab": 15,
    "BackSpace": 14,
    "Delete": 111,
    "Home": 102,
    "End": 107,
    "Up": 33,
    "Down": 45,
    "Left": 105,
    "Right": 106,
    "Shift_L": 42,
    "Shift_R": 54,
    "Control_L": 389,
    "Control_R": 389,
}
LETTERS = {
    c: i
    for start, row in ((16, "qwertyuiop"), (30, "asdfghjkl"), (44, "zxcvbnm"))
    for i, c in enumerate(row, start)
}
DIGITS = {c: i for i, c in enumerate("1234567890", 2)}


def key_code(keysym, char="", text_input=False):
    if text_input and keysym in ("Up", "Down"):
        return 103 if keysym == "Up" else 108
    if keysym in SPECIAL:
        return SPECIAL[keysym]
    if text_input and len(char) == 1 and char.isprintable() and ord(char) <= 0x10FFFF:
        return TEXT_BASE + ord(char)
    key = keysym.lower()
    if key in LETTERS:
        return LETTERS[key]
    if key in DIGITS:
        return DIGITS[key]
    if len(char) == 1 and char.isprintable() and ord(char) <= 0x10FFFF:
        return TEXT_BASE + ord(char)
    return None


def viewport(width, height):
    scale = max(0.1, min(width / 320, height / 170))
    w, h = max(1, round(320 * scale)), max(1, round(170 * scale))
    return (width - w) // 2, (height - h) // 2, w, h


class DesktopClosed(KeyboardInterrupt):
    pass


class Context:
    def __init__(self, app, fullscreen=False):
        import tkinter as tk
        from PIL import ImageTk

        self.app, self.tk, self.ImageTk = app, tk, ImageTk
        self.event_flags = tk._tkinter.DONT_WAIT
        self.events = deque()
        self.held = {}
        self.releases = {}
        self.closing = False
        self.display = None
        self.image = self.photo = None
        self.root = tk.Tk(className="ICOMzero")
        self.root.title("ICOMzero · uConsole · " + app["VERSION"])
        self.root.configure(background="#101a21")
        self.root.minsize(640, 390)
        width = min(1100, self.root.winfo_screenwidth() - 60)
        height = min(round(width * 170 / 320) + 44, self.root.winfo_screenheight() - 70)
        self.root.geometry(f"{width}x{height}")
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.root.attributes("-fullscreen", fullscreen)
        self.canvas = tk.Canvas(
            self.root, background="#080f16", highlightthickness=0, takefocus=True
        )
        self.canvas.pack(fill="both", expand=True)
        bar = tk.Frame(self.root, background="#172731")
        bar.pack(fill="x")
        for label, code in (
            ("Back · Esc", 1),
            ("Previous · ↑", 33),
            ("Next · ↓", 45),
            ("Open · Enter", 28),
            ("Menu · Tab", 15),
        ):
            tk.Button(
                bar,
                text=label,
                command=lambda c=code: self.tap(c),
                takefocus=False,
                foreground="#d5ece8",
                background="#203642",
                activebackground="#31505b",
                relief="flat",
                padx=10,
                pady=7,
            ).pack(side="left", padx=2, pady=2)
        tk.Label(
            bar,
            text="Ctrl+↑/↓ zoom   ·   Ctrl+Enter fullscreen",
            background="#172731",
            foreground="#9bb2bb",
        ).pack(side="right", padx=10)
        self.canvas.bind("<Configure>", lambda _: self.paint())
        self.canvas.bind("<Button-1>", self.click)
        self.root.bind("<KeyPress>", self.press)
        self.root.bind("<KeyRelease>", self.release)
        self.root.bind("<FocusOut>", self.focus_out)
        self.root.bind("<FocusIn>", lambda _: self.canvas.focus_set())
        self.canvas.focus_set()

    def request_close(self):
        self.closing = True
        self.release_all()

    def tap(self, code):
        self.events.extend(((code, 1), (code, 0)))
        self.canvas.focus_set()

    def press(self, event):
        identity = event.keycode
        # X11 repeat sends Release/Press pairs; keep a held shortcut held too.
        if identity in self.releases:
            self.root.after_cancel(self.releases.pop(identity))
        if event.keysym == "F11" or (event.keysym == "Return" and event.state & 4):
            if identity not in self.held:
                self.held[identity] = 0
                self.root.attributes(
                    "-fullscreen", not self.root.attributes("-fullscreen")
                )
            return "break"
        if event.keysym.lower() == "q" and event.state & 4:
            self.request_close()
            return "break"
        code = self.held.get(identity)
        if code == 0:
            return "break"
        if code is None:
            text_input = bool(getattr(self.display, "text_input", False))
            if (
                text_input
                and event.state & 4
                and event.keysym not in ("Control_L", "Control_R")
            ):
                if event.keysym.lower() == "v":
                    try:
                        text = self.root.clipboard_get()[:4096]
                    except self.tk.TclError:
                        return "break"
                    for char in text:
                        if char.isprintable():
                            self.events.extend(
                                ((TEXT_BASE + ord(char), 1), (TEXT_BASE + ord(char), 0))
                            )
                return "break"
            commands = {
                "Up": 103,
                "Down": 108,
                "Left": 114,
                "Right": 115,
                "m": 113,
                "M": 113,
            }
            code = (
                commands.get(event.keysym)
                if event.state & 4 and not text_input
                else None
            )
            if code is None:
                code = key_code(event.keysym, event.char, text_input)
            if code is not None:
                self.held[identity] = code
                self.events.append((code, 1))
        elif code is not None:
            self.events.append((code, 2))
        return "break"

    def release(self, event):
        identity = event.keycode
        if identity in self.releases:
            self.root.after_cancel(self.releases[identity])
        self.releases[identity] = self.root.after(
            12, lambda: self.finish_release(identity)
        )
        return "break"

    def finish_release(self, identity):
        self.releases.pop(identity, None)
        code = self.held.pop(identity, None)
        if code:
            self.events.append((code, 0))

    def release_all(self):
        for callback in self.releases.values():
            self.root.after_cancel(callback)
        self.releases.clear()
        self.events.extend((code, 0) for code in self.held.values() if code)
        self.held.clear()

    def focus_out(self, _):
        self.release_all()

    def poll(self, _keyboard=None, timeout=0.05):
        deadline = time.monotonic() + timeout
        while True:
            if self.closing:
                raise DesktopClosed()
            # Match the hardware reader: deliver one event at a time so an
            # Enter opening a nested form cannot swallow already queued text.
            if self.events:
                return [self.events.popleft()]
            processed = self.root.tk.dooneevent(self.event_flags)
            if self.closing:
                raise DesktopClosed()
            if self.events:
                return [self.events.popleft()]
            if time.monotonic() >= deadline:
                return []
            if not processed:
                time.sleep(min(0.005, max(0, deadline - time.monotonic())))

    def present(self, display, image):
        self.display, self.image = display, image
        self.paint()

    def paint(self):
        if self.image is None or self.closing:
            return
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width < 2 or height < 2:
            return
        x, y, w, h = viewport(width, height)
        image = self.image.resize((w, h), Image.Resampling.BICUBIC)
        self.photo = self.ImageTk.PhotoImage(image, master=self.root)
        self.canvas.delete("frame")
        self.canvas.create_image(x, y, image=self.photo, anchor="nw", tags="frame")

    def click(self, event):
        self.canvas.focus_set()
        if not self.display:
            return
        x, y, w, h = viewport(self.canvas.winfo_width(), self.canvas.winfo_height())
        px, py = (event.x - x) * 320 / w, (event.y - y) * 170 / h
        for rect, events in getattr(self.display, "pointer_regions", ()):
            left, top, right, bottom = rect
            if left <= px <= right and top <= py <= bottom:
                self.events.extend(events)
                break

    def close(self):
        self.release_all()
        self.root.destroy()


def run_compact(app, fullscreen=False, setup=None):
    context = Context(app, fullscreen)
    base = app["Display"]

    class WindowDisplay(base):
        def __init__(self, _=None):
            self.init_graphics()
            self.text_input = False
            self.pointer_regions = []

        def present(self, image):
            context.present(self, image)

    original_display, original_keys = app["Display"], app["keys_ready"]
    # runpy/global dictionaries may differ: function globals are authoritative.
    namespaces = {
        id(f.__globals__): f.__globals__
        for f in (app["home"], app["ui"], app["ft8_ui"])
    }
    for namespace in namespaces.values():
        namespace["Display"], namespace["keys_ready"] = WindowDisplay, context.poll
    if setup:
        setup(context)
    try:
        app["home"](None, None)
    except DesktopClosed:
        pass
    finally:
        for namespace in namespaces.values():
            namespace["Display"], namespace["keys_ready"] = (
                original_display,
                original_keys,
            )
        context.close()


def run(app, fullscreen=False, setup=None):
    from desktop_ui import DesktopApp

    window = DesktopApp(app, fullscreen)
    if setup:
        setup(window)
    window.run()
