"""Private station configuration, durable ADIF log and independent upload outboxes."""

from pathlib import Path
from contextlib import contextmanager
import hashlib
import json
import os
import queue
import re
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request


def directory():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "icomzero"


DEFAULTS = {
    "callsign": "",
    "grid": "",
    "tx_hz": "Auto",
    "tx_level": "50",
    "tx_power_w": "10",
    "cq_period": "Even",
    "qso_beep": True,
    "hide_worked": False,
    "wavelog_url": "",
    "wavelog_key": "",
    "wavelog_station": "",
    "wavelog_enabled": False,
    "qrz_key": "",
    "qrz_enabled": False,
}


def load_settings():
    try:
        values = json.loads((directory() / "ft8.json").read_text())
        return {**DEFAULTS, **{k: v for k, v in values.items() if k in DEFAULTS}}
    except (OSError, ValueError, AttributeError):
        return dict(DEFAULTS)


def validate_station(settings):
    if not re.fullmatch(
        r"[A-Z0-9]{1,4}[0-9][A-Z0-9]{1,4}(?:/[A-Z0-9]{1,4})?", settings["callsign"]
    ):
        raise ValueError("Enter a valid callsign")
    if not re.fullmatch(r"[A-R]{2}[0-9]{2}(?:[A-X]{2})?", settings["grid"]):
        raise ValueError("Enter a 4 or 6 character grid")
    if (
        str(settings["tx_hz"]).strip().lower() != "auto"
        and not 200 <= int(settings["tx_hz"]) <= 2800
    ):
        raise ValueError("TX audio must be Auto or 200–2800 Hz")
    if not 1 <= int(settings.get("tx_power_w", "10")) <= 200:
        raise ValueError("TX power must be 1–200 W (radio limit applies)")
    if not 10 <= int(settings.get("tx_level", "50")) <= 80:
        raise ValueError("TX level must be 10–80%")


def save_settings(values):
    directory().mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory(), delete=False) as f:
        try:
            json.dump({k: v for k, v in values.items() if k in DEFAULTS}, f)
            f.flush()
            os.fsync(f.fileno())
            os.replace(f.name, directory() / "ft8.json")
        finally:
            Path(f.name).unlink(missing_ok=True)


def request(url, data, form=False):
    if not url.startswith("https://"):
        raise ValueError("Logging requires HTTPS")
    body = (
        None
        if data is None
        else (
            urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        )
    )
    req = urllib.request.Request(
        url,
        body,
        headers={
            "Content-Type": (
                "application/x-www-form-urlencoded" if form else "application/json"
            ),
            "Accept": "application/json",
            "User-Agent": "ICOMzero/0.19.3",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        text = response.read(2 * 1024 * 1024).decode("utf-8")
    return dict(urllib.parse.parse_qsl(text)) if form else json.loads(text)


def wavelog_station(settings):
    result = request(
        settings["wavelog_url"].rstrip("/")
        + "/api/station_info/"
        + urllib.parse.quote(settings["wavelog_key"], safe=""),
        None,
    )
    stations = result if isinstance(result, list) else result.get("stations", [])
    matches = [
        s
        for s in stations
        if s.get("station_callsign", "").upper() == settings["callsign"]
    ]
    matches.sort(
        key=lambda s: (
            s.get("station_gridsquare", "").upper() != settings["grid"],
            str(s.get("station_active")) != "1",
        )
    )
    if not matches:
        raise ValueError("No matching Wavelog station")
    return str(matches[0]["station_id"])


def test_service(service, settings):
    if service == "wavelog":
        return "Station " + wavelog_station(settings)
    result = request(
        "https://logbook.qrz.com/api",
        {"KEY": settings["qrz_key"], "ACTION": "STATUS"},
        True,
    )
    if result.get("RESULT") != "OK":
        raise ValueError("QRZ authentication/subscription failed")
    return "QRZ connected"


def report_text(value):
    return f"{value:+03d}" if isinstance(value, int) else str(value or "")


def adif(qso):
    start = time.gmtime(qso["start"])
    end = time.gmtime(qso["end"])
    fields = {
        "CALL": qso["call"],
        "STATION_CALLSIGN": qso["own_call"],
        "MY_GRIDSQUARE": qso["own_grid"],
        "MODE": qso.get("mode", "FT8"),
        "FREQ": f"{qso['dial']/1e6:.6f}" if qso["dial"] else "",
        "BAND": qso["band"],
        "QSO_DATE": time.strftime("%Y%m%d", start),
        "TIME_ON": time.strftime("%H%M%S", start),
        "QSO_DATE_OFF": time.strftime("%Y%m%d", end),
        "TIME_OFF": time.strftime("%H%M%S", end),
        "RST_SENT": report_text(qso.get("sent")),
        "RST_RCVD": report_text(qso.get("received")),
        "GRIDSQUARE": qso.get("grid", ""),
        "APP_ICOMZERO_ID": qso["id"],
        "COMMENT": qso.get("comment", ""),
    }
    if fields["MODE"] in ("FT4", "PSK31"):
        fields["SUBMODE"] = fields["MODE"]
        fields["MODE"] = "MFSK" if fields["MODE"] == "FT4" else "PSK"
    return (
        "".join(f"<{k}:{len(str(v))}>{v}" for k, v in fields.items() if v != "")
        + "<EOR>"
    )


class Logbook:
    def __init__(self):
        directory().mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory() / "qsos.sqlite3"
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS qsos (id TEXT PRIMARY KEY, payload TEXT NOT NULL, adif TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS deliveries (id TEXT, service TEXT, state TEXT, error TEXT, PRIMARY KEY(id,service))"
            )
        os.chmod(self.path, 0o600)
        from ft8history import WorkedHistory

        self.worked_history = WorkedHistory(self.path)
        self.closed = threading.Event()
        self.wake = threading.Event()
        from logsync import ContactSync

        self.contact_sync = ContactSync(self)
        self.status = "Local log ready"
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.thread.start()
        self.wake.set()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add(self, qso):
        settings = load_settings()
        with self.db() as db:
            for (payload,) in db.execute(
                "SELECT payload FROM qsos WHERE id=? OR (json_extract(payload,'$.call')=? "
                "AND json_extract(payload,'$.own_call')=? AND json_extract(payload,'$.dial')=? "
                "AND json_extract(payload,'$.start')<=? AND json_extract(payload,'$.end')>=?)",
                (
                    qso["id"],
                    qso["call"],
                    qso["own_call"],
                    qso["dial"],
                    qso["end"],
                    qso["start"],
                ),
            ):
                prior = json.loads(payload)
                if prior["id"] == qso["id"] or (
                    all(prior[k] == qso[k] for k in ("call", "own_call", "dial"))
                    and prior.get("mode", "FT8") == qso.get("mode", "FT8")
                    and prior["start"] <= qso["end"]
                    and qso["start"] <= prior["end"]
                ):
                    self.status = "QSO already logged"
                    return False
            db.execute(
                "INSERT OR IGNORE INTO qsos VALUES (?,?,?)",
                (qso["id"], json.dumps(qso), adif(qso)),
            )
            for service in ("wavelog", "qrz"):
                if settings[service + "_enabled"]:
                    db.execute(
                        "INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?)",
                        (qso["id"], service, "pending", ""),
                    )
                    self.contact_sync.bind(db, service, qso["id"], settings)
        try:
            self.worked_history.add_local(qso)
        except (OSError, ValueError, sqlite3.Error):
            self.worked_history.status = "Contact saved; worked history refresh needed"
        self.status = "Contact saved locally"
        self.wake.set()
        return True

    def recent(self):
        return self.page(0, 100)[0]

    def page(self, offset=0, limit=4, search=""):
        """Read only the visible rows, ordered by contact time (including imports)."""
        search = str(search).strip().upper()
        where = (
            "WHERE instr(upper(json_extract(payload,'$.call')),?)>0" if search else ""
        )
        args = (search,) if search else ()
        with self.db() as db:
            count = db.execute("SELECT count(*) FROM qsos " + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT payload FROM qsos "
                + where
                + " ORDER BY json_extract(payload,'$.start') DESC,id DESC LIMIT ? OFFSET ?",
                args + (max(1, min(100, int(limit))), max(0, int(offset))),
            ).fetchall()
        return [json.loads(row[0]) for row in rows], count

    def delivery_states(self, qso_id):
        with self.db() as db:
            return dict(
                db.execute("SELECT service,state FROM deliveries WHERE id=?", (qso_id,))
            )

    def sync_contacts(self, service, settings=None):
        self.contact_sync.start(service, settings or load_settings())

    def summary(self):
        with self.db() as db:
            rows = db.execute(
                "SELECT service,state,count(*) FROM deliveries GROUP BY service,state"
            ).fetchall()
        return [f"{service}: {count} {state}" for service, state, count in rows] or [
            "No uploads queued"
        ]

    @property
    def history_status(self):
        return self.worked_history.status

    def worked_calls(self, band, settings=None):
        return self.worked_history.calls(
            settings if settings is not None else load_settings(), band
        )

    def sync_history(self, service, settings=None):
        self.worked_history.sync(
            service, settings if settings is not None else load_settings()
        )

    def sync(self):
        self.wake.set()

    def work(self):
        retry_at = time.monotonic() + 60
        while not self.closed.is_set():
            if not self.wake.wait(0.5):
                if time.monotonic() < retry_at:
                    continue
            retry_at = time.monotonic() + 60
            self.wake.clear()
            with self.db() as db:
                pending = db.execute(
                    "SELECT d.id,d.service,q.adif,a.account FROM deliveries d JOIN qsos q ON q.id=d.id LEFT JOIN delivery_accounts a ON a.id=d.id AND a.service=d.service WHERE d.state=?",
                    ("pending",),
                ).fetchall()
            for qso_id, service, record, bound_account in pending:
                if self.closed.is_set():
                    break
                settings = load_settings()
                if not settings.get(service + "_enabled"):
                    continue
                from ft8history import account_id

                if bound_account and bound_account != account_id(service, settings):
                    self.status = service.title() + " account changed; use Log sync"
                    continue
                try:
                    if service == "wavelog":
                        station = settings["wavelog_station"] or wavelog_station(
                            settings
                        )
                        result = request(
                            settings["wavelog_url"].rstrip("/") + "/api/qso",
                            {
                                "key": settings["wavelog_key"],
                                "station_profile_id": station,
                                "type": "adif",
                                "string": record,
                            },
                        )
                        if str(result.get("status", "")).lower() not in (
                            "created",
                            "success",
                            "ok",
                        ):
                            raise ValueError("Wavelog rejected upload")
                    else:
                        result = request(
                            "https://logbook.qrz.com/api",
                            {
                                "KEY": settings["qrz_key"],
                                "ACTION": "INSERT",
                                "ADIF": record,
                            },
                            True,
                        )
                        if result.get("RESULT") != "OK":
                            reason = result.get("REASON", "").lower()
                            if not (
                                "duplicate" in reason or "already exists" in reason
                            ):
                                raise ValueError("QRZ rejected upload")
                    with self.db() as db:
                        db.execute(
                            "UPDATE deliveries SET state=?,error=? WHERE id=? AND service=?",
                            ("sent", "", qso_id, service),
                        )
                    from ft8history import account_id

                    with self.db() as db:
                        db.execute(
                            "INSERT OR IGNORE INTO contact_sync_presence VALUES (?,?)",
                            (account_id(service, settings), qso_id),
                        )
                    self.status = service.title() + " upload complete"
                except Exception as error:
                    # Never retain server bodies/URLs that may contain credentials.
                    with self.db() as db:
                        db.execute(
                            "UPDATE deliveries SET error=? WHERE id=? AND service=?",
                            (type(error).__name__, qso_id, service),
                        )
                    self.status = service.title() + " pending; retry in Logging"

    def close(self):
        self.closed.set()
        self.wake.set()
        self.thread.join(timeout=0.5)
        self.contact_sync.close()
        self.worked_history.close()
