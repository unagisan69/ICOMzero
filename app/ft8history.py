"""Private, incremental worked-station history; never an upload source."""

from collections import defaultdict
from contextlib import closing, contextmanager
import hashlib
import json
import os
import queue
import re
import sqlite3
import threading
import time

from ft8log import directory, request, wavelog_station

PAGE_SIZE = 250
KEEP_FIELDS = {"CALL", "BAND", "MODE", "SUBMODE", "APP_QRZLOG_LOGID"}


class SyncError(ValueError):
    pass


def adif_records(text):
    """Consume declared field lengths, including tags embedded inside values."""
    record = {}
    position = 0
    for_header = re.compile(r"<([A-Z0-9_]+)(?::([0-9]+)(?::[A-Z])?)?>", re.I)
    while True:
        match = for_header.search(text, position)
        if not match:
            if record:
                raise SyncError("Incomplete ADIF record")
            return
        name = match[1].upper()
        position = match.end()
        if name == "EOH":
            record = {}
        elif name == "EOR":
            if record:
                yield record
            record = {}
        elif match[2] is not None:
            end = position + int(match[2])
            if end > len(text):
                raise SyncError("Truncated ADIF field")
            if name in KEEP_FIELDS:
                record[name] = text[position:end]
            position = end


def normal_call(value):
    value = str(value or "").strip().upper().strip("<>")
    if not (3 <= len(value) <= 24 and re.fullmatch(r"[A-Z0-9]+(?:/[A-Z0-9]+)*", value)):
        return ""
    return value if re.search(r"[A-Z]", value) and re.search(r"[0-9]", value) else ""


def worked_record(record):
    fields = {str(k).upper(): str(v or "").strip() for k, v in record.items()}
    mode = (fields.get("SUBMODE") or fields.get("MODE", "")).upper()
    call = normal_call(fields.get("CALL"))
    band = fields.get("BAND", "").lower()
    if mode != "FT8" or not call or not re.fullmatch(r"[0-9.]+(?:m|cm|mm)", band):
        return None
    return call, band


def account_id(service, settings):
    values = [service, settings.get("callsign", "").upper()]
    if service == "wavelog":
        values += [
            settings.get("wavelog_url", "").rstrip("/"),
            settings.get("wavelog_station", ""),
            settings.get("wavelog_key", ""),
        ]
    elif service == "qrz":
        values += [settings.get("qrz_key", "")]
    else:
        raise ValueError("Choose Wavelog or QRZ")
    return service + ":" + hashlib.sha256(json.dumps(values).encode()).hexdigest()


class WorkedHistory:
    def __init__(self, local_log=None, path=None, start=True, pause=0.15):
        self.path = path or directory() / "worked.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.local_log = local_log
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.jobs = queue.Queue(maxsize=1)
        self.busy = False
        self.pause = pause
        self.status = "Worked history ready"
        self.revision = 0
        self.cache = defaultdict(set)
        self.memo_key = self.memo_calls = None
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS worked (account TEXT, call TEXT, band TEXT, PRIMARY KEY(account,call,band))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS sync_state (account TEXT PRIMARY KEY, cursor INTEGER NOT NULL, synced_at REAL)"
            )
            for account, call, band in db.execute(
                "SELECT account,call,band FROM worked"
            ):
                self.cache[account, band].add(call)
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.work, daemon=True)
        if start:
            self.thread.start()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def apply(self, account, records, cursor=None, complete=False):
        rows = [item for record in records if (item := worked_record(record))]
        with self.lock:
            with self.db() as db:
                db.executemany(
                    "INSERT OR IGNORE INTO worked VALUES (?,?,?)",
                    [(account, call, band) for call, band in rows],
                )
                if cursor is not None:
                    db.execute(
                        "INSERT INTO sync_state VALUES (?,?,?) ON CONFLICT(account) DO UPDATE SET cursor=excluded.cursor, synced_at=COALESCE(excluded.synced_at,sync_state.synced_at)",
                        (account, cursor, time.time() if complete else None),
                    )
            changed = False
            for call, band in rows:
                bucket = self.cache[account, band]
                if call not in bucket:
                    bucket.add(call)
                    changed = True
            if changed:
                self.revision += 1

    def add_local(self, qso):
        self.apply(
            "local:" + qso["own_call"].upper(),
            [{"CALL": qso["call"], "BAND": qso["band"], "MODE": "FT8"}],
        )

    def seed_local(self):
        if self.local_log is None or not self.local_log.exists():
            return
        last = 0
        while not self.closed.is_set():
            with closing(sqlite3.connect(self.local_log, timeout=5)) as db:
                rows = db.execute(
                    "SELECT rowid,payload FROM qsos WHERE rowid>? ORDER BY rowid LIMIT 250",
                    (last,),
                ).fetchall()
            if not rows:
                break
            grouped = defaultdict(list)
            for rowid, payload in rows:
                qso = json.loads(payload)
                grouped["local:" + qso["own_call"].upper()].append(
                    {"CALL": qso["call"], "BAND": qso["band"], "MODE": "FT8"}
                )
                last = rowid
            for account, records in grouped.items():
                self.apply(account, records)

    def calls(self, settings, band):
        # Stable memoized sets: no HTTP or SQLite on the decode/tuning path.
        signature = tuple(
            settings.get(k, "")
            for k in (
                "callsign",
                "wavelog_url",
                "wavelog_station",
                "wavelog_key",
                "qrz_key",
            )
        )
        with self.lock:
            key = self.revision, band, signature
            if key != self.memo_key:
                accounts = ["local:" + settings.get("callsign", "").upper()]
                accounts += [
                    account_id(service, settings)
                    for service in ("wavelog", "qrz")
                    if settings.get(service + "_key")
                ]
                self.memo_calls = frozenset().union(
                    *(self.cache.get((account, band), ()) for account in accounts)
                )
                self.memo_key = key
            return self.memo_calls

    def cursor(self, account):
        with self.db() as db:
            row = db.execute(
                "SELECT cursor FROM sync_state WHERE account=?", (account,)
            ).fetchone()
        return int(row[0]) if row else 0

    def sync(self, service, settings):
        if service not in ("wavelog", "qrz") or not settings.get(service + "_key"):
            raise ValueError("Configure the logging service first")
        if service == "wavelog" and not settings.get("wavelog_url", "").startswith(
            "https://"
        ):
            raise ValueError("Configure the Wavelog HTTPS URL first")
        with self.lock:
            if self.busy:
                raise ValueError("Log sync already running")
            self.busy = True
            self.status = service.title() + " sync starting"
            self.jobs.put_nowait((service, dict(settings)))

    def sync_now(self, service, settings):
        account = account_id(service, settings)
        cursor = self.cursor(account)
        station = (
            (settings.get("wavelog_station") or wavelog_station(settings))
            if service == "wavelog"
            else None
        )
        for _ in range(5000):
            if self.closed.is_set():
                return
            if service == "wavelog":
                result = request(
                    settings["wavelog_url"].rstrip("/") + "/api/get_contacts_adif",
                    {
                        "key": settings["wavelog_key"],
                        "station_id": station,
                        "fetchfromid": cursor,
                        "limit": PAGE_SIZE,
                        "output_format": "json",
                        "fields": ["CALL", "BAND", "MODE", "SUBMODE"],
                    },
                )
                if str(result.get("status", "")).lower() in (
                    "failed",
                    "error",
                    "abort",
                ):
                    raise SyncError("Wavelog history request rejected")
                if "lastfetchedid" not in result or not (
                    {"qsos", "adif"} & result.keys()
                ):
                    raise SyncError("Invalid Wavelog history response")
                records = result.get("qsos")
                if records is None:
                    records = list(adif_records(result.get("adif") or ""))
                if not isinstance(records, list) or any(
                    not isinstance(row, dict) for row in records
                ):
                    raise SyncError("Invalid Wavelog history response")
                next_cursor = int(result.get("lastfetchedid", cursor))
                complete = not records and next_cursor == cursor
                if next_cursor < cursor or (records and next_cursor <= cursor):
                    raise SyncError("Wavelog history cursor did not advance")
            else:
                result = request(
                    "https://logbook.qrz.com/api",
                    {
                        "KEY": settings["qrz_key"],
                        "ACTION": "FETCH",
                        "OPTION": f"MAX:{PAGE_SIZE},AFTERLOGID:{cursor},TYPE:ADIF",
                    },
                    True,
                )
                if result.get("RESULT") != "OK":
                    raise SyncError("QRZ history access failed; check API subscription")
                records = list(adif_records(result.get("ADIF", "")))
                if records:
                    try:
                        ids = [int(row["APP_QRZLOG_LOGID"]) for row in records]
                    except KeyError:
                        ids = [
                            int(value)
                            for value in str(result.get("LOGIDS", "")).split(",")
                            if value.strip()
                        ]
                        if len(ids) != len(records):
                            raise SyncError("QRZ history response missing log IDs")
                    next_cursor = max(ids) + 1
                    if min(ids) < cursor or next_cursor <= cursor:
                        raise SyncError("QRZ history cursor did not advance")
                else:
                    next_cursor = cursor
                complete = len(records) < PAGE_SIZE
            # Commit each page and its bookmark together; retry resumes safely.
            self.apply(account, records, next_cursor, complete)
            cursor = next_cursor
            with self.lock:
                count = sum(
                    len(calls)
                    for (key, _), calls in self.cache.items()
                    if key == account
                )
            self.status = f"{service.title()}: {count} worked FT8 entries" + (
                "" if complete else "…"
            )
            if complete:
                return
            if self.closed.wait(self.pause):
                return
        raise SyncError("History sync paused; select Sync Log again to continue")

    def work(self):
        try:
            self.seed_local()
        except (OSError, ValueError, sqlite3.Error):
            self.status = "Local history refresh failed; retry Sync Log"
        while not self.closed.is_set():
            try:
                service, settings = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.sync_now(service, settings)
            except Exception as error:
                reason = (
                    str(error) if isinstance(error, SyncError) else type(error).__name__
                )
                self.status = service.title() + " sync failed: " + reason
            finally:
                with self.lock:
                    self.busy = False

    def close(self):
        self.closed.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=0.5)
