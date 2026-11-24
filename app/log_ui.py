"""Onboard log browser, manual contact entry and explicit provider sync."""

from datetime import datetime, timezone
import time
import textwrap
from PIL import Image, ImageDraw

from contacts import MODES, manual_contact
from ft8log import Logbook, load_settings, report_text
from settings_ui import SettingsScreens, BG, CARD, TEXT, MUTED, ACCENT


def fit(draw, text, font, width):
    text = str(text)
    while text and draw.textlength(text, font=font) > width:
        text = text[:-1]
    return text


def log_frame(display, rows, total, selected=0, offset=0, search="", status=""):
    display.pointer_regions = []
    image = Image.new("RGB", (320, 170), BG)
    draw = ImageDraw.Draw(image)
    font = display.fonts["small"]
    tiny = display.fonts.setdefault("log_meta", font.font_variant(size=10))
    draw.text((12, 6), "Logbook", font=display.fonts["title"], fill=TEXT)
    badge = f"{total:,} QSOs"
    width = draw.textlength(badge, font=font)
    draw.rounded_rectangle((302 - width - 12, 9, 309, 30), 7, fill="#173c49")
    draw.text((302 - width - 6, 12), badge, font=font, fill=ACCENT)
    subtitle = (
        f"Search: {search}" if search else (status or "Your contacts · newest first")
    )
    draw.text((12, 35), fit(draw, subtitle, tiny, 295), font=tiny, fill=MUTED)
    for row, qso in enumerate(rows):
        y = 51 + row * 24
        active = row + offset == selected
        delta = row + offset - selected
        display.pointer_regions.append(
            (
                (8, y, 312, y + 22),
                [(33 if delta < 0 else 45, 1)] * abs(delta) + [(28, 1), (28, 0)],
            )
        )
        draw.rounded_rectangle(
            (8, y, 312, y + 22),
            5,
            fill="#173c49" if active else CARD,
            outline=ACCENT if active else CARD,
        )
        draw.text((15, y + 1), qso["call"], font=font, fill=TEXT if active else ACCENT)
        label = f"{qso['band']}  {qso.get('mode','FT8')}"
        draw.text((304, y + 1), label, anchor="ra", font=tiny, fill=MUTED)
        date = datetime.fromtimestamp(qso["start"], timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        draw.text((15, y + 12), date, font=tiny, fill=MUTED)
        draw.text(
            (304, y + 12),
            (f"{qso['dial']/1e6:g} MHz" if qso["dial"] else "— MHz"),
            anchor="ra",
            font=tiny,
            fill=MUTED,
        )
    if not rows:
        draw.rounded_rectangle((12, 60, 308, 135), 9, fill=CARD)
        draw.text(
            (160, 74),
            "No matching contacts" if search else "Your next contact belongs here",
            anchor="ma",
            font=font,
            fill=TEXT,
        )
        draw.text(
            (160, 101),
            "Tab · add an entry or sync a log",
            anchor="ma",
            font=tiny,
            fill=MUTED,
        )
    draw.text((12, 153), "F / X browse   Ent view", font=font, fill=MUTED)
    draw.rounded_rectangle((229, 150, 311, 169), 5, fill=CARD)
    draw.text((236, 153), "Tab · Menu", font=font, fill=ACCENT)
    display.pointer_regions.append(((229, 150, 311, 169), [(15, 1), (15, 0)]))
    return image


class LogScreens(SettingsScreens):
    def __init__(self, display, keyboard, events, character, store=None, book=None):
        super().__init__(display, keyboard, events, character, store)
        self.book = book or Logbook()
        self.owns_book = book is None

    def browser(self):
        selected, search, status = 0, "", ""
        painted = None
        next_refresh = 0
        try:
            while True:
                now = time.monotonic()
                if now >= next_refresh:
                    # Four rows per page; imports are visible while sync runs.
                    offset = selected // 4 * 4
                    rows, total = self.book.page(offset, 4, search)
                    selected = min(selected, max(0, total - 1))
                    if offset != selected // 4 * 4:
                        continue
                    current_status = (
                        self.book.contact_sync.status
                        if self.book.contact_sync.thread is not None
                        else status
                    )
                    state = (rows, total, selected, search, current_status)
                    if state != painted:
                        self.display.present(
                            log_frame(
                                self.display,
                                rows,
                                total,
                                selected,
                                offset,
                                search,
                                current_status,
                            )
                        )
                        painted = state
                    next_refresh = now + 0.25
                for key, pressed in self.events(self.keyboard, 0.05):
                    if not pressed:
                        continue
                    if key == 1:
                        return
                    if key in (33, 44, 103, 105):
                        selected = max(0, selected - 1)
                    elif key in (45, 46, 108, 106):
                        selected = min(max(0, total - 1), selected + 1)
                    elif key == 28 and pressed == 1 and rows:
                        self.detail(rows[selected - offset])
                        painted = None
                    elif key == 15 and pressed == 1:
                        choice = self.select(
                            "Logbook",
                            [
                                "New entry",
                                "Sync logs",
                                "Search callsign",
                                "Station & logging",
                                "Back",
                            ],
                            f"{total:,} onboard contacts",
                        )
                        if choice == 0:
                            if self.new_entry():
                                selected, search, status = (
                                    0,
                                    "",
                                    "Contact saved locally",
                                )
                        elif choice == 1:
                            self.sync_menu()
                            status = "Sync status available in Menu → Sync logs"
                        elif choice == 2:
                            value = self.input("Search callsign", search, limit=24)
                            if value is not None:
                                search, selected = value.strip().upper(), 0
                        elif choice == 3:
                            if self.book.contact_sync.busy:
                                self.message(
                                    "Sync in progress",
                                    "Wait for sync before changing accounts",
                                )
                            else:
                                self.logging()
                        painted = None
                    next_refresh = 0
        finally:
            if self.owns_book:
                self.book.close()

    def detail(self, qso):
        while True:
            states = self.book.delivery_states(qso["id"])
            image, draw = self.frame(
                qso["call"],
                f"{qso.get('mode','FT8')} · {qso['band']} · "
                + (
                    f"{qso['dial']/1e6:g} MHz"
                    if qso["dial"]
                    else "No frequency recorded"
                ),
                "Enter / Esc back",
            )
            date = datetime.fromtimestamp(qso["start"], timezone.utc).strftime(
                "%Y-%m-%d  %H:%M:%S UTC"
            )
            draw.rounded_rectangle((8, 53, 312, 148), 8, fill=CARD)
            for y, label in (
                (58, date),
                (76, f"{qso['own_call']} → {qso['call']}"),
                (
                    94,
                    f"Sent {report_text(qso.get('sent')) or '—'}   Rcvd {report_text(qso.get('received')) or '—'}   {qso.get('grid','')}",
                ),
                (
                    112,
                    " · ".join(
                        f"{name.upper()} {state}" for name, state in states.items()
                    )
                    or "Onboard only",
                ),
            ):
                self.text(draw, (15, y), label, MUTED if y == 112 else TEXT)
            if qso.get("comment"):
                self.text(draw, (15, 131), qso["comment"].replace("\n", " "), MUTED)
            self.display.present(image)
            for key, pressed in self.events(self.keyboard):
                if pressed == 1 and key in (1, 15, 28):
                    return

    def new_entry(self):
        settings = load_settings()
        if not settings.get("callsign"):
            self.message("Station identity", "Set your callsign in Station & logging")
            self.logging()
            settings = load_settings()
            if not settings.get("callsign"):
                return False
        now = datetime.now(timezone.utc)
        form = dict(
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
        labels = [
            ("Call", "call"),
            ("Grid", "grid"),
            ("UTC date", "date"),
            ("UTC time", "time"),
            ("Frequency MHz", "frequency"),
            ("Mode", "mode"),
            ("Sent report", "sent"),
            ("Received report", "received"),
            ("Notes", "comment"),
        ]
        choice = 0
        while True:
            items = [label + ": " + (form[key] or "—") for label, key in labels] + [
                "Save contact",
                "Cancel",
            ]
            selected = self.select(
                "New contact",
                items,
                "Manual entry · no radio connection needed",
                choice,
            )
            if selected is None or selected == len(labels) + 1:
                return False
            choice = selected
            if selected == len(labels):
                try:
                    qso = manual_contact(form, load_settings())
                    saved = self.book.add(qso)
                    self.message(
                        "Contact saved" if saved else "Already logged",
                        qso["call"] + " · " + qso["band"] + " · " + qso["mode"],
                    )
                    return saved
                except (ValueError, OSError) as error:
                    self.message("Check contact", str(error))
            else:
                label, field = labels[selected]
                if field == "mode":
                    mode = self.select(
                        "Contact mode", list(MODES), choice=MODES.index(form[field])
                    )
                    if mode is not None:
                        form[field] = MODES[mode]
                else:
                    value = self.input(
                        label, form[field], limit=256 if field == "comment" else 32
                    )
                    if value is not None:
                        form[field] = (
                            value.strip().upper()
                            if field in ("call", "grid")
                            else value.strip()
                        )

    def sync_menu(self):
        choice = 0
        while True:
            settings = load_settings()
            items = [
                "Wavelog · "
                + (
                    "Ready"
                    if settings.get("wavelog_key") and settings.get("wavelog_enabled")
                    else "Configure in Settings"
                ),
                "QRZ · "
                + (
                    "Ready"
                    if settings.get("qrz_key") and settings.get("qrz_enabled")
                    else "Configure in Settings"
                ),
                "Retry pending uploads",
                "Back",
            ]
            selected = self.select(
                "Sync logs", items, self.book.contact_sync.status, choice
            )
            if selected is None or selected == 3:
                return
            choice = selected
            if selected == 2:
                self.book.sync()
                self.message("Uploads", "Pending uploads will retry in background")
                continue
            service = ("wavelog", "qrz")[selected]
            if self.book.contact_sync.busy:
                self.sync_progress()
                continue
            confirm = self.select(
                "Sync " + service.title(),
                ["Start sync", "Cancel"],
                "Download contacts + upload missing onboard entries",
            )
            if confirm == 0:
                try:
                    self.book.sync_contacts(service, settings)
                    self.sync_progress()
                except ValueError as error:
                    self.message("Check service", str(error))

    def sync_progress(self):
        while True:
            image, draw = self.frame(
                "Sync logs",
                "Contacts merge without replacing existing entries",
                "Esc back · sync continues while Log stays open",
            )
            draw.rounded_rectangle((10, 63, 310, 126), 8, fill=CARD)
            for row, line in enumerate(
                textwrap.wrap(self.book.contact_sync.status, 40)
            ):
                self.text(draw, (18, 75 + row * 16), line, ACCENT)
            self.display.present(image)
            if not self.book.contact_sync.busy:
                self.message(
                    (
                        "Sync failed"
                        if "failed" in self.book.contact_sync.status
                        else "Sync complete"
                    ),
                    self.book.contact_sync.status,
                )
                return
            for key, value in self.events(self.keyboard, 0.2):
                if value == 1 and key in (1, 15):
                    return
