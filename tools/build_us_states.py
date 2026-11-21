#!/usr/bin/env python3
"""Reduce FCC ULS amateur records to active callsign/state pairs only."""

import argparse
import hashlib
import sqlite3
from pathlib import Path
import zipfile

STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR VI GU AS MP".split()
)


def build(archive, output):
    active = {}
    rows = {}
    with zipfile.ZipFile(archive) as zipped:
        with zipped.open("HD.dat") as stream:
            for raw in stream:
                row = raw.decode("latin1").rstrip("\r\n").split("|")
                if (
                    len(row) > 6
                    and row[0] == "HD"
                    and row[5] == "A"
                    and row[6] in ("HA", "HV")
                ):
                    active[row[1]] = row[4].strip().upper()
        with zipped.open("EN.dat") as stream:
            for raw in stream:
                row = raw.decode("latin1").rstrip("\r\n").split("|")
                if len(row) > 17 and row[0] == "EN" and row[5] == "L":
                    call, state = row[4].strip().upper(), row[17].strip().upper()
                    if active.get(row[1]) == call and state in STATES:
                        prior = rows.get(call)
                        if prior is None or int(row[1]) > prior[1]:
                            rows[call] = (state, int(row[1]))
        snapshot = max(zipped.getinfo(n).date_time[:3] for n in ("HD.dat", "EN.dat"))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    with sqlite3.connect(output) as db:
        db.execute(
            "CREATE TABLE states (call TEXT PRIMARY KEY,state TEXT NOT NULL) WITHOUT ROWID"
        )
        db.executemany(
            "INSERT INTO states VALUES (?,?)",
            ((call, rows[call][0]) for call in sorted(rows)),
        )
        db.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID"
        )
        db.executemany(
            "INSERT INTO metadata VALUES (?,?)",
            [
                ("source", "https://data.fcc.gov/download/pub/uls/complete/l_amat.zip"),
                ("snapshot", "%04d-%02d-%02d" % snapshot),
                (
                    "source_sha256",
                    hashlib.file_digest(open(archive, "rb"), "sha256").hexdigest(),
                ),
                ("records", str(len(rows))),
                (
                    "location",
                    "FCC licensee registered state; not live operating position",
                ),
            ],
        )
    print(
        "Saved",
        len(rows),
        "callsign/state pairs; no names, addresses, ZIP codes or contacts",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("output")
    args = parser.parse_args()
    build(args.archive, args.output)
