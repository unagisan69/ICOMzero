"""Main-menu preferences, named radio editing and explicit update screens."""

import time
import threading
from PIL import Image, ImageDraw
from models import RADIO_CHOICES
from profiles import ProfileStore, FIELDS, DEFAULTS
from update_session import UpdateSession
from version import VERSION

BG, CARD, TEXT, MUTED, ACCENT = "#101a21", "#1d2d36", "#f0f6f6", "#9bb2bb", "#70edbb"


class RestartApp(Exception):
    pass


class SettingsScreens:
    def __init__(self, display, keyboard, events, character, store=None):
        self.display, self.keyboard = display, keyboard
        self.events, self.character = events, character
        self.store = store or ProfileStore()

    def frame(self, title, subtitle="", footer="F / X select   Ent open   Esc back"):
        image = Image.new("RGB", (320, 170), BG)
        draw = ImageDraw.Draw(image)
        draw.text((12, 7), title, font=self.display.fonts["title"], fill=TEXT)
        self.text(draw, (12, 35), subtitle, MUTED)
        self.text(draw, (12, 153), footer, MUTED)
        return image, draw

    def text(self, draw, xy, text, color=TEXT, width=294):
        text = str(text)
        font = self.display.fonts["small"]
        while text and draw.textlength(text, font=font) > width:
            text = text[:-1]
        draw.text(xy, text, font=font, fill=color)

    def select(self, title, items, subtitle="", choice=0):
        choice = min(choice, len(items) - 1)
        painted = None
        while True:
            if painted != choice:
                image, draw = self.frame(title, subtitle)
                first = max(0, min(choice - 3, len(items) - 5))
                for row, i in enumerate(range(first, min(first + 5, len(items)))):
                    y = 54 + row * 18
                    if choice == i:
                        draw.rounded_rectangle(
                            (8, y - 1, 312, y + 17), 4, fill=CARD, outline=ACCENT
                        )
                    self.text(
                        draw,
                        (15, y),
                        items[i],
                        ACCENT if i == choice else TEXT,
                        width=290,
                    )
                self.display.present(image)
                painted = choice
            for key, value in self.events(self.keyboard):
                if not value:
                    continue
                if key in (1, 15):
                    return None
                if key in (33, 44, 103, 105):
                    choice = (choice - 1) % len(items)
                elif key in (45, 46, 108, 106):
                    choice = (choice + 1) % len(items)
                elif key == 28 and value == 1:
                    return choice

    def message(self, title, message):
        self.select(title, ["Back"], message)

    def input(self, title, initial="", *, secret=False, limit=256, numeric=False):
        value = str(initial)
        shift = set()
        painted = None
        while True:
            state = (value, bool(shift))
            if state != painted:
                image, draw = self.frame(
                    title, "Enter saves · Esc cancels", "Backspace erase   Del clear"
                )
                draw.rounded_rectangle((10, 64, 310, 111), 7, fill=CARD, outline=ACCENT)
                visible = ("•" * len(value)) if secret else value
                self.text(draw, (20, 80), visible[-36:] + "▏", ACCENT, width=280)
                self.display.present(image)
                painted = state
            for key, pressed in self.events(self.keyboard):
                if key in (42, 54):
                    (shift.add if pressed else shift.discard)(key)
                    continue
                if not pressed:
                    continue
                if key == 1:
                    return None
                if key == 28 and pressed == 1:
                    return value
                if key == 14:
                    value = value[:-1]
                elif key == 111:
                    value = ""
                else:
                    char = self.character(key, shift)
                    if (
                        char is not None
                        and len(value) < limit
                        and (not numeric or char.isdecimal())
                    ):
                        value += char

    def edit_radio(self, profile_id):
        choice = 0
        while True:
            radio = next(
                r for r in self.store.read()["radios"] if r["id"] == profile_id
            )
            connection = radio["connection"]
            labels = ["Name: " + radio["name"]]
            labels += [
                key
                + ": "
                + (
                    "•" * min(14, len(connection[key]))
                    if key == "Password"
                    else connection[key] or "(not set)"
                )
                for key in FIELDS
            ]
            selected = self.select(
                "Radio connection", labels + ["Back"], radio["name"], choice
            )
            if selected is None or selected == len(labels):
                return
            choice = selected
            try:
                if selected == 0:
                    name = self.input("Radio name", radio["name"], limit=32)
                    if name is not None:
                        self.store.update(profile_id, name=name)
                    continue
                key = FIELDS[selected - 1]
                if key == "Radio":
                    index = self.select(
                        "Radio model",
                        RADIO_CHOICES,
                        choice=(
                            RADIO_CHOICES.index(connection[key])
                            if connection[key] in RADIO_CHOICES
                            else 0
                        ),
                    )
                    if index is not None:
                        model = RADIO_CHOICES[index]
                        changes = {"Radio": model}
                        if model == "TS-890S" and connection["Control port"] == "50001":
                            changes["Control port"] = "60000"
                        elif (
                            model != "TS-890S" and connection["Control port"] == "60000"
                        ):
                            changes["Control port"] = "50001"
                        self.store.update(profile_id, connection=changes)
                elif key == "Login":
                    index = self.select(
                        "Radio login",
                        ["User", "Admin"],
                        choice=connection[key] == "Admin",
                    )
                    if index is not None:
                        self.store.update(
                            profile_id, connection={key: ("User", "Admin")[index]}
                        )
                else:
                    numeric = key in ("Control port", "Audio port")
                    limit = (
                        5
                        if numeric
                        else (
                            (99 if connection["Radio"] == "TS-890S" else 16)
                            if key in ("Username", "Password")
                            else 253
                        )
                    )
                    value = self.input(
                        key,
                        connection[key],
                        secret=key == "Password",
                        limit=limit,
                        numeric=numeric,
                    )
                    if value is not None:
                        if numeric and (
                            not value.isascii()
                            or not value.isdecimal()
                            or not 1 <= int(value) <= 65535
                        ):
                            raise ValueError("Use a port from 1 to 65535")
                        self.store.update(profile_id, connection={key: value})
            except ValueError as error:
                self.message("Check settings", str(error))

    def radios(self):
        choice = 0
        while True:
            data = self.store.read()
            radios = data["radios"]
            items = [
                ("● " if r["id"] == data["active_radio"] else "  ") + r["name"]
                for r in radios
            ] + ["+ Add radio", "Back"]
            selected = self.select(
                "Your radios", items, "Choose a profile to use or edit", choice
            )
            if selected is None or selected == len(radios) + 1:
                return
            choice = selected
            try:
                if selected == len(radios):
                    self.edit_radio(self.store.add())
                    continue
                radio = radios[selected]
                action = self.select(
                    radio["name"],
                    [
                        "Use this radio",
                        "Edit connection",
                        "Clone radio",
                        "Delete radio",
                        "Back",
                    ],
                    radio["connection"]["Radio"],
                )
                if action == 0:
                    self.store.select(radio["id"])
                elif action == 1:
                    self.edit_radio(radio["id"])
                elif action == 2:
                    self.edit_radio(self.store.add(radio["id"]))
                elif action == 3:
                    confirm = self.select(
                        "Delete profile?", ["Keep radio", "Delete radio"], radio["name"]
                    )
                    if confirm == 1:
                        self.store.delete(radio["id"])
            except ValueError as error:
                self.message("Check settings", str(error))

    def logging(self):
        from ft8log import load_settings, save_settings
        from contacts import callsign, grid

        choice = 0
        while True:
            settings = load_settings()
            selected = self.select(
                "Station & logging",
                [
                    "Call sign: " + (settings["callsign"] or "Set your call"),
                    "Grid: " + (settings["grid"] or "Not set"),
                    "Wavelog",
                    "QRZ",
                    "Back",
                ],
                "Shared by FT8 and the onboard log",
                choice,
            )
            if selected is None or selected == 4:
                return
            choice = selected
            if selected in (2, 3):
                self.logging_provider(("wavelog", "qrz")[selected - 2])
                continue
            key = ("callsign", "grid")[selected]
            value = self.input(
                "Your callsign" if selected == 0 else "Maidenhead grid",
                settings[key],
                limit=24,
            )
            if value is not None:
                try:
                    settings[key] = callsign(value) if selected == 0 else grid(value)
                    save_settings(settings)
                except ValueError as error:
                    self.message("Check station", str(error))

    def logging_provider(self, service):
        from ft8log import load_settings, save_settings, test_service

        fields = (
            [
                ("API URL", "wavelog_url"),
                ("API key", "wavelog_key"),
                ("Station ID", "wavelog_station"),
            ]
            if service == "wavelog"
            else [("API key", "qrz_key")]
        )
        choice = 0
        while True:
            settings = load_settings()
            items = [
                label
                + ": "
                + (
                    ("•" * min(12, len(settings[key])))
                    if key.endswith("_key")
                    else settings[key]
                )
                for label, key in fields
            ]
            items += [
                "Enabled: " + ("On" if settings[service + "_enabled"] else "Off"),
                "Test connection",
                "Back",
            ]
            selected = self.select(
                service.title(),
                items,
                "Private credentials stay on this device",
                choice,
            )
            if selected is None or selected == len(fields) + 2:
                return
            choice = selected
            if selected < len(fields):
                label, key = fields[selected]
                value = self.input(
                    label, settings[key], secret=key.endswith("_key"), limit=512
                )
                if value is not None:
                    settings[key] = value.strip()
                    save_settings(settings)
            elif selected == len(fields):
                settings[service + "_enabled"] = not settings[service + "_enabled"]
                save_settings(settings)
            else:
                result = self.check_logging_connection(service, settings)
                if result and service == "wavelog" and not settings["wavelog_station"]:
                    settings["wavelog_station"] = result.removeprefix("Station ")
                    save_settings(settings)

    def check_logging_connection(self, service, settings):
        from ft8log import test_service

        done = threading.Event()
        result = {}

        def check():
            try:
                result["value"] = test_service(service, dict(settings))
            except Exception as error:
                result["error"] = type(error).__name__
            finally:
                done.set()

        threading.Thread(target=check, daemon=True).start()
        image, _ = self.frame(
            "Checking connection",
            "Read-only check · Esc cancels",
            "No test QSO will be created",
        )
        self.display.present(image)
        while not done.is_set():
            for key, value in self.events(self.keyboard, 0.1):
                if value == 1 and key in (1, 15):
                    return None
        if "error" in result:
            self.message(
                "Connection failed", result["error"] + " · check settings/subscription"
            )
            return None
        self.message("Connected", result["value"])
        return result["value"]

    def general(self, update=None):
        choice = 0
        while True:
            data = self.store.read()
            items = [
                "Radios · " + self.store.active(data)["name"],
                "Check for updates",
                "Launch update check: "
                + ("On" if data.get("check_updates", True) else "Off"),
                "Station & logging",
                "About ICOMzero",
                "Back",
            ]
            selected = self.select("Settings", items, "ICOMzero " + VERSION, choice)
            if selected is None or selected == 5:
                return
            choice = selected
            if selected == 0:
                self.radios()
            elif selected == 1:
                update = update or UpdateSession()
                update.check()
                self.update(update)
            elif selected == 2:
                self.store.preference(
                    "check_updates", not data.get("check_updates", True)
                )
            elif selected == 3:
                self.logging()
            elif selected == 4:
                self.message(
                    "ICOMzero " + VERSION, "Direct radio · FT8 · GitHub updates"
                )

    def update(self, update):
        stage_names = {
            "check": "Checking GitHub",
            "manifest": "Reading checksum",
            "connect": "Connecting to download",
            "download": "Downloading update",
            "verify_download": "Verifying download",
            "authenticate": "Administrator access",
            "stage_package": "Preparing package",
            "verify_package": "Verifying package",
            "metadata": "Checking package",
            "prepare_install": "Preparing installation",
            "dependencies": "Getting dependencies",
            "installing": "Installing update",
            "finish": "Finishing",
        }
        while True:
            state = update.state
            release = update.updater.release
            latest = release[0] if release else ""
            image, draw = self.frame(
                "Software update",
                "Installed " + VERSION,
                "Esc - Later" if state != "installing" else "Installing · Please wait",
            )
            title = {
                "idle": "Check for an update",
                "offline": "Connect to check for updates",
                "checking": "Checking GitHub…",
                "current": "You are up to date",
                "available": "Update " + latest + " available",
                "downloading": stage_names.get(update.stage, "Downloading…"),
                "ready": "Download verified",
                "installing": stage_names.get(update.stage, "Installing…"),
                "done": "Update installed",
                "error": "Update unavailable",
            }.get(state, state)
            self.text(draw, (14, 65), title, ACCENT)
            if state in ("downloading", "installing"):
                ratio = (
                    update.received / update.total
                    if update.total and update.received is not None
                    else None
                )
                draw.rounded_rectangle((14, 91, 306, 104), 5, fill=CARD)
                if ratio is not None:
                    percent = max(0, min(1, ratio))
                    width = max(2, int(292 * percent))
                    draw.rounded_rectangle((14, 91, 14 + width, 104), 5, fill=ACCENT)
                    label = f"{percent:.0%}"
                    if update.stage == "download":
                        label += f" · {update.received/1024:.0f} / {update.total/1024:.0f} KB"
                else:
                    offset = int(time.monotonic() * 80) % 242
                    draw.rounded_rectangle(
                        (14 + offset, 91, 64 + offset, 104), 5, fill=ACCENT
                    )
                    label = "Please wait…"
                self.text(draw, (14, 115), label, MUTED)
            elif state == "available":
                self.text(draw, (14, 96), "Ent - Download update", TEXT)
            elif state == "ready":
                self.text(draw, (14, 94), "Ent - Install · device password required")
            elif state == "done":
                self.text(draw, (14, 96), "Ent - Restart ICOMzero", TEXT)
            elif state == "error":
                self.text(draw, (14, 94), update.error, MUTED)
                self.text(draw, (14, 115), "Ent - Retry check", TEXT)
            elif state in ("idle", "current", "offline"):
                self.text(draw, (14, 96), "Ent - Check again", TEXT)
            self.display.present(image)
            for key, value in self.events(self.keyboard):
                if value != 1:
                    continue
                if key in (1, 15) and state != "installing":
                    update.cancel()
                    return
                if key != 28:
                    continue
                if state in ("idle", "current", "error", "offline"):
                    update.check()
                elif state == "available":
                    update.download()
                elif state == "ready":
                    password = self.input("Device password", "", secret=True, limit=128)
                    if password is not None:
                        update.install(password)
                    password = None
                elif state == "done":
                    raise RestartApp()
