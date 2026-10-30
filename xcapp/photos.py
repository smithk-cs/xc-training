"""Athlete photos: shrunk to a small JPEG with any hidden data (such as location) removed, and matched to the roster by
file name when a coach uploads a .zip of them."""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from pathlib import PurePosixPath

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Athlete, AthletePhoto

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_ZIP_BYTES = 100 * 1024 * 1024
SIZE = 400  # longest side, pixels
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
Image.MAX_IMAGE_PIXELS = 40_000_000


class PhotoError(ValueError):
    pass


def prepare(data: bytes, size: int = SIZE) -> bytes:
    """Any common image -> a JPEG no larger than size (default SIZE) on its longest side, without EXIF or other metadata."""
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise PhotoError("Photos must be smaller than 10 MB.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise PhotoError("That file is not a photo the app can read (use JPEG, PNG or WebP).") from exc
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((size, size))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def name_key(text: str) -> str:
    """Letters only, lower case, accents removed: 'José O’Neil' -> 'joseoneil'."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", plain.lower())


def keys_for_file(stem: str) -> list[str]:
    """'Last, First', 'First Last', 'first-last', 'first_last_2026' -> keys to try, first-name-first then reversed."""
    stem = re.sub(r"[_\-.]+", " ", stem)
    stem = re.sub(r"\b\d+\b", " ", stem).strip()
    if "," in stem:
        last, first = (x.strip() for x in stem.split(",", 1))
        parts = first.split() + last.split()
    else:
        parts = stem.split()
    if not parts:
        return []
    return [name_key(" ".join(parts)), name_key(" ".join(parts[-1:] + parts[:-1]))]


def save(db: Session, athlete: Athlete, data: bytes) -> None:
    jpeg = prepare(data)
    if athlete.photo is None:
        athlete.photo = AthletePhoto(media_type="image/jpeg", data=jpeg)
    else:
        athlete.photo.media_type, athlete.photo.data = "image/jpeg", jpeg


def import_zip(db: Session, data: bytes) -> tuple[list[str], list[str]]:
    """Save every photo in the zip whose file name matches an active athlete. Returns (athletes updated, files not
    matched or not readable)."""
    if len(data) > MAX_ZIP_BYTES:
        raise PhotoError("That .zip is larger than 100 MB.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PhotoError("That is not a readable .zip file.") from exc
    by_key: dict[str, list[Athlete]] = {}
    for a in db.scalars(select(Athlete).where(Athlete.active.is_(True))).all():
        for n in [f"{a.first_name} {a.last_name}"] + re.split(r"[;\n]", a.other_names or ""):
            if n.strip() and a not in by_key.setdefault(name_key(n), []):
                by_key[name_key(n)].append(a)
    done, missed = [], []
    for info in zf.infolist():
        path = PurePosixPath(info.filename)
        if info.is_dir() or path.suffix.lower() not in IMAGE_EXT or path.name.startswith("."):
            continue
        match = next((by_key[k] for k in keys_for_file(path.stem) if len(by_key.get(k, [])) == 1), None)
        if match is None or info.file_size > MAX_PHOTO_BYTES:
            missed.append(path.name)
            continue
        try:
            save(db, match[0], zf.read(info))
            done.append(match[0].name)
        except PhotoError:
            missed.append(path.name)
    return done, missed
