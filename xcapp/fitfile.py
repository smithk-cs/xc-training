"""Decode Garmin .FIT activity files with Garmin's official FIT SDK (garmin-fit-sdk).

Only timing, distance and heart rate are read. Position fields are never touched.
"""
from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass, field

from garmin_fit_sdk import Decoder, Stream

MAX_FIT_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # one uploaded file, e.g. a Garmin "Export Your Data" zip
MAX_FITS_PER_UPLOAD = 3000
RUNNING_SPORTS = {"running", "track_running", "trail_running", "treadmill_running", "indoor_running", "virtual_running"}


class FitError(ValueError):
    pass


@dataclass
class ParsedLap:
    start: dt.datetime
    elapsed_s: float
    timer_s: float
    distance_m: float
    avg_hr: float | None = None
    max_hr: float | None = None
    intensity: str = ""
    trigger: str = ""

    @property
    def speed(self) -> float:
        return self.distance_m / self.timer_s if self.timer_s > 0 else 0.0


@dataclass
class ParsedActivity:
    external_id: str
    start: dt.datetime  # UTC, timezone-aware
    duration_s: float
    distance_m: float
    sport: str = "running"
    avg_hr: float | None = None
    max_hr: float | None = None
    device: str = ""
    vo2_max: float | None = None  # the watch's VO2 max estimate (mL/kg/min), when the file has one
    laps: list[ParsedLap] = field(default_factory=list)
    # (seconds since start, heart rate or None)
    hr_samples: list[tuple[float, int]] = field(default_factory=list)
    # (seconds since start, distance so far in meters): for the time spent in each pace zone
    dist_samples: list[tuple[float, float]] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.sport in RUNNING_SPORTS or self.sport.endswith("running") or self.sport == "running"


def _utc(v) -> dt.datetime | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    return None


_SUFFIXES = {"large", "small", "asia", "japan", "china", "taiwan", "korea", "sea", "apac", "india", "russia", "single", "byte", "product", "id"}


def pretty_device(garmin_product: str | None, product_name: str | None, manufacturer: str | None) -> str:
    """Human device label for Garmin's attribution requirement, e.g. 'Garmin Forerunner 265'."""
    if product_name:
        name = product_name.strip()
        return name if name.lower().startswith("garmin") else f"Garmin {name}"
    if garmin_product and isinstance(garmin_product, str):
        parts = [p for p in garmin_product.lower().split("_") if p not in _SUFFIXES]
        base = "".join(parts) if parts else garmin_product
        m = re.match(r"^(fr|fenix|epix|vivoactive|venu|instinct|enduro|forerunner)(\d.*)?$", base)
        if m:
            fam = {"fr": "Forerunner", "fenix": "Fenix", "epix": "Epix", "vivoactive": "Vivoactive", "venu": "Venu",
                   "instinct": "Instinct", "enduro": "Enduro", "forerunner": "Forerunner"}[m.group(1)]
            return f"Garmin {fam} {m.group(2) or ''}".strip()
        return f"Garmin {base.title()}"
    if manufacturer and str(manufacturer).lower() == "garmin":
        return "Garmin device"
    return str(manufacturer).title() if manufacturer else ""


def parse_fit(data: bytes) -> ParsedActivity:
    if not data or len(data) > MAX_FIT_BYTES:
        raise FitError("File is empty or larger than 25 MB.")
    # Integrity is checked on its own stream: the decoder consumes the stream it checks.
    checker = Decoder(Stream.from_byte_array(bytearray(data)))
    if not checker.is_fit():
        raise FitError("This is not a FIT file.")
    if not Decoder(Stream.from_byte_array(bytearray(data))).check_integrity():
        raise FitError("The FIT file is damaged (checksum failed).")
    messages, errors = Decoder(Stream.from_byte_array(bytearray(data))).read(
        apply_scale_and_offset=True, convert_datetimes_to_dates=True, convert_types_to_strings=True,
        enable_crc_check=True, expand_sub_fields=True, expand_components=True, merge_heart_rates=True,
    )
    if errors:
        raise FitError(f"The FIT file could not be read: {errors[0]}")

    file_id = (messages.get("file_id_mesgs") or [{}])[0]
    if file_id.get("type") not in (None, "activity"):
        raise FitError("This FIT file is not an activity.")
    sessions = messages.get("session_mesgs") or []
    records = messages.get("record_mesgs") or []
    laps_raw = messages.get("lap_mesgs") or []
    if not sessions and not records:
        raise FitError("The FIT file has no activity data.")

    start = _utc(sessions[0].get("start_time")) if sessions else None
    if start is None and records:
        start = _utc(records[0].get("timestamp"))
    if start is None:
        raise FitError("The FIT file has no start time.")

    sport = str(sessions[0].get("sport", "running")) if sessions else "running"
    if sessions:
        distance = float(sum(s.get("total_distance") or 0 for s in sessions))
        duration = float(sum(s.get("total_timer_time") or s.get("total_elapsed_time") or 0 for s in sessions))
        avg_hr = sessions[0].get("avg_heart_rate")
        max_hr = max((s.get("max_heart_rate") or 0 for s in sessions), default=0) or None
    else:
        distance = float(max((r.get("distance") or 0 for r in records), default=0))
        t_last = _utc(records[-1].get("timestamp"))
        duration = (t_last - start).total_seconds() if t_last else 0.0
        hrs = [r["heart_rate"] for r in records if r.get("heart_rate")]
        avg_hr = sum(hrs) / len(hrs) if hrs else None
        max_hr = max(hrs) if hrs else None

    hr_samples: list[tuple[float, int]] = []
    dist_samples: list[tuple[float, float]] = []
    for r in records:
        ts = _utc(r.get("timestamp"))
        hr = r.get("heart_rate")
        if ts is not None and hr:
            hr_samples.append(((ts - start).total_seconds(), int(hr)))
        if ts is not None and r.get("distance") is not None:
            dist_samples.append(((ts - start).total_seconds(), float(r["distance"])))

    laps: list[ParsedLap] = []
    for lap in laps_raw:
        ls = _utc(lap.get("start_time"))
        if ls is None:
            continue
        laps.append(ParsedLap(
            start=ls,
            elapsed_s=float(lap.get("total_elapsed_time") or 0),
            timer_s=float(lap.get("total_timer_time") or lap.get("total_elapsed_time") or 0),
            distance_m=float(lap.get("total_distance") or 0),
            avg_hr=lap.get("avg_heart_rate"),
            max_hr=lap.get("max_heart_rate"),
            intensity=str(lap.get("intensity") or ""),
            trigger=str(lap.get("lap_trigger") or ""),
        ))
    laps.sort(key=lambda x: x.start)

    dev_info = messages.get("device_info_mesgs") or []
    product_name = next((d.get("product_name") for d in dev_info if d.get("product_name") and d.get("device_index") in (0, "creator", None)), None)
    device = pretty_device(file_id.get("garmin_product"), product_name, file_id.get("manufacturer"))

    # Garmin watches write their VO2 max estimate after a run (max_met_data); prefer the running one.
    mets = [m for m in messages.get("max_met_data_mesgs") or [] if m.get("vo2_max")]
    mets.sort(key=lambda m: str(m.get("sport", "")) != "running")
    vo2 = float(mets[0]["vo2_max"]) if mets else None
    if vo2 is not None and not 10 <= vo2 <= 100:
        vo2 = None

    created = _utc(file_id.get("time_created")) or start
    serial = file_id.get("serial_number") or 0
    external_id = f"fit:{serial}:{int(created.timestamp())}"

    return ParsedActivity(
        external_id=external_id, start=start, duration_s=duration, distance_m=distance, sport=sport,
        avg_hr=float(avg_hr) if avg_hr else None, max_hr=float(max_hr) if max_hr else None,
        device=device, vo2_max=vo2, laps=laps, hr_samples=hr_samples, dist_samples=dist_samples,
    )


def fit_files(name: str, data: bytes, _depth: int = 0) -> list[tuple[str, bytes]]:
    """The .fit files in one upload: the file itself, or every .fit inside a .zip (zips inside zips too, as in
    Garmin's data export). Everything else in a zip is ignored. Raises FitError for a damaged or oversized zip."""
    if not data.startswith(b"PK\x03\x04"):
        return [(name, data)]
    if _depth > 3:
        return []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise FitError(f"{name} is not a readable .zip file.") from exc
    out: list[tuple[str, bytes]] = []
    for info in zf.infolist():
        low = info.filename.lower()
        if info.is_dir() or not low.endswith((".fit", ".zip")):
            continue
        limit = MAX_FIT_BYTES if low.endswith(".fit") else MAX_UPLOAD_BYTES
        if info.file_size > limit:  # sizes are checked before anything is unpacked
            continue
        with zf.open(info) as f:
            inner = f.read(limit + 1)
        if len(inner) > limit:
            continue
        out.extend([(info.filename, inner)] if low.endswith(".fit") else fit_files(info.filename, inner, _depth + 1))
        if len(out) > MAX_FITS_PER_UPLOAD:
            raise FitError(f"That upload holds more than {MAX_FITS_PER_UPLOAD} activity files. Upload it in smaller parts.")
    return out

