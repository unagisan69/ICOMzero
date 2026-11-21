"""Offline CTY entity lookup. Prefix estimates are not live station locations."""

from functools import lru_cache
import os
from pathlib import Path
import re
from us_states import label as us_label


def bundled_path():
    installed = Path(__file__).with_name("cty.dat")
    return (
        installed
        if installed.exists()
        else Path(__file__).resolve().parents[1] / "vendor/country-files/cty.dat"
    )


class Countries:
    def __init__(self, text):
        self.exact, self.prefix = {}, {}
        for block in text.split(";"):
            lines = block.strip().splitlines()
            if not lines:
                continue
            header = lines[0].split(":")
            if len(header) < 9 or header[7].strip().startswith("*"):
                continue
            country = header[0].strip()
            for token in "".join(lines[1:]).split(","):
                token = (
                    re.sub(r"\([^)]*\)|\[[^]]*\]|<[^>]*>|\{[^}]*\}|~[^~]*~", "", token)
                    .strip()
                    .upper()
                )
                if token.startswith("="):
                    self.exact[token[1:]] = country
                elif token:
                    self.prefix[token] = country
        if len(self.prefix) < 100:
            raise ValueError("Incomplete country database")

    def prefix_country(self, call):
        for size in range(len(call), 0, -1):
            if call[:size] in self.prefix:
                return self.prefix[call[:size]]
        return ""

    @lru_cache(maxsize=4096)
    def lookup(self, call):
        call = call.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]+(?:/[A-Z0-9]+)*", call):
            return "Unknown"
        if call in self.exact:
            return self.exact[call]
        if call.endswith("/MM"):
            return "Maritime mobile"
        if call.endswith("/AM"):
            return "Aeronautical mobile"
        parts = call.split("/")
        while len(parts) > 1 and (
            parts[-1] in ("P", "R", "M", "QRP", "QRPP") or parts[-1].isdigit()
        ):
            parts.pop()
        base = "/".join(parts)
        if base in self.exact:
            return self.exact[base]
        if len(parts) > 1:
            # A short operating prefix takes precedence over the home call.
            full = lambda p: bool(re.fullmatch(r"[A-Z0-9]*[0-9][A-Z]+", p))
            for part in (parts[0], parts[-1]):
                if not full(part):
                    found = self.prefix_country(part)
                    if found:
                        return found
            for part in parts:
                if full(part):
                    return (
                        self.exact.get(part) or self.prefix_country(part) or "Unknown"
                    )
        return self.exact.get(base) or self.prefix_country(base) or "Unknown"


@lru_cache(maxsize=1)
def database():
    custom = (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "icomzero/cty.dat"
    )
    for path in (custom, bundled_path()):
        try:
            return Countries(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
    return None


def country(call):
    db = database()
    return db.lookup(call) if db is not None else "Unknown"


def message_country(message):
    parts = message.text.split()
    if not parts or "<" in message.text:
        return "Unknown"
    if parts[0] == "CQ":
        call = (
            parts[-2]
            if re.fullmatch(r"[A-R]{2}[0-9]{2}", parts[-1]) and len(parts) >= 3
            else parts[-1]
        )
    elif len(parts) == 3:
        call = parts[0] if message.local_tx else parts[1]
    else:
        return "Unknown"
    return us_label(call, country(call))


def activation_cq(message):
    parts = message.text.upper().split()
    return bool(
        parts and parts[0] == "CQ" and any(p in ("POTA", "SOTA") for p in parts[1:])
    )


def short_country(name):
    return {
        "United States": "USA",
        "European Russia": "Eur. Russia",
        "Asiatic Russia": "As. Russia",
        "Federal Republic of Germany": "Germany",
        "Fed. Rep. of Germany": "Germany",
    }.get(name, name)
