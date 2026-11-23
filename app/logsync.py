"""Explicit full-contact synchronization, separate from the FT8 worked index."""

import json
import threading
import time

from contacts import imported_contact
from ft8history import PAGE_SIZE, SyncError, account_id, adif_records
from ft8log import adif, request, wavelog_station

FIELDS = (
    "CALL",
    "STATION_CALLSIGN",
    "MY_GRIDSQUARE",
    "GRIDSQUARE",
    "MODE",
    "SUBMODE",
    "FREQ",
    "BAND",
    "QSO_DATE",
    "QSO_DATE_OFF",
    "TIME_ON",
    "TIME_OFF",
    "RST_SENT",
    "RST_RCVD",
    "COMMENT",
)


class ContactSync:
    def __init__(self, book):
        self.book = book
        self.busy = False
        self.status = "Choose Wavelog or QRZ"
        self.lock = threading.Lock()
        self.thread = None
        with book.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS contact_sync_state (account TEXT PRIMARY KEY,cursor INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS contact_sync_presence (account TEXT,id TEXT,PRIMARY KEY(account,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS delivery_accounts (id TEXT,service TEXT,account TEXT,PRIMARY KEY(id,service))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS qso_call_time ON qsos(json_extract(payload,'$.call'),json_extract(payload,'$.start'))"
            )

    def bind(self, db, service, qso_id, settings):
        db.execute(
            "INSERT INTO delivery_accounts VALUES (?,?,?) ON CONFLICT(id,service) DO UPDATE SET account=excluded.account",
            (qso_id, service, account_id(service, settings)),
        )

    def start(self, service, settings):
        if service not in ("wavelog", "qrz") or not settings.get(service + "_key"):
            raise ValueError("Configure the logging service in Settings")
        if not settings.get(service + "_enabled"):
            raise ValueError("Enable this logging service in Settings")
        if service == "wavelog" and not settings.get("wavelog_url", "").startswith(
            "https://"
        ):
            raise ValueError("Set the Wavelog HTTPS URL in Settings")
        if not settings.get("callsign"):
            raise ValueError("Set your callsign in Settings first")
        with self.lock:
            if self.busy:
                raise ValueError("A log sync is already running")
            self.busy = True
            self.status = service.title() + " · downloading contacts…"
            self.thread = threading.Thread(
                target=self.run, args=(service, dict(settings)), daemon=True
            )
            self.thread.start()

    def run(self, service, settings):
        try:
            self.sync_now(service, settings)
        except Exception as error:
            # Never display server bodies, API URLs or credentials.
            self.status = (
                service.title()
                + " sync failed · "
                + (str(error) if isinstance(error, SyncError) else type(error).__name__)
            )
        finally:
            self.busy = False

    def match(self, db, qso):
        by_id = db.execute(
            "SELECT payload FROM qsos WHERE id=?", (qso["id"],)
        ).fetchone()
        if by_id:
            prior = json.loads(by_id[0])
            if prior["own_call"] == qso["own_call"] and prior["call"] == qso["call"]:
                return prior["id"]
            raise ValueError("Conflicting contact ID")
        rows = db.execute(
            "SELECT payload FROM qsos WHERE json_extract(payload,'$.call')=? AND json_extract(payload,'$.start') BETWEEN ? AND ?",
            (qso["call"], qso["start"] - 60, qso["start"] + 60),
        )
        for (payload,) in rows:
            prior = json.loads(payload)
            if (
                prior["own_call"] == qso["own_call"]
                and prior["band"] == qso["band"]
                and prior.get("mode", "FT8") == qso["mode"]
                and (
                    not prior["dial"]
                    or not qso["dial"]
                    or abs(prior["dial"] - qso["dial"]) <= 1000
                )
            ):
                return prior["id"]
        return None

    def apply_page(self, service, settings, account, records, cursor):
        added = merged = skipped = 0
        contacts = []
        with self.book.db() as db:
            for fields in records:
                try:
                    qso = imported_contact(fields, settings)
                    existing = self.match(db, qso)
                except (ValueError, KeyError, TypeError):
                    skipped += 1
                    continue
                if existing:
                    qso_id = existing
                    merged += 1
                else:
                    qso_id = qso["id"]
                    db.execute(
                        "INSERT INTO qsos VALUES (?,?,?)",
                        (qso_id, json.dumps(qso), adif(qso)),
                    )
                    contacts.append(qso)
                    added += 1
                db.execute(
                    "INSERT OR IGNORE INTO contact_sync_presence VALUES (?,?)",
                    (account, qso_id),
                )
                self.bind(db, service, qso_id, settings)
                # This service already has the contact. Do not upload it back.
                db.execute(
                    "INSERT INTO deliveries VALUES (?,?,?,?) ON CONFLICT(id,service) DO UPDATE SET state='sent',error=''",
                    (qso_id, service, "sent", ""),
                )
            db.execute(
                "INSERT INTO contact_sync_state VALUES (?,?) ON CONFLICT(account) DO UPDATE SET cursor=excluded.cursor",
                (account, cursor),
            )
        for qso in contacts:
            self.book.worked_history.add_local(qso)
        return added, merged, skipped

    def queue_missing(self, service, settings):
        account = account_id(service, settings)
        with self.book.db() as db:
            db.execute(
                "INSERT INTO deliveries SELECT id,?,'pending','' FROM qsos WHERE upper(json_extract(payload,'$.own_call'))=? AND id NOT IN (SELECT id FROM contact_sync_presence WHERE account=?) ON CONFLICT(id,service) DO UPDATE SET state='pending',error=''",
                (service, settings["callsign"].upper(), account),
            )
            pending_ids = db.execute(
                "SELECT id FROM deliveries WHERE service=? AND state='pending' AND id IN (SELECT id FROM qsos WHERE upper(json_extract(payload,'$.own_call'))=?)",
                (service, settings["callsign"].upper()),
            ).fetchall()
            for (qso_id,) in pending_ids:
                self.bind(db, service, qso_id, settings)
            count = db.execute(
                "SELECT count(*) FROM deliveries WHERE service=? AND state='pending'",
                (service,),
            ).fetchone()[0]
        self.book.sync()
        return count

    def sync_now(self, service, settings):
        account = account_id(service, settings)
        with self.book.db() as db:
            row = db.execute(
                "SELECT cursor FROM contact_sync_state WHERE account=?", (account,)
            ).fetchone()
        cursor = row[0] if row else 0
        station = (
            settings.get("wavelog_station") or wavelog_station(settings)
            if service == "wavelog"
            else None
        )
        added = merged = skipped = 0
        for _ in range(5000):
            if self.book.closed.is_set():
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
                        "fields": list(FIELDS),
                    },
                )
                if (
                    str(result.get("status", "")).lower()
                    in ("error", "failed", "abort")
                    or "lastfetchedid" not in result
                    or not ({"qsos", "adif"} & result.keys())
                ):
                    raise SyncError("Wavelog rejected the contact download")
                records = result.get("qsos")
                if records is None:
                    records = list(adif_records(result.get("adif") or "", fields=None))
                if not isinstance(records, list) or any(
                    not isinstance(r, dict) for r in records
                ):
                    raise SyncError("Invalid Wavelog contact response")
                next_cursor = int(result["lastfetchedid"])
                done = not records and next_cursor == cursor
                if next_cursor < cursor or (records and next_cursor <= cursor):
                    raise SyncError("Wavelog download cursor did not advance")
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
                empty = (
                    result.get("RESULT") == "FAIL"
                    and str(result.get("COUNT")) == "0"
                    and not result.get("REASON")
                    and not result.get("ADIF")
                )
                if result.get("RESULT") != "OK" and not empty:
                    raise SyncError("QRZ download failed; check API subscription")
                records = list(adif_records(result.get("ADIF", ""), fields=None))
                if records:
                    try:
                        ids = [int(r["APP_QRZLOG_LOGID"]) for r in records]
                    except KeyError:
                        ids = [
                            int(v)
                            for v in str(result.get("LOGIDS", "")).split(",")
                            if v.strip()
                        ]
                    if len(ids) != len(records) or min(ids) < cursor:
                        raise SyncError("QRZ response missing valid log IDs")
                    next_cursor = max(ids) + 1
                else:
                    next_cursor = cursor
                done = len(records) < PAGE_SIZE
                if records and next_cursor <= cursor:
                    raise SyncError("QRZ download cursor did not advance")
            if self.book.closed.is_set():
                return
            counts = self.apply_page(service, settings, account, records, next_cursor)
            added, merged, skipped = [
                a + b for a, b in zip((added, merged, skipped), counts)
            ]
            self.status = f"{service.title()} · {added} new / {merged} matched / {skipped} skipped"
            cursor = next_cursor
            if done:
                pending = self.queue_missing(service, settings)
                self.status = (
                    f"{service.title()} · {added} new, {pending} uploads queued"
                    + (f", {skipped} skipped" if skipped else "")
                )
                return
            if self.book.closed.wait(0.15):
                return
        raise SyncError("Sync paused; run again to continue")

    def close(self):
        if self.thread is not None:
            self.thread.join(timeout=0.5)
