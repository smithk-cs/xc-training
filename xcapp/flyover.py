"""Course flyovers: a runner moving along a course's route with its elevation profile, mile markers, the section the
runner is in and what's there, for athletes to preview a course before they race it.

A flyover comes from a GPS track of the course (a GPX file from Strava, Garmin Connect or a mapping site, or a watch's
.fit file from someone running it), or from one of the built-in courses (flyover_presets/), which were traced by hand
from the official course maps and have a drawn base map and a satellite photo or official map to switch to.

The route is projected onto a flat 440 x 440 drawing (fine at the size of a cross country course), thinned to a point
about every 10 m, with the miles along it. Heights come from the track (smoothed, since GPS heights are noisy), or,
when the track has none, from Open-Meteo's elevation map (about 90 m detail). Either way they're approximate.

Coaches add what's at each point ('1.0 Big climb out of G') and name the course's sections ('1.56 Reservoir lap: H, D,
E to G'), one per line.
"""
from __future__ import annotations

import bisect
import json
import math
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

METERS_PER_MILE = 1609.344
FEET_PER_METER = 3.28084
MAX_TRACK_BYTES = 10 * 1024 * 1024
SIDE = 440.0          # the drawing is SIDE x SIDE, with the route inside a margin
MARGIN = 24.0
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
PRESET_DIR = Path(__file__).with_name("flyover_presets")


class FlyoverError(ValueError):
    pass


# ---------- reading a track ----------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_gpx(data: bytes) -> list[tuple[float, float, float | None]]:
    """(lat, lon, height in meters or None) for each track point, else each route point, in order."""
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise FlyoverError("That GPX file can't be read (it declares a document type).")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FlyoverError("That GPX file can't be read. Export it again and upload it.") from exc
    pts: dict[str, list] = {"trkpt": [], "rtept": []}
    for el in root.iter():
        kind = _local(el.tag)
        if kind not in pts:
            continue
        try:
            lat, lon = float(el.get("lat", "")), float(el.get("lon", ""))
        except ValueError:
            continue
        ele = None
        for child in el:
            if _local(child.tag) == "ele":
                try:
                    ele = float((child.text or "").strip())
                except ValueError:
                    ele = None
        pts[kind].append((lat, lon, ele))
    return pts["trkpt"] or pts["rtept"]


def parse_fit_track(data: bytes) -> list[tuple[float, float, float | None]]:
    """(lat, lon, height in meters or None) for each record with a position in a watch's .fit file."""
    from garmin_fit_sdk import Decoder, Stream
    dec = Decoder(Stream.from_byte_array(bytearray(data)))
    if not dec.is_fit():
        raise FlyoverError("That isn't a FIT file.")
    messages, errors = Decoder(Stream.from_byte_array(bytearray(data))).read(
        apply_scale_and_offset=True, convert_datetimes_to_dates=True, convert_types_to_strings=True,
        expand_sub_fields=True, expand_components=True)
    if errors and not messages.get("record_mesgs"):
        raise FlyoverError("That FIT file can't be read.")
    semi = 180.0 / 2 ** 31
    out = []
    for r in messages.get("record_mesgs") or []:
        lat, lon = r.get("position_lat"), r.get("position_long")
        if lat is None or lon is None:
            continue
        ele = r.get("enhanced_altitude", r.get("altitude"))
        out.append((lat * semi, lon * semi, float(ele) if ele is not None else None))
    return out


def read_track(filename: str, data: bytes) -> list[tuple[float, float, float | None]]:
    if not data:
        raise FlyoverError("Choose a GPX or FIT file first.")
    if len(data) > MAX_TRACK_BYTES:
        raise FlyoverError("That file is larger than 10 MB.")
    name = (filename or "").lower()
    if name.endswith(".fit") or data[8:12] == b".FIT":
        pts = parse_fit_track(data)
    elif name.endswith(".gpx") or data.lstrip()[:5] in (b"<?xml", b"<gpx ", b"<gpx>"):
        pts = parse_gpx(data)
    else:
        raise FlyoverError("Upload the course as a GPX file (from Strava, Garmin Connect or a route planner) or a watch's .fit file.")
    pts = [(lat, lon, ele) for lat, lon, ele in pts if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0)]
    if len(pts) < 10:
        raise FlyoverError("That file has too few GPS points for a course.")
    return pts


# ---------- building the flyover ----------

def _hav(a, b) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def fetch_elevations(coords: list[tuple[float, float]]) -> list[float] | None:
    """Heights (m) from Open-Meteo's elevation map for up to 100 points; None when it can't be reached."""
    import httpx
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0)) as http:
            r = http.get(ELEVATION_URL, params={"latitude": ",".join(f"{c[0]:.5f}" for c in coords),
                                                "longitude": ",".join(f"{c[1]:.5f}" for c in coords)})
            r.raise_for_status()
            out = r.json().get("elevation")
    except Exception:   # noqa: BLE001 - no heights is fine
        return None
    if not isinstance(out, list) or len(out) != len(coords):
        return None
    return [float(x) for x in out]


def nice_grid(lo: float, hi: float) -> list[int]:
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500):
        first = math.ceil(lo / step) * step
        lines = list(range(int(first), int(hi) + 1, step))
        if len(lines) <= 4:
            return lines
    return [int(lo), int(hi)]


def marker_label(m: float) -> str:
    whole = int(m)
    half = abs(m - whole - 0.5) < 1e-6
    return ("½" if not whole else f"{whole}½") + " mi" if half else f"{whole} mi"


def build(track: list[tuple[float, float, float | None]], lookup=fetch_elevations) -> dict:
    """The flyover drawing for a track: P [[x, y, mile]], E [[mile, feet]], view box, markers and figures."""
    cum = [0.0]
    for a, b in zip(track, track[1:]):
        cum.append(cum[-1] + _hav(a, b))
    total_m = cum[-1]
    if total_m < 400:
        raise FlyoverError("That track is shorter than 400 m. Is it the whole course?")
    if total_m > 25000:
        raise FlyoverError("That track is longer than 15 miles. Is it the course, or a whole day's run?")
    lat0 = sum(p[0] for p in track) / len(track)
    lon0 = sum(p[1] for p in track) / len(track)
    kx, ky = math.cos(math.radians(lat0)) * 111320.0, 110574.0
    xs = [(p[1] - lon0) * kx for p in track]
    ys = [-(p[0] - lat0) * ky for p in track]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    s = (SIDE - 2 * MARGIN) / max(w, h, 1.0)
    ox = MARGIN + (SIDE - 2 * MARGIN - w * s) / 2 - min(xs) * s
    oy = MARGIN + (SIDE - 2 * MARGIN - h * s) / 2 - min(ys) * s
    step = max(10.0, total_m / 700)
    P, last = [], None
    for i, (x, y) in enumerate(zip(xs, ys)):
        if last is None or cum[i] - last >= step or i == len(xs) - 1:
            P.append([round(x * s + ox, 1), round(y * s + oy, 1), round(cum[i] / METERS_PER_MILE, 4)])
            last = cum[i]
    total = P[-1][2]

    # Heights: the track's own when most points have one, else Open-Meteo's map, else none
    have = [(cum[i], p[2]) for i, p in enumerate(track) if p[2] is not None]
    source = "track"
    if len(have) < len(track) * 0.6:
        n = min(100, max(20, int(total_m / 40)))
        idx = [min(len(track) - 1, bisect.bisect_left(cum, total_m * k / (n - 1))) for k in range(n)]
        got = lookup([(track[i][0], track[i][1]) for i in idx]) if lookup else None
        have = [(cum[i], e) for i, e in zip(idx, got)] if got else []
        source = "map" if got else "none"
    E: list[list[float]] = []
    if have:
        hd = [d for d, _ in have]
        n = max(40, min(300, int(total_m / 15)))
        half = 35.0 if source == "track" else 0.0
        for k in range(n + 1):
            d = total_m * k / n
            if half:
                lo, hi = bisect.bisect_left(hd, d - half), bisect.bisect_right(hd, d + half)
                vals = [e for _, e in have[lo:hi]] or [have[min(len(have) - 1, lo)][1]]
                e = sum(vals) / len(vals)
            else:
                j = bisect.bisect_left(hd, d)
                if j <= 0:
                    e = have[0][1]
                elif j >= len(have):
                    e = have[-1][1]
                else:
                    (d0, e0), (d1, e1) = have[j - 1], have[j]
                    e = e0 + (e1 - e0) * (d - d0) / ((d1 - d0) or 1)
            E.append([round(d / METERS_PER_MILE, 4), round(e * FEET_PER_METER, 1)])
    else:
        E = [[0.0, 0.0], [total, 0.0]]
    feet = [e for _, e in E]
    lo, hi = min(feet), max(feet)
    pad = max(hi - lo, 30.0) * 0.15
    climb, base = 0.0, feet[0]
    for f in feet[1:]:          # count a rise once it's 3 ft above the last low, so GPS wobble isn't climbing
        if f < base:
            base = f
        elif f - base >= 3:
            climb += f - base
            base = f

    def at(m):
        for a, b in zip(P, P[1:]):
            if m <= b[2]:
                p = (m - a[2]) / ((b[2] - a[2]) or 1)
                return a[0] + (b[0] - a[0]) * p, a[1] + (b[1] - a[1]) * p
        return P[-1][0], P[-1][1]

    MK, m = [], 0.5
    while m < total - 0.05:
        (x0, y0), (x1, y1) = at(max(0, m - 0.02)), at(min(total, m + 0.02))
        nx, ny = -(y1 - y0), x1 - x0              # the left side of the direction of travel
        side = ("r" if nx > 0 else "l") if abs(nx) > abs(ny) else ("d" if ny > 0 else "u")
        MK.append([m, marker_label(m), side])
        m += 0.5
    i_hi, i_lo = feet.index(hi), feet.index(lo)
    return {
        "total": total, "vb": [0, 0, SIDE, SIDE], "P": P, "E": E, "MK": MK, "CP": [], "LAB": [], "base": "",
        "start": P[0][:2], "finish": P[-1][:2], "fmin": round(lo - pad), "fmax": round(hi + pad),
        "grid": nice_grid(lo - pad, hi + pad), "heights": source, "climb_ft": round(climb), "measured_mi": round(total, 2),
        "high": [E[i_hi][0], round(hi)], "low": [E[i_lo][0], round(lo)],
        # how drawing units map back to the earth, for the satellite photo (to_lat_lon)
        "geo": {"lat0": lat0, "lon0": lon0, "kx": kx, "ky": ky, "s": s, "ox": ox, "oy": oy},
    }


# ---------- satellite photo ----------
# Map tiles, stitched on the server when a flyover is made and kept with it, so pages load one picture from this site.
# Only USGS's imagery (The National Map), which is public domain: free to use, no key, and it covers the whole US.
# Where it has no imagery the flyover simply has no photo (no other source is used). An ArcGIS tile service:
# /tile/{z}/{y}/{x}, 256-pixel Web Mercator tiles.
TILE_SOURCES = [
    ("Imagery: USGS The National Map",
     "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"),
]
IMG_PAD = 130.0       # drawing units around the 440 square, so following the runner near an edge stays on the photo
MAX_IMG_PX = 2048
MAX_TILES = 200


def to_lat_lon(geo: dict, u: float, v: float) -> tuple[float, float]:
    """The latitude and longitude of a point in the drawing (build's projection, undone)."""
    return geo["lat0"] - (v - geo["oy"]) / geo["s"] / geo["ky"], geo["lon0"] + (u - geo["ox"]) / geo["s"] / geo["kx"]


def _tile_xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Web Mercator position in tiles (fractional) at zoom z."""
    n = 2 ** z
    lat = max(-85.0, min(85.0, lat))
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return (lon + 180.0) / 360.0 * n, y


def fetch_tile(url: str) -> bytes | None:
    import httpx
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), headers={"User-Agent": "xc-training course flyover"}) as http:
            r = http.get(url)
    except Exception:   # noqa: BLE001 - a missing tile is handled by the caller
        return None
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        return None
    return r.content


def _stitch(geo: dict, z: int, template: str, fetch) -> bytes | None:
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO

    from PIL import Image
    lat_n, lon_w = to_lat_lon(geo, -IMG_PAD, -IMG_PAD)
    lat_s, lon_e = to_lat_lon(geo, SIDE + IMG_PAD, SIDE + IMG_PAD)
    (x0, y0), (x1, y1) = _tile_xy(lat_n, lon_w, z), _tile_xy(lat_s, lon_e, z)
    tiles = [(tx, ty) for ty in range(int(y0), int(y1) + 1) for tx in range(int(x0), int(x1) + 1)]
    if not tiles or len(tiles) > MAX_TILES:
        return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda t: fetch(template.format(z=z, x=t[0], y=t[1])), tiles))
    canvas = Image.new("RGB", ((int(x1) - int(x0) + 1) * 256, (int(y1) - int(y0) + 1) * 256))
    missing = 0
    for (tx, ty), raw in zip(tiles, got):
        try:
            im = Image.open(BytesIO(raw)).convert("RGB") if raw else None
        except Exception:   # noqa: BLE001 - a broken tile counts as missing
            im = None
        # a tile all one shade is the service's "no imagery here" filler
        if im is None or max(hi - lo for lo, hi in im.getextrema()) < 6:
            missing += 1
            continue
        canvas.paste(im.resize((256, 256)), ((tx - int(x0)) * 256, (ty - int(y0)) * 256))
    if missing > len(tiles) * 0.05:
        return None
    box = tuple(round(v) for v in ((x0 - int(x0)) * 256, (y0 - int(y0)) * 256, (x1 - int(x0)) * 256, (y1 - int(y0)) * 256))
    side = min(MAX_IMG_PX, max(box[2] - box[0], box[3] - box[1]))
    out = BytesIO()
    canvas.crop(box).resize((side, side), Image.LANCZOS).save(out, "JPEG", quality=80, optimize=True, progressive=True)
    return out.getvalue()


def satellite_image(geo: dict | None, fetch=fetch_tile) -> tuple[bytes, str] | None:
    """(JPEG, credit) of the satellite photo covering the drawing and IMG_PAD around it, or None when no source has
    imagery for the whole of it. MAX_IMG_PX across, from the zoom just more detailed than that (sharper when shrunk)."""
    if not geo:
        return None
    meters = (SIDE + 2 * IMG_PAD) / geo["s"]
    per_px = 156543.03392 * math.cos(math.radians(geo["lat0"]))      # meters per pixel at zoom 0
    best = max(10, min(18, math.ceil(math.log2(per_px * MAX_IMG_PX / meters))))   # then shrunk to MAX_IMG_PX
    for credit, template in TILE_SOURCES:
        for z in (best, best - 1):
            img = _stitch(geo, z, template, fetch)
            if img:
                return img, credit
    return None


def default_notes(d: dict) -> str:
    """Starting notes for a new flyover, for the coach to edit: the start, the high and low points, the finish."""
    lines = [(0.0, "Start")]
    if d.get("heights") != "none" and d["high"][1] - d["low"][1] >= 15:
        lines += [(d["high"][0], f"High point, {d['high'][1]:,} ft"), (d["low"][0], f"Low point, {d['low'][1]:,} ft")]
    lines.append((max(0.0, d["total"] - 0.2), "Kick to the finish"))
    return "\n".join(f"{m:.2f} {t}" for m, t in sorted(lines))


def about_text(d: dict, filename: str) -> str:
    src = {"track": "Heights come from the file's GPS, smoothed", "map": "The file had no heights, so they come from an "
           "elevation map (Open-Meteo, about 90 m detail)", "none": "The file had no heights and none could be looked up, "
           "so the profile is flat"}[d.get("heights", "track")]
    return (f"Route from {filename or 'a GPS track'} ({d['measured_mi']:.2f} miles as measured). {src}, so treat them as "
            f"approximate. About {d['climb_ft']:,} ft of climbing.")


# ---------- coaches' notes and sections ----------

LINE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:mi(?:les?)?\b)?\s*[-:–—]?\s*(.+?)\s*$")


def parse_notes(text: str, total: float) -> list[list]:
    """'1.0 Big climb out of G' lines -> [[1.0, 'Big climb out of G'], ...], in order, within the course."""
    out = []
    for line in (text or "").splitlines():
        m = LINE.match(line)
        if m and m.group(2):
            out.append([min(float(m.group(1)), total), m.group(2)[:120]])
    out.sort(key=lambda x: x[0])
    return out[:60]


def parse_sections(text: str, total: float) -> list[list]:
    """'1.56 Reservoir lap: H, D, E to G' lines (where each section starts) -> [[start, end, name, detail], ...]."""
    starts = []
    for line in (text or "").splitlines():
        m = LINE.match(line)
        if not m:
            continue
        name, _, detail = m.group(2).partition(":")
        if name.strip():
            starts.append((min(float(m.group(1)), total), name.strip()[:60], detail.strip()[:120]))
    starts.sort(key=lambda x: x[0])
    if not starts:
        return []
    starts[0] = (0.0, *starts[0][1:])
    ends = [s[0] for s in starts[1:]] + [total]
    return [[round(s[0], 3), round(e, 3), s[1], s[2]] for s, e in zip(starts, ends) if e > s[0]][:20]


def notes_text(rows) -> str:
    return "\n".join(f"{m:g} {t}" for m, t in rows)


def sections_text(rows) -> str:
    return "\n".join(f"{r[0]:g} {r[2]}" + (f": {r[3]}" if r[3] else "") for r in rows)


# ---------- built-in courses ----------

@lru_cache(maxsize=1)
def presets() -> dict[str, dict]:
    out = {}
    for f in sorted(PRESET_DIR.glob("*.json")):
        d = json.loads(f.read_text())
        out[d["id"]] = d
    return out


def preset_record(pid: str) -> tuple[dict, str, str]:
    """(drawing data, notes text, sections text) for a built-in course."""
    p = dict(presets()[pid])
    notes, sections = p.pop("notes", []), p.pop("sections", [])
    for k in ("id", "distance"):
        p.pop(k, None)
    p.setdefault("heights", "track")
    return p, notes_text(notes), sections_text(sections)


def view_data(fly, course, satellite_url: str = "") -> dict:
    """What the flyover page draws: the saved drawing with the coach's notes and sections, title and place, and the
    satellite photo under it when there is one."""
    d = json.loads(fly.data or "{}")
    d.pop("geo", None)
    sat = fly.satellite
    if sat is not None and satellite_url:
        d["satellite"] = {"href": satellite_url, "x": sat.x, "y": sat.y, "w": sat.size, "h": sat.size, "credit": sat.credit}
    total = d.get("total") or 0
    d["notes"] = parse_notes(fly.notes, total) or [[0, "Start"]]
    d["sections"] = parse_sections(fly.sections, total)
    d["title"] = f"{course.name} {fly.distance}".strip()
    d["place"] = d.get("place") if fly.preset else (course.location or "")
    if not fly.preset:
        d["about"] = about_text(d, fly.filename)
    return d
