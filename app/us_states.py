"""Offline FCC registered-state labels. No personal names or addresses stored."""

from contextlib import closing
from functools import lru_cache
import os
from pathlib import Path
import re
import sqlite3

STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR VI GU AS MP".split()
)
US_ENTITIES = {
    "United States",
    "Alaska",
    "Hawaii",
    "Puerto Rico",
    "Guam",
    "US Virgin Islands",
    "U.S. Virgin Islands",
    "American Samoa",
    "Mariana Islands",
}


def database_paths():
    custom = (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "icomzero/us-states.sqlite3"
    )
    installed = Path(__file__).with_name("states.sqlite3")
    bundled = (
        installed
        if installed.exists()
        else Path(__file__).resolve().parents[1] / "vendor/us-states/states.sqlite3"
    )
    return custom, bundled


@lru_cache(maxsize=4096)
def state_for_call(call):
    parts = sorted(set(call.upper().split("/")), key=len, reverse=True)
    for path in database_paths():
        if not path.exists():
            continue
        try:
            with closing(
                sqlite3.connect(
                    path.resolve().as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                    timeout=0.1,
                )
            ) as db:
                db.execute("PRAGMA cache_size=-256")
                for part in parts:
                    if not re.fullmatch(r"[A-Z0-9]*[0-9][A-Z0-9]+", part):
                        continue
                    row = db.execute(
                        "SELECT state FROM states WHERE call=?", (part,)
                    ).fetchone()
                    if row and row[0] in STATES:
                        return row[0]
        except (sqlite3.Error, OSError):
            continue
    return None


def label(call, entity):
    if entity not in US_ENTITIES:
        return entity
    state = state_for_call(call)
    if state is None:
        state = {
            "Alaska": "AK",
            "Hawaii": "HI",
            "Puerto Rico": "PR",
            "Guam": "GU",
            "American Samoa": "AS",
            "Mariana Islands": "MP",
            "US Virgin Islands": "VI",
            "U.S. Virgin Islands": "VI",
        }.get(entity, "??")
    return "USA-" + state
