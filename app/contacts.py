"""Contact validation shared by manual entry and full-log import."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re

MODES = ("FT8", "SSB", "CW", "FT4", "AM", "FM", "RTTY", "PSK31")
BAND_RANGES = (
    (1800000, 2000000, "160m"),
    (3500000, 4000000, "80m"),
    (5000000, 5500000, "60m"),
    (7000000, 7300000, "40m"),
    (10100000, 10150000, "30m"),
    (14000000, 14350000, "20m"),
    (18068000, 18168000, "17m"),
    (21000000, 21450000, "15m"),
    (24890000, 24990000, "12m"),
    (28000000, 29700000, "10m"),
    (50000000, 54000000, "6m"),
    (70000000, 71000000, "4m"),
    (144000000, 148000000, "2m"),
    (222000000, 225000000, "1.25m"),
    (420000000, 450000000, "70cm"),
    (1240000000, 1300000000, "23cm"),
)


def band_for(hz):
    return next((band for lo, hi, band in BAND_RANGES if lo <= hz <= hi), "")


def callsign(value):
    value = str(value).strip().upper()
    if (
        not re.fullmatch(r"[A-Z0-9]+(?:/[A-Z0-9]+)*", value)
        or not 3 <= len(value) <= 24
        or not re.search(r"[A-Z]", value)
        or not re.search(r"[0-9]", value)
    ):
        raise ValueError("Enter a valid callsign")
    return value


def grid(value):
    value = str(value or "").strip().upper()
    if value and not re.fullmatch(r"[A-R]{2}[0-9]{2}(?:[A-X]{2})?", value):
        raise ValueError("Grid must be 4 or 6 characters")
    return value


def utc_stamp(date, clock):
    date, clock = str(date).replace("-", ""), str(clock).replace(":", "")
    if len(clock) == 4:
        clock += "00"
    if not re.fullmatch(r"[0-9]{8}", date) or not re.fullmatch(r"[0-9]{6}", clock):
        raise ValueError("Use UTC date YYYY-MM-DD and time HH:MM:SS")
    try:
        return (
            datetime.strptime(date + clock, "%Y%m%d%H%M%S")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        raise ValueError("Enter a valid UTC date and time") from None


def frequency(value):
    try:
        mhz = Decimal(str(value))
        if not mhz.is_finite() or not Decimal("0.001") <= mhz <= Decimal("300000"):
            raise ValueError()
        return int(mhz * 1000000)
    except (ValueError, InvalidOperation, OverflowError):
        raise ValueError("Enter a valid frequency in MHz") from None


def identity(qso):
    key = "|".join(str(qso[k]) for k in ("own_call", "call", "start", "dial", "mode"))
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def manual_contact(form, settings):
    own = callsign(settings.get("callsign", ""))
    call = callsign(form.get("call", ""))
    if call == own:
        raise ValueError("Contact callsign must differ from yours")
    start = utc_stamp(form["date"], form["time"])
    hz = frequency(form["frequency"])
    band = band_for(hz)
    if not band:
        raise ValueError("Choose a frequency in a supported amateur band")
    mode = str(form["mode"]).strip().upper()
    if mode not in MODES:
        raise ValueError("Choose a supported mode")
    sent, received = (str(form.get(k, "")).strip() for k in ("sent", "received"))
    for report in (sent, received):
        if not report:
            continue
        if mode in ("FT8", "FT4"):
            try:
                valid = (
                    bool(re.fullmatch(r"[+-]?[0-9]{1,2}", report))
                    and -50 <= int(report) <= 49
                )
            except ValueError:
                valid = False
        else:
            valid = bool(re.fullmatch(r"[1-5][1-9](?:[1-9])?", report))
        if not valid:
            raise ValueError("Enter a valid signal report or leave blank")
    qso = dict(
        call=call,
        own_call=own,
        own_grid=grid(settings.get("grid", "")),
        grid=grid(form.get("grid", "")),
        dial=hz,
        band=band,
        mode=mode,
        start=start,
        end=start,
        sent=sent,
        received=received,
        comment=str(form.get("comment", "")).strip()[:256],
        source="manual",
    )
    qso["id"] = identity(qso)
    return qso


def imported_contact(fields, settings):
    f = {str(k).upper(): str(v or "").strip() for k, v in fields.items()}
    call = callsign(f.get("CALL", ""))
    own = callsign(f.get("STATION_CALLSIGN") or settings.get("callsign", ""))
    if own != callsign(settings.get("callsign", "")):
        raise ValueError("Contact belongs to another station")
    start = utc_stamp(f.get("QSO_DATE", ""), f.get("TIME_ON", ""))
    end = utc_stamp(
        f.get("QSO_DATE_OFF") or f.get("QSO_DATE", ""),
        f.get("TIME_OFF") or f.get("TIME_ON", ""),
    )
    if end < start:
        end = start
    hz = frequency(f["FREQ"]) if f.get("FREQ") else 0
    mode = (f.get("SUBMODE") or f.get("MODE", "")).upper()
    if not mode or not re.fullmatch(r"[A-Z0-9-]{1,16}", mode):
        raise ValueError("Missing contact mode")
    qso = dict(
        call=call,
        own_call=own,
        own_grid=f.get("MY_GRIDSQUARE") or settings.get("grid", ""),
        grid=f.get("GRIDSQUARE", ""),
        dial=hz,
        band=f.get("BAND", "").lower() or band_for(hz),
        mode=mode,
        start=start,
        end=end,
        sent=f.get("RST_SENT", ""),
        received=f.get("RST_RCVD", ""),
        comment=f.get("COMMENT", ""),
        source="import",
        imported_fields=f,
    )
    if not re.fullmatch(r"[0-9.]+(?:m|cm|mm)", qso["band"]):
        raise ValueError("Missing or invalid contact band")
    qso["id"] = f.get("APP_ICOMZERO_ID") or identity(qso)
    return qso
