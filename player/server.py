from __future__ import annotations

import argparse
import colorsys
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import threading
import time
from math import ceil, sqrt
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .audio import MpvPlayer, default_ipc_path
from .display import create_display


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
COVER_STEMS = {"cover", "folder", "front", "album", "artwork"}
DEFAULT_HOME_THEME = {
    "bg": "#05050a",
    "panel": "#0d0e16",
    "header": "#110b1f",
    "row": "#0d0e16",
    "rowHot": "#47237b",
    "edge": "#be7aff",
    "text": "#f5efff",
    "muted": "#a08fbc",
    "cyan": "#5ddeff",
    "purple": "#be7aff",
    "grid": "#191128",
}
DEFAULT_STATE = {
    "current_track_id": None,
    "queue": [],
    "queue_name": "All Songs",
    "paused": False,
    "volume": 70,
    "ui_screen": "home",
    "ui_path": ["Home"],
    "ui_cursor": 0,
    "ui_message": "READY",
    "updated_at": None,
}


HOME = Path(os.environ.get("PLAYER_HOME", Path.cwd() / ".sym-data")).resolve()
LIBRARY_DIR = Path(os.environ.get("PLAYER_LIBRARY", HOME / "library")).resolve()
PLAYLIST_DIR = Path(os.environ.get("PLAYER_PLAYLISTS", HOME / "playlists")).resolve()
ASSETS_DIR = Path(os.environ.get("PLAYER_ASSETS", HOME / "assets")).resolve()
STATE_FILE = Path(os.environ.get("PLAYER_STATE", HOME / "state.json")).resolve()

LOCK = threading.RLock()
DISPLAY_LOCK = threading.RLock()
PLAYER = MpvPlayer(default_ipc_path(HOME))
STATE = DEFAULT_STATE.copy()
DISPLAY = create_display()
DISPLAY_LAST_SIGNATURE = ""
DISPLAY_LAST_INPUT_REINIT = 0.0
METADATA_CACHE: dict[tuple[str, int, int], dict[str, str]] = {}
THEME_CACHE: dict[str, dict[str, str]] = {}
LIBRARY_CACHE: list[dict[str, object]] | None = None
LIBRARY_CACHE_EXPIRES = 0.0
LIBRARY_CACHE_TTL = 3600.0
STORAGE_CACHE: dict[str, object] | None = None
STORAGE_CACHE_EXPIRES = 0.0
STORAGE_CACHE_TTL = 3600.0
HOME_FOCUS_ACTIONS = {
    0: "menu",
    1: "previous",
    2: "playpause",
    3: "next",
}
HOME_FOCUS_LABELS = {
    0: "MENU",
    1: "PREV",
    2: "PLAY",
    3: "NEXT",
}
HOME_FOCUS_X = {
    0: 117,
    1: 43,
    2: 120,
    3: 197,
}
HOME_TOP_FOCUS = (0,)
HOME_BOTTOM_FOCUS = (1, 2, 3)


def ensure_dirs() -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)


def slugify(value: str, fallback: str = "playlist") -> str:
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "", value).strip().replace(" ", "_")
    clean = re.sub(r"_+", "_", clean)
    return clean[:80] or fallback


def track_id_for(path: Path) -> str:
    rel = path.relative_to(LIBRARY_DIR).as_posix()
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def clean_display_name(value: str) -> str:
    return re.sub(r"^[\s._-]*\d{1,3}[\s._-]+", "", value).strip() or value


def media_url(relative_path: str) -> str:
    return "/library-art/" + relative_path.replace("\\", "/")


def album_info_for_relative_path(relative_path: str) -> tuple[str, str]:
    parts = [part for part in relative_path.replace("\\", "/").split("/") if part]
    if len(parts) >= 3:
        return parts[-2], parts[-3]
    if len(parts) >= 2:
        album = parts[0]
        if " - " in album:
            artist, title = album.split(" - ", 1)
            return title.strip() or album, artist.strip()
        return album, ""
    return "Loose Files", ""


def empty_cover_metadata() -> dict[str, str]:
    return {"cover": "", "cover_pixel": "", "cover_lcd": "", "cover_lcd_path": ""}


def asset_url(path: Path) -> str:
    return "/assets/" + path.resolve().relative_to(ASSETS_DIR).as_posix()


def asset_path(value: str) -> Path | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    if clean.startswith("/assets/"):
        path = (ASSETS_DIR / clean.removeprefix("/assets/")).resolve()
    elif clean.startswith("../assets/"):
        path = (PLAYLIST_DIR / clean).resolve()
    elif clean.startswith("assets/"):
        path = (HOME / clean).resolve()
    else:
        path = (PLAYLIST_DIR / clean).resolve()
    if path.is_file() and path.is_relative_to(ASSETS_DIR):
        return path
    return None


def crop_square_image(image: object) -> object:
    width, height = image.size  # type: ignore[attr-defined]
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    return image.crop((left, top, left + edge, top + edge))  # type: ignore[attr-defined]


def generated_cover_metadata(source: Path, key: str) -> dict[str, str]:
    source = source.resolve()
    if not source.is_file():
        return empty_cover_metadata()
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps  # type: ignore[import-not-found]
    except Exception:
        return {"cover": asset_url(source) if source.is_relative_to(ASSETS_DIR) else media_url(source.relative_to(LIBRARY_DIR).as_posix()), "cover_pixel": "", "cover_lcd": "", "cover_lcd_path": ""}

    stem = slugify(key, "cover").lower()
    preview_dir = ASSETS_DIR / "covers" / "preview"
    lcd_dir = ASSETS_DIR / "covers" / "lcd"
    preview_dir.mkdir(parents=True, exist_ok=True)
    lcd_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{stem}_16color_64.png"
    lcd_path = lcd_dir / f"{stem}_48x48_mono.png"

    try:
        if not preview_path.exists() or not lcd_path.exists():
            image = Image.open(source).convert("RGB")
            square = crop_square_image(image)

            if not preview_path.exists():
                color_16 = square.resize((64, 64), Image.Resampling.BOX).quantize(colors=16, method=Image.Quantize.MEDIANCUT)
                color_16.convert("RGB").save(preview_path)

            if not lcd_path.exists():
                gray = ImageOps.grayscale(square)
                gray = ImageOps.autocontrast(gray, cutoff=2)
                gray = ImageEnhance.Contrast(gray).enhance(1.8)
                gray = gray.filter(ImageFilter.UnsharpMask(radius=1, percent=180, threshold=2))
                mono = gray.resize((48, 48), Image.Resampling.BOX).convert("1", dither=Image.Dither.FLOYDSTEINBERG)
                mono.save(lcd_path)
    except Exception:
        return {"cover": asset_url(source) if source.is_relative_to(ASSETS_DIR) else media_url(source.relative_to(LIBRARY_DIR).as_posix()), "cover_pixel": "", "cover_lcd": "", "cover_lcd_path": ""}

    source_url = asset_url(source) if source.is_relative_to(ASSETS_DIR) else media_url(source.relative_to(LIBRARY_DIR).as_posix())
    return {
        "cover": source_url,
        "cover_pixel": asset_url(preview_path),
        "cover_lcd": asset_url(lcd_path),
        "cover_lcd_path": str(lcd_path),
    }


def cover_metadata_from_value(value: str, key: str) -> dict[str, str]:
    path = asset_path(value)
    if path:
        metadata = generated_cover_metadata(path, key)
        if metadata.get("cover"):
            return metadata
    return empty_cover_metadata()


def cover_file_path(value: str) -> Path | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    if clean.startswith("/assets/"):
        path = (ASSETS_DIR / clean.removeprefix("/assets/")).resolve()
    elif clean.startswith("/library-art/"):
        path = (LIBRARY_DIR / clean.removeprefix("/library-art/")).resolve()
    elif clean.startswith("../assets/"):
        path = (PLAYLIST_DIR / clean).resolve()
    elif clean.startswith("../library/"):
        path = (PLAYLIST_DIR / clean).resolve()
    elif clean.startswith("assets/"):
        path = (HOME / clean).resolve()
    elif clean.startswith("library/"):
        path = (HOME / clean).resolve()
    else:
        path = (PLAYLIST_DIR / clean).resolve()
    if path.is_file() and (path.is_relative_to(ASSETS_DIR) or path.is_relative_to(LIBRARY_DIR)):
        return path
    return None


def fit_image_to_box(image: object, width: int, height: int, Image: object) -> object:
    source_width, source_height = image.size  # type: ignore[attr-defined]
    scale = max(width / max(1, source_width), height / max(1, source_height))
    resized_width = max(1, int(ceil(source_width * scale)))
    resized_height = max(1, int(ceil(source_height * scale)))
    resized = image.resize((resized_width, resized_height), Image.Resampling.LANCZOS)  # type: ignore[attr-defined]
    left = max(0, (resized_width - width) // 2)
    top = max(0, (resized_height - height) // 2)
    return resized.crop((left, top, left + width, top + height))  # type: ignore[attr-defined]


def clamp_channel(value: float) -> int:
    return max(0, min(255, int(round(value))))


def rgb_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{clamp_channel(channel):02x}" for channel in rgb)


def blend_rgb(base: tuple[int, int, int], overlay: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return tuple(clamp_channel(base[index] * (1 - amount) + overlay[index] * amount) for index in range(3))  # type: ignore[return-value]


def color_distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    return sum((left[index] - right[index]) ** 2 for index in range(3)) ** 0.5


def hue_distance(left: float, right: float) -> float:
    delta = abs(left - right) % 1.0
    return min(delta, 1.0 - delta)


def tune_accent(rgb: tuple[int, int, int], target_light: float = 0.63, min_saturation: float = 0.55) -> tuple[int, int, int]:
    red, green, blue = [channel / 255 for channel in rgb]
    hue, light, saturation = colorsys.rgb_to_hls(red, green, blue)
    saturation = max(min_saturation, min(0.92, saturation + 0.08))
    light = max(0.48, min(0.74, (light * 0.35) + (target_light * 0.65)))
    tuned = colorsys.hls_to_rgb(hue, light, saturation)
    return tuple(clamp_channel(channel * 255) for channel in tuned)  # type: ignore[return-value]


def rotated_accent(rgb: tuple[int, int, int], turn: float = 0.42) -> tuple[int, int, int]:
    red, green, blue = [channel / 255 for channel in rgb]
    hue, light, saturation = colorsys.rgb_to_hls(red, green, blue)
    tuned = colorsys.hls_to_rgb((hue + turn) % 1.0, max(0.58, min(0.72, light)), max(0.62, saturation))
    return tuple(clamp_channel(channel * 255) for channel in tuned)  # type: ignore[return-value]


def theme_for_track(track: dict[str, object] | None, is_playing: bool) -> dict[str, str]:
    if not track or not is_playing:
        return DEFAULT_HOME_THEME
    cover_path = None
    for key in ("album_cover", "album_cover_pixel", "cover", "cover_pixel", "album_cover_lcd"):
        cover_path = cover_file_path(str(track.get(key) or ""))
        if cover_path:
            break
    if not cover_path:
        return DEFAULT_HOME_THEME
    try:
        stat = cover_path.stat()
    except OSError:
        return DEFAULT_HOME_THEME
    cache_key = f"{cover_path}:{stat.st_mtime_ns}:{stat.st_size}"
    if cache_key in THEME_CACHE:
        return THEME_CACHE[cache_key]
    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(cover_path) as opened:
            image = opened.convert("RGB").resize((64, 64))
        quantized = image.quantize(colors=14, method=Image.Quantize.MEDIANCUT).convert("RGB")
        samples = quantized.getcolors(4096) or []
    except Exception:
        return DEFAULT_HOME_THEME

    candidates: list[tuple[float, float, tuple[int, int, int]]] = []
    for count, rgb in samples:
        red, green, blue = [channel / 255 for channel in rgb]
        hue, light, saturation = colorsys.rgb_to_hls(red, green, blue)
        if light < 0.08 or light > 0.92:
            continue
        score = count * (0.35 + saturation) * (1.15 - abs(light - 0.56))
        if saturation < 0.12:
            score *= 0.35
        candidates.append((score, hue, rgb))
    if not candidates:
        return DEFAULT_HOME_THEME

    candidates.sort(key=lambda item: item[0], reverse=True)
    accent_raw = candidates[0][2]
    accent_hue = candidates[0][1]
    secondary_raw = None
    for _, hue, rgb in candidates[1:]:
        if hue_distance(accent_hue, hue) >= 0.16 and color_distance(accent_raw, rgb) >= 62:
            secondary_raw = rgb
            break
    accent = tune_accent(accent_raw, 0.64, 0.58)
    secondary = tune_accent(secondary_raw, 0.67, 0.56) if secondary_raw else rotated_accent(accent)
    if color_distance(accent, secondary) < 92:
        secondary = rotated_accent(accent)

    black = (5, 5, 10)
    white = (248, 240, 255)
    bg = blend_rgb(black, accent, 0.08)
    bg = blend_rgb(bg, secondary, 0.04)
    panel = blend_rgb(bg, accent, 0.13)
    header = blend_rgb(bg, secondary, 0.12)
    row_hot = blend_rgb(bg, accent, 0.38)
    muted = blend_rgb(white, blend_rgb(accent, secondary, 0.35), 0.34)
    theme = {
        "bg": rgb_hex(bg),
        "panel": rgb_hex(panel),
        "header": rgb_hex(header),
        "row": rgb_hex(blend_rgb(bg, accent, 0.08)),
        "rowHot": rgb_hex(row_hot),
        "edge": rgb_hex(accent),
        "text": rgb_hex(white),
        "muted": rgb_hex(muted),
        "cyan": rgb_hex(secondary),
        "purple": rgb_hex(accent),
        "grid": rgb_hex(blend_rgb(bg, secondary, 0.16)),
    }
    THEME_CACHE[cache_key] = theme
    return theme


def playlist_album_cover_paths(track_ids: list[str], by_id: dict[str, dict[str, object]]) -> list[Path]:
    seen_albums: set[str] = set()
    seen_paths: set[Path] = set()
    paths: list[Path] = []
    for track_id in track_ids:
        track = by_id.get(str(track_id))
        if not track:
            continue
        album_key = "|".join(
            [
                str(track.get("artist") or "").strip().lower(),
                str(track.get("album") or "").strip().lower(),
            ]
        )
        if album_key in seen_albums:
            continue
        for key in ("album_cover", "album_cover_pixel", "album_cover_lcd"):
            path = cover_file_path(str(track.get(key) or ""))
            if path and path not in seen_paths:
                seen_albums.add(album_key)
                seen_paths.add(path)
                paths.append(path)
                break
    return paths


def playlist_mosaic_metadata(slug: str, track_ids: list[str], by_id: dict[str, dict[str, object]]) -> dict[str, str]:
    sources = playlist_album_cover_paths(track_ids, by_id)
    if not sources:
        return empty_cover_metadata()
    signature_parts = [slug, *[str(track_id) for track_id in track_ids]]
    for source in sources:
        try:
            stat = source.stat()
            signature_parts.append(f"{source}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            signature_parts.append(str(source))
    digest = hashlib.sha1("|".join(signature_parts).encode("utf-8")).hexdigest()[:12]
    stem = f"{slugify(slug, 'playlist').lower()}_{digest}_mosaic"
    source_dir = ASSETS_DIR / "covers" / "playlists"
    preview_dir = ASSETS_DIR / "covers" / "preview"
    lcd_dir = ASSETS_DIR / "covers" / "lcd"
    source_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)
    lcd_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / f"{stem}.png"
    preview_path = preview_dir / f"{stem}_16color_64.png"
    lcd_path = lcd_dir / f"{stem}_48x48_mono.png"
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps  # type: ignore[import-not-found]
    except Exception:
        return generated_cover_metadata(sources[0], slug)

    try:
        if not source_path.exists():
            size = 256
            mosaic = Image.new("RGB", (size, size), (5, 5, 10))
            count = len(sources)
            columns = max(1, ceil(sqrt(count)))
            rows = max(1, ceil(count / columns))
            index = 0
            for row in range(rows):
                remaining = count - index
                cells = min(columns, remaining)
                if cells <= 0:
                    break
                y0 = round(row * size / rows)
                y1 = round((row + 1) * size / rows)
                for cell in range(cells):
                    x0 = round(cell * size / cells)
                    x1 = round((cell + 1) * size / cells)
                    with Image.open(sources[index]) as opened:
                        cover_image = opened.convert("RGB")
                    tile = fit_image_to_box(cover_image, x1 - x0, y1 - y0, Image)
                    mosaic.paste(tile, (x0, y0))
                    index += 1
            mosaic.save(source_path)

        if not preview_path.exists() or not lcd_path.exists():
            image = Image.open(source_path).convert("RGB")
            if not preview_path.exists():
                color_16 = image.resize((64, 64), Image.Resampling.BOX).quantize(colors=16, method=Image.Quantize.MEDIANCUT)
                color_16.convert("RGB").save(preview_path)
            if not lcd_path.exists():
                gray = ImageOps.grayscale(image)
                gray = ImageOps.autocontrast(gray, cutoff=2)
                gray = ImageEnhance.Contrast(gray).enhance(1.8)
                gray = gray.filter(ImageFilter.UnsharpMask(radius=1, percent=180, threshold=2))
                gray.resize((48, 48), Image.Resampling.BOX).convert("1", dither=Image.Dither.FLOYDSTEINBERG).save(lcd_path)
    except Exception:
        return generated_cover_metadata(sources[0], slug)

    return {
        "cover": asset_url(source_path),
        "cover_pixel": asset_url(preview_path),
        "cover_lcd": asset_url(lcd_path),
        "cover_lcd_path": str(lcd_path),
    }


def cover_metadata_for_album_dir(album_dir: Path, cache: dict[Path, dict[str, str]]) -> dict[str, str]:
    if album_dir in cache:
        return cache[album_dir]
    metadata = empty_cover_metadata()
    if album_dir.is_dir():
        candidates = sorted(album_dir.iterdir(), key=lambda item: item.name.lower())
        for item in candidates:
            if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS and item.stem.lower() in COVER_STEMS:
                metadata = generated_cover_metadata(item, track_id_for(next((audio for audio in album_dir.rglob("*") if audio.is_file() and audio.suffix.lower() in AUDIO_EXTENSIONS), item)))
                break
        if not metadata.get("cover"):
            for item in candidates:
                if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS:
                    metadata = generated_cover_metadata(item, track_id_for(next((audio for audio in album_dir.rglob("*") if audio.is_file() and audio.suffix.lower() in AUDIO_EXTENSIONS), item)))
                    break
    cache[album_dir] = metadata
    return metadata


def write_embedded_cover(track_path: Path, data: bytes, mime: str) -> dict[str, str]:
    if not data:
        return empty_cover_metadata()
    ext = ".png" if "png" in mime.lower() else ".jpg"
    output_dir = ASSETS_DIR / "embedded_covers"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{track_id_for(track_path)}{ext}"
    if not output.exists():
        output.write_bytes(data)
    return generated_cover_metadata(output, track_id_for(track_path))


def first_tag_value(tags: object, keys: tuple[str, ...]) -> str:
    for key in keys:
        try:
            value = tags.get(key)  # type: ignore[attr-defined]
        except AttributeError:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def extract_cover_from_tags(track_path: Path, audio: object) -> dict[str, str]:
    tags = getattr(audio, "tags", None)
    pictures = getattr(audio, "pictures", None)
    if pictures:
        for picture in pictures:
            data = getattr(picture, "data", b"")
            mime = getattr(picture, "mime", "image/jpeg")
            cover = write_embedded_cover(track_path, data, mime)
            if cover.get("cover"):
                return cover

    if not tags:
        return empty_cover_metadata()

    try:
        values = tags.values()  # type: ignore[attr-defined]
    except AttributeError:
        values = []
    for value in values:
        data = getattr(value, "data", None)
        mime = getattr(value, "mime", "image/jpeg")
        if isinstance(data, bytes):
            cover = write_embedded_cover(track_path, data, mime)
            if cover.get("cover"):
                return cover

    covr = None
    try:
        covr = tags.get("covr")  # type: ignore[attr-defined]
    except AttributeError:
        pass
    if covr:
        image = covr[0] if isinstance(covr, list) else covr
        data = bytes(image)
        fmt = getattr(image, "imageformat", None)
        mime = "image/png" if fmt == 14 else "image/jpeg"
        return write_embedded_cover(track_path, data, mime)

    return empty_cover_metadata()


def audio_metadata(path: Path) -> dict[str, str]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key in METADATA_CACHE:
        return METADATA_CACHE[key]

    metadata = {"title": "", "artist": "", "album": "", **empty_cover_metadata()}
    try:
        from mutagen import File as MutagenFile  # type: ignore[import-not-found]
    except Exception:
        METADATA_CACHE[key] = metadata
        return metadata

    try:
        easy = MutagenFile(path, easy=True)
        if easy and getattr(easy, "tags", None):
            metadata["title"] = first_tag_value(easy.tags, ("title",))
            metadata["artist"] = first_tag_value(easy.tags, ("artist", "albumartist", "albumartistsort"))
            metadata["album"] = first_tag_value(easy.tags, ("album",))
    except Exception:
        pass

    try:
        full = MutagenFile(path)
        if full:
            metadata.update(extract_cover_from_tags(path, full))
    except Exception:
        pass

    METADATA_CACHE[key] = metadata
    return metadata


def invalidate_library_cache() -> None:
    global LIBRARY_CACHE, LIBRARY_CACHE_EXPIRES
    with LOCK:
        LIBRARY_CACHE = None
        LIBRARY_CACHE_EXPIRES = 0.0


def invalidate_storage_cache() -> None:
    global STORAGE_CACHE, STORAGE_CACHE_EXPIRES
    with LOCK:
        STORAGE_CACHE = None
        STORAGE_CACHE_EXPIRES = 0.0


def scan_library(force: bool = False) -> list[dict[str, object]]:
    global LIBRARY_CACHE, LIBRARY_CACHE_EXPIRES
    now = time.monotonic()
    with LOCK:
        if not force and LIBRARY_CACHE is not None and now < LIBRARY_CACHE_EXPIRES:
            return [track.copy() for track in LIBRARY_CACHE]

    ensure_dirs()
    tracks: list[dict[str, object]] = []
    cover_cache: dict[Path, dict[str, str]] = {}
    for path in sorted(LIBRARY_DIR.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(LIBRARY_DIR).parts):
            continue
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        stat = path.stat()
        rel = path.relative_to(LIBRARY_DIR).as_posix()
        album, artist = album_info_for_relative_path(rel)
        metadata = audio_metadata(path)
        title = metadata.get("title") or clean_display_name(path.stem)
        artist = metadata.get("artist") or artist
        album = metadata.get("album") or album
        folder_cover = cover_metadata_for_album_dir(path.parent, cover_cache)
        cover = metadata.get("cover") or folder_cover.get("cover", "")
        cover_pixel = metadata.get("cover_pixel") or folder_cover.get("cover_pixel", "")
        cover_lcd = metadata.get("cover_lcd") or folder_cover.get("cover_lcd", "")
        cover_lcd_path = metadata.get("cover_lcd_path") or folder_cover.get("cover_lcd_path", "")
        tracks.append(
            {
                "id": track_id_for(path),
                "name": title,
                "filename": path.name,
                "relative_path": rel,
                "extension": path.suffix.lower(),
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
                "artist": artist,
                "album": album,
                "album_cover": cover,
                "album_cover_pixel": cover_pixel,
                "album_cover_lcd": cover_lcd,
                "album_cover_lcd_path": cover_lcd_path,
            }
        )
    with LOCK:
        LIBRARY_CACHE = [track.copy() for track in tracks]
        LIBRARY_CACHE_EXPIRES = time.monotonic() + LIBRARY_CACHE_TTL
    return tracks


def tracks_by_id() -> dict[str, dict[str, object]]:
    return {str(track["id"]): track for track in scan_library()}


def album_name_for_track(track: dict[str, object]) -> str:
    return str(track.get("album") or "Loose Files")


def list_albums() -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for track in scan_library():
        grouped.setdefault(album_name_for_track(track), []).append(track)
    return [
        {
            "name": name,
            "slug": slugify(name, "album"),
            "track_ids": [str(track["id"]) for track in tracks],
            "count": len(tracks),
            "artist": str(tracks[0].get("artist") or ""),
            "cover_source": str(next((track.get("album_cover") for track in tracks if track.get("album_cover")), "")),
            "cover": str(next((track.get("album_cover_pixel") or track.get("album_cover") for track in tracks if track.get("album_cover_pixel") or track.get("album_cover")), "")),
            "cover_lcd": str(next((track.get("album_cover_lcd") for track in tracks if track.get("album_cover_lcd")), "")),
            "cover_lcd_path": str(next((track.get("album_cover_lcd_path") for track in tracks if track.get("album_cover_lcd_path")), "")),
        }
        for name, tracks in sorted(grouped.items(), key=lambda item: item[0].lower())
    ]


def album_by_slug(slug: str) -> dict[str, object] | None:
    clean_slug = slugify(slug, "album")
    for album in list_albums():
        if str(album["slug"]) == clean_slug:
            return album
    return None


def path_for_track(track_id: str) -> Path:
    track = tracks_by_id().get(track_id)
    if not track:
        raise KeyError(f"Track not found: {track_id}")
    path = (LIBRARY_DIR / str(track["relative_path"])).resolve()
    if not path.is_file() or not path.is_relative_to(LIBRARY_DIR):
        raise KeyError(f"Track path is invalid: {track_id}")
    return path


def playlist_path(slug: str) -> Path:
    return PLAYLIST_DIR / f"{slugify(slug)}.m3u8"


def load_state() -> None:
    global STATE
    if not STATE_FILE.exists():
        STATE = DEFAULT_STATE.copy()
        return
    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        STATE = DEFAULT_STATE.copy()
        return
    merged = DEFAULT_STATE.copy()
    merged.update({k: v for k, v in loaded.items() if k in merged})
    if merged.get("queue_name") == "All Tracks":
        merged["queue_name"] = "All Songs"
    STATE = merged


def save_state() -> None:
    STATE["updated_at"] = int(time.time())
    STATE_FILE.write_text(json.dumps(STATE, indent=2), encoding="utf-8")
    update_display()


def player_status() -> dict[str, object]:
    snapshot = PLAYER.snapshot()
    return {
        "backend": "mpv",
        "available": PLAYER.available,
        "running": snapshot.get("running", False),
        "audio_driver": snapshot.get("audio_driver", ""),
        "audio_device": snapshot.get("audio_device", ""),
        "alsa_card": snapshot.get("alsa_card", ""),
        "alsa_volume": snapshot.get("alsa_volume", ""),
        "install_hint": "sudo apt update && sudo apt install -y mpv",
    }


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def storage_payload(force: bool = False) -> dict[str, object]:
    global STORAGE_CACHE, STORAGE_CACHE_EXPIRES
    total, used, free = shutil.disk_usage(HOME)
    now = time.monotonic()
    with LOCK:
        if not force and STORAGE_CACHE is not None and now < STORAGE_CACHE_EXPIRES:
            cached = STORAGE_CACHE.copy()
            cached.update({"total": total, "used": used, "free": free})
            return cached

    base: dict[str, object] = {
        "path": str(HOME),
        "total": total,
        "used": used,
        "free": free,
    }
    if not force and os.environ.get("PLAYER_FAST_STORAGE", "1") != "0":
        with LOCK:
            if STORAGE_CACHE is None:
                return {**base, "player": 0, "library": 0, "playlists": 0, "assets": 0}
            cached = STORAGE_CACHE.copy()
            cached.update(base)
            return cached

    payload = {
        **base,
        "player": directory_size(HOME),
        "library": directory_size(LIBRARY_DIR),
        "playlists": directory_size(PLAYLIST_DIR),
        "assets": directory_size(ASSETS_DIR),
    }
    with LOCK:
        STORAGE_CACHE = payload.copy()
        STORAGE_CACHE_EXPIRES = time.monotonic() + STORAGE_CACHE_TTL
    return payload


def status_payload() -> dict[str, object]:
    tracks = tracks_by_id()
    current = tracks.get(str(STATE.get("current_track_id"))) if STATE.get("current_track_id") else None
    return {
        "ok": True,
        "version": __version__,
        "state": STATE,
        "current_track": current,
        "library_count": len(tracks),
        "player": player_status(),
        "display": DISPLAY.snapshot(),
        "storage": storage_payload(),
    }


def has_playlist_cover_view(payload: dict[str, object]) -> bool:
    path = payload.get("path", [])
    playlist = payload.get("current_playlist")
    album = payload.get("current_album")
    cover_item = playlist if isinstance(playlist, dict) else album
    if not isinstance(path, list) or not isinstance(cover_item, dict):
        return False
    return (
        (
            path == ["Home", "Menu", "Playlists"]
            or path == ["Home", "Menu", "Albums"]
            or (len(path) == 4 and path[:3] in (["Home", "Menu", "Playlists"], ["Home", "Menu", "Albums"]))
        )
        and bool(cover_item.get("cover_lcd"))
    )


def selected_label_overflows(payload: dict[str, object]) -> bool:
    items = payload.get("items", [])
    if not isinstance(items, list) or not items:
        return False
    try:
        cursor = int(payload.get("cursor", 0) or 0)
    except (TypeError, ValueError):
        cursor = 0
    if cursor < 0 or cursor >= len(items):
        return False
    item = items[cursor]
    if not isinstance(item, dict):
        return False
    label = str(item.get("label", "") or "")
    body_width = 11 if has_playlist_cover_view(payload) else 19
    return len(label.upper()) > body_width


def breadcrumb_overflows(payload: dict[str, object]) -> bool:
    return len(str(payload.get("breadcrumb", "") or "").upper()) > 20


def display_signature(payload: dict[str, object]) -> str:
    path = payload.get("path", [])
    raw_frame = payload.get("frame")
    frame = raw_frame if path == ["Home"] else None
    if frame is None and (selected_label_overflows(payload) or breadcrumb_overflows(payload)):
        try:
            frame = int(raw_frame or 0) // 2
        except (TypeError, ValueError):
            frame = 0
    state = payload.get("state", {})
    if not isinstance(state, dict):
        state = {}
    items = payload.get("items", [])
    if not isinstance(items, list):
        items = []
    compact_items = []
    for item in items:
        if isinstance(item, dict):
            compact_items.append(
                {
                    "label": item.get("label"),
                    "type": item.get("type"),
                    "slug": item.get("slug"),
                    "track_id": item.get("track_id"),
                }
            )
    playlist = payload.get("current_playlist")
    album = payload.get("current_album")
    cover = ""
    if isinstance(playlist, dict):
        cover = str(playlist.get("cover_lcd") or "")
    elif isinstance(album, dict):
        cover = str(album.get("cover_lcd") or "")
    return json.dumps(
        {
            "path": path,
            "breadcrumb": payload.get("breadcrumb"),
            "cursor": payload.get("cursor"),
            "items": compact_items,
            "message": payload.get("message"),
            "current_track": (payload.get("current_track") or {}).get("id") if isinstance(payload.get("current_track"), dict) else None,
            "cover": cover,
            "paused": state.get("paused"),
            "volume": state.get("volume"),
            "library_count": payload.get("library_count"),
            "frame": frame,
        },
        sort_keys=True,
    )


def update_display(force: bool = False) -> None:
    global DISPLAY_LAST_SIGNATURE
    if not bool(getattr(DISPLAY, "enabled", True)):
        return
    try:
        with DISPLAY_LOCK:
            payload = screen_payload()
            signature = display_signature(payload)
            if not force and signature == DISPLAY_LAST_SIGNATURE:
                return
            DISPLAY.render(payload)
            DISPLAY_LAST_SIGNATURE = signature
    except Exception as exc:
        print(f"Display render failed: {type(exc).__name__}: {exc}")


def reinitialize_display() -> dict[str, object]:
    global DISPLAY_LAST_SIGNATURE
    with DISPLAY_LOCK:
        method = getattr(DISPLAY, "reinitialize", None)
        reinitialized = bool(method()) if callable(method) else False
        DISPLAY_LAST_SIGNATURE = ""
        update_display(force=True)
        payload = status_payload()
        payload["display_reinitialized"] = reinitialized
        return payload


def maybe_reinitialize_display_for_input() -> None:
    global DISPLAY_LAST_INPUT_REINIT, DISPLAY_LAST_SIGNATURE
    if os.environ.get("PLAYER_TFT_REINIT_ON_INPUT", "0") == "0":
        return
    now = time.monotonic()
    minimum_gap = 1.0
    if now - DISPLAY_LAST_INPUT_REINIT < minimum_gap:
        return
    method = getattr(DISPLAY, "reinitialize", None)
    if not callable(method):
        return
    with DISPLAY_LOCK:
        method()
        DISPLAY_LAST_SIGNATURE = ""
        DISPLAY_LAST_INPUT_REINIT = now


def set_queue(track_ids: list[str], name: str) -> None:
    valid = tracks_by_id()
    STATE["queue"] = [track_id for track_id in track_ids if track_id in valid]
    STATE["queue_name"] = name or "Queue"


def play_track(
    track_id: str,
    queue: list[str] | None = None,
    queue_name: str | None = None,
    output: bool = True,
) -> dict[str, object]:
    with LOCK:
        if queue is not None:
            set_queue(queue, queue_name or "Queue")
        elif not STATE.get("queue"):
            set_queue([str(track["id"]) for track in scan_library()], "All Songs")

        path = path_for_track(track_id)
        if PLAYER.available:
            if output:
                PLAYER.play(path)
                PLAYER.volume(int(STATE.get("volume", 70) or 70))
            else:
                PLAYER.stop()
        STATE["current_track_id"] = track_id
        STATE["paused"] = False
        save_state()
        return status_payload()


def stop_playback(output: bool = True) -> dict[str, object]:
    with LOCK:
        if PLAYER.available and output:
            PLAYER.stop()
        STATE["paused"] = False
        save_state()
        return status_payload()


def reset_player_state(output: bool = True) -> dict[str, object]:
    global STATE
    with LOCK:
        if PLAYER.available and output:
            PLAYER.stop()
        STATE = {
            key: value.copy() if isinstance(value, list) else value
            for key, value in DEFAULT_STATE.items()
        }
        STATE["ui_message"] = "RESET"
        save_state()
        return status_payload()


def pause_playback(paused: bool, output: bool = True) -> dict[str, object]:
    with LOCK:
        if PLAYER.available:
            if output:
                PLAYER.pause(paused)
            else:
                PLAYER.stop()
        STATE["paused"] = bool(paused)
        save_state()
        return status_payload()


def set_volume(value: int, output: bool = True) -> dict[str, object]:
    with LOCK:
        value = max(0, min(100, int(value)))
        if PLAYER.available and output:
            PLAYER.volume(value)
        STATE["volume"] = value
        save_state()
        return status_payload()


def step_track(direction: int, output: bool = True) -> dict[str, object]:
    with LOCK:
        queue = [str(item) for item in STATE.get("queue", [])]
        if not queue:
            queue = [str(track["id"]) for track in scan_library()]
            set_queue(queue, "All Songs")
        if not queue:
            return status_payload()
        current = str(STATE.get("current_track_id") or "")
        index = queue.index(current) if current in queue else -1
        next_index = (index + direction) % len(queue)
        return play_track(queue[next_index], output=output)


MAIN_MENU_LABELS = {"Now Playing", "Playlists", "Albums", "Volume", "Settings"}


def perform_transport_action(action: str) -> None:
    if action == "playpause":
        current = STATE.get("current_track_id")
        if current:
            pause_playback(not bool(STATE.get("paused", False)))
        else:
            tracks = scan_library()
            if tracks:
                play_track(str(tracks[0]["id"]), [str(track["id"]) for track in tracks], "All Songs")
        STATE["ui_message"] = "PAUSE" if STATE.get("paused") else "PLAY"
    elif action == "next":
        step_track(1)
        STATE["ui_message"] = "NEXT"
    elif action == "previous":
        step_track(-1)
        STATE["ui_message"] = "PREV"
    elif action == "stop":
        stop_playback()
        STATE["ui_message"] = "STOP"
    elif action == "volume_up":
        set_volume(int(STATE.get("volume", 70)) + 5)
        STATE["ui_message"] = "VOL UP"
    elif action == "volume_down":
        set_volume(int(STATE.get("volume", 70)) - 5)
        STATE["ui_message"] = "VOL DOWN"
    elif action == "status":
        STATE["ui_message"] = f"{len(scan_library())} SONGS"
    elif action == "refresh":
        STATE["ui_message"] = "REFRESH"
    elif action == "about":
        STATE["ui_message"] = f"PLAYER {__version__}"
    else:
        raise ValueError(f"Unknown transport action: {action}")


def playlist_by_slug(slug: str) -> dict[str, object] | None:
    clean_slug = slugify(slug)
    for playlist in list_playlists():
        if str(playlist["slug"]) == clean_slug:
            return playlist
    return None


def playlist_track_ids(slug: str) -> tuple[dict[str, object] | None, list[str]]:
    playlist = playlist_by_slug(slug)
    if not playlist:
        return None, []
    return playlist, [str(track_id) for track_id in playlist.get("track_ids", [])]


def menu_for_path(path: list[str]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if path == ["Home"]:
        return []
    if path == ["Home", "Menu"]:
        items = [
            {"label": "Playlists", "type": "screen", "target": "playlists"},
            {"label": "Albums", "type": "screen", "target": "albums"},
            {"label": "Now Playing", "type": "screen", "target": "now"},
            {"label": "Volume", "type": "screen", "target": "volume"},
            {"label": "Settings", "type": "screen", "target": "settings"},
        ]
    elif path == ["Home", "Menu", "Now Playing"]:
        items = [
            {"label": "Play / Pause", "type": "action", "action": "playpause"},
            {"label": "Next Track", "type": "action", "action": "next"},
            {"label": "Previous Track", "type": "action", "action": "previous"},
            {"label": "Stop", "type": "action", "action": "stop"},
        ]
    elif path == ["Home", "Menu", "Playlists"]:
        playlists = list_playlists()
        items = [
            {
                "label": str(playlist["name"]),
                "type": "playlist",
                "slug": str(playlist["slug"]),
                "cover": str(playlist.get("cover") or ""),
                "cover_lcd": str(playlist.get("cover_lcd") or ""),
                "cover_lcd_path": str(playlist.get("cover_lcd_path") or ""),
                "count": str(playlist.get("count") or len(playlist.get("track_ids", []))),
            }
            for playlist in playlists
        ] or [{"label": "No Playlists", "type": "noop"}]
    elif len(path) == 4 and path[:3] == ["Home", "Menu", "Playlists"]:
        slug = str(path[3])
        playlist, track_ids = playlist_track_ids(slug)
        tracks = tracks_by_id()
        if playlist:
            items = [{"label": "Play All", "type": "playlist_play", "slug": slug}]
            items.extend(
                {
                    "label": str(tracks[track_id]["name"]),
                    "type": "playlist_track",
                    "slug": slug,
                    "track_id": track_id,
                }
                for track_id in track_ids
                if track_id in tracks
            )
        else:
            items = [{"label": "Missing Playlist", "type": "noop"}]
    elif path == ["Home", "Menu", "Albums"]:
        albums = list_albums()
        items = [
            {
                "label": str(album["name"]),
                "type": "album",
                "slug": str(album["slug"]),
                "artist": str(album.get("artist") or ""),
                "cover": str(album.get("cover") or ""),
                "cover_lcd": str(album.get("cover_lcd") or ""),
                "cover_lcd_path": str(album.get("cover_lcd_path") or ""),
                "count": str(album.get("count") or len(album.get("track_ids", []))),
            }
            for album in albums
        ] or [{"label": "No Albums", "type": "noop"}]
    elif len(path) == 4 and path[:3] == ["Home", "Menu", "Albums"]:
        slug = str(path[3])
        album = album_by_slug(slug)
        tracks = tracks_by_id()
        if album:
            track_ids = [str(track_id) for track_id in album.get("track_ids", [])]
            items = [{"label": "Play All", "type": "album_play", "slug": slug}]
            items.extend(
                {
                    "label": str(tracks[track_id]["name"]),
                    "type": "album_track",
                    "slug": slug,
                    "track_id": track_id,
                }
                for track_id in track_ids
                if track_id in tracks
            )
        else:
            items = [{"label": "Missing Album", "type": "noop"}]
    elif path == ["Home", "Menu", "Volume"]:
        items = [
            {"label": "Volume Up", "type": "action", "action": "volume_up"},
            {"label": "Volume Down", "type": "action", "action": "volume_down"},
        ]
    elif path == ["Home", "Menu", "Settings"]:
        items = [
            {"label": "Status", "type": "action", "action": "status"},
            {"label": "Refresh Screen", "type": "action", "action": "refresh"},
            {"label": "About", "type": "action", "action": "about"},
        ]
    if path != ["Home"] and items:
        return [{"label": ".. Back", "type": "back"}] + items
    return items


def current_ui_path() -> list[str]:
    path = STATE.get("ui_path")
    if not isinstance(path, list) or not path:
        return ["Home"]
    clean = [str(item) for item in path if str(item)]
    if clean[:3] == ["Home", "Menu", "Library"]:
        return ["Home", "Menu", "Albums"]
    if len(clean) == 2 and clean[0] == "Home" and clean[1] in MAIN_MENU_LABELS:
        return ["Home", "Menu", clean[1]]
    return clean or ["Home"]


def set_ui_path(path: list[str]) -> None:
    STATE["ui_path"] = path
    STATE["ui_cursor"] = 0


def current_cursor(items: list[dict[str, str]]) -> int:
    if not items:
        return 0
    return int(STATE.get("ui_cursor", 0) or 0) % len(items)


def home_focus() -> int:
    try:
        focus = int(STATE.get("ui_cursor", 0) or 0)
    except (TypeError, ValueError):
        focus = 0
    return max(0, min(max(HOME_FOCUS_ACTIONS), focus))


def set_home_focus(focus: int) -> None:
    focus = max(0, min(max(HOME_FOCUS_ACTIONS), int(focus)))
    STATE["ui_cursor"] = focus
    STATE["ui_message"] = HOME_FOCUS_LABELS.get(focus, "READY")


def nearest_home_focus(source: int, candidates: tuple[int, ...]) -> int:
    source_x = HOME_FOCUS_X.get(source, 0)
    return min(candidates, key=lambda candidate: abs(HOME_FOCUS_X[candidate] - source_x))


def move_home_focus(action: str) -> None:
    focus = home_focus()
    if action == "left":
        if focus in HOME_TOP_FOCUS:
            set_home_focus(max(HOME_TOP_FOCUS[0], focus - 1))
        else:
            set_home_focus(max(HOME_BOTTOM_FOCUS[0], focus - 1))
    elif action == "right":
        if focus in HOME_TOP_FOCUS:
            set_home_focus(min(HOME_TOP_FOCUS[-1], focus + 1))
        else:
            set_home_focus(min(HOME_BOTTOM_FOCUS[-1], focus + 1))
    elif action == "up" and focus in HOME_BOTTOM_FOCUS:
        set_home_focus(nearest_home_focus(focus, HOME_TOP_FOCUS))
    elif action == "down" and focus in HOME_TOP_FOCUS:
        set_home_focus(nearest_home_focus(focus, HOME_BOTTOM_FOCUS))
    else:
        set_home_focus(focus)


def perform_home_focus() -> None:
    focus = home_focus()
    action = HOME_FOCUS_ACTIONS.get(focus, "menu")
    if action == "menu":
        STATE["ui_path"] = ["Home", "Menu"]
        STATE["ui_cursor"] = 1
        STATE["ui_message"] = "MENU"
    else:
        perform_transport_action(action)


def perform_menu_item(item: dict[str, str]) -> None:
    item_type = item.get("type", "noop")
    if item_type == "back":
        path = current_ui_path()
        set_ui_path(path[:-1] if len(path) > 1 else ["Home"])
        STATE["ui_message"] = "BACK"
        return
    if item_type == "screen":
        label = str(item["label"])
        set_ui_path(["Home", "Menu", label])
        STATE["ui_cursor"] = 1
        STATE["ui_message"] = label.upper()
        return
    if item_type == "playlist":
        slug = str(item["slug"])
        playlist = playlist_by_slug(slug)
        set_ui_path(["Home", "Menu", "Playlists", slug])
        STATE["ui_cursor"] = 1
        STATE["ui_message"] = str(playlist.get("name", slug) if playlist else slug).upper()
        return
    if item_type == "playlist_play":
        slug = str(item["slug"])
        playlist, track_ids = playlist_track_ids(slug)
        if track_ids:
            play_track(track_ids[0], track_ids, str(playlist.get("name", slug) if playlist else slug))
            STATE["ui_message"] = "PLAYLIST"
        return
    if item_type == "playlist_track":
        slug = str(item["slug"])
        playlist, track_ids = playlist_track_ids(slug)
        track_id = str(item["track_id"])
        if track_id in track_ids:
            play_track(track_id, track_ids, str(playlist.get("name", slug) if playlist else slug))
            STATE["ui_message"] = "PLAY"
        return
    if item_type == "album":
        slug = str(item["slug"])
        album = album_by_slug(slug)
        set_ui_path(["Home", "Menu", "Albums", slug])
        STATE["ui_cursor"] = 1
        STATE["ui_message"] = str(album.get("name", slug) if album else slug).upper()
        return
    if item_type == "album_play":
        slug = str(item["slug"])
        album = album_by_slug(slug)
        track_ids = [str(track_id) for track_id in album.get("track_ids", [])] if album else []
        if track_ids:
            play_track(track_ids[0], track_ids, str(album.get("name", slug) if album else slug))
            STATE["ui_message"] = "ALBUM"
        return
    if item_type == "album_track":
        slug = str(item["slug"])
        album = album_by_slug(slug)
        track_ids = [str(track_id) for track_id in album.get("track_ids", [])] if album else []
        track_id = str(item["track_id"])
        if track_id in track_ids:
            play_track(track_id, track_ids, str(album.get("name", slug) if album else slug))
            STATE["ui_message"] = "PLAY"
        return
    action = item.get("action")
    if action:
        perform_transport_action(str(action))


def breadcrumb_for_path(path: list[str]) -> str:
    if len(path) == 4 and path[:3] == ["Home", "Menu", "Playlists"]:
        playlist = playlist_by_slug(str(path[3]))
        if playlist:
            return " / ".join(path[:3] + [str(playlist["name"])])
    if len(path) == 4 and path[:3] == ["Home", "Menu", "Albums"]:
        album = album_by_slug(str(path[3]))
        if album:
            return " / ".join(path[:3] + [str(album["name"])])
    return " / ".join(path)


def playlist_for_current_queue() -> dict[str, object] | None:
    queue = [str(item) for item in STATE.get("queue", []) if str(item)]
    if not queue:
        return None
    queue_name_slug = slugify(str(STATE.get("queue_name") or ""))
    matched_by_tracks = None
    for playlist in list_playlists():
        playlist_ids = [str(item) for item in playlist.get("track_ids", []) if str(item)]
        if not playlist_ids:
            continue
        if str(playlist.get("slug") or "") == queue_name_slug or slugify(str(playlist.get("name") or "")) == queue_name_slug:
            return playlist
        if playlist_ids == queue:
            matched_by_tracks = playlist
    return matched_by_tracks


def screen_payload() -> dict[str, object]:
    path = current_ui_path()
    items = menu_for_path(path)
    cursor = home_focus() if path == ["Home"] else current_cursor(items)
    selected = items[cursor] if items else None
    tracks = tracks_by_id()
    current = tracks.get(str(STATE.get("current_track_id"))) if STATE.get("current_track_id") else None
    current_playlist = None
    current_queue_playlist = playlist_for_current_queue()
    current_album = None
    if len(path) == 4 and path[:3] == ["Home", "Menu", "Playlists"]:
        current_playlist = playlist_by_slug(str(path[3]))
    elif path == ["Home", "Menu", "Playlists"] and isinstance(selected, dict) and selected.get("type") == "playlist":
        current_playlist = playlist_by_slug(str(selected.get("slug", "")))
    if len(path) == 4 and path[:3] == ["Home", "Menu", "Albums"]:
        current_album = album_by_slug(str(path[3]))
    elif path == ["Home", "Menu", "Albums"] and isinstance(selected, dict) and selected.get("type") == "album":
        current_album = album_by_slug(str(selected.get("slug", "")))
    return {
        "ok": True,
        "version": __version__,
        "screen": STATE.get("ui_screen", "home"),
        "path": path,
        "breadcrumb": breadcrumb_for_path(path),
        "items": items,
        "cursor": cursor,
        "selected": selected,
        "message": STATE.get("ui_message", ""),
        "frame": int(time.time() * 4),
        "state": STATE,
        "current_track": current,
        "current_playlist": current_playlist,
        "current_queue_playlist": current_queue_playlist,
        "current_album": current_album,
        "library_count": len(tracks),
        "theme": theme_for_track(current, bool(current and not STATE.get("paused", False))),
    }


def handle_input_action(action: str) -> dict[str, object]:
    action = action.strip().lower()
    path = current_ui_path()
    items = menu_for_path(path)
    selected = items[current_cursor(items)] if items else None
    if action == "back":
        if len(path) > 1:
            set_ui_path(path[:-1])
            STATE["ui_message"] = "BACK"
        else:
            STATE["ui_message"] = "HOME"
    elif action == "home":
        set_ui_path(["Home"])
        STATE["ui_message"] = "HOME"
    elif path == ["Home"] and action == "menu":
        STATE["ui_path"] = ["Home", "Menu"]
        STATE["ui_cursor"] = 1
        STATE["ui_message"] = "MENU"
    elif path == ["Home"] and action in {"up", "down", "left", "right"}:
        move_home_focus(action)
    elif path == ["Home"] and action == "select":
        perform_home_focus()
    elif action in {"playpause", "next", "previous", "stop", "volume_up", "volume_down"}:
        perform_transport_action(action)
    elif action == "menu":
        set_ui_path(["Home", "Menu"])
        STATE["ui_cursor"] = 1
        STATE["ui_message"] = "MENU"
    elif action == "up" and items:
        STATE["ui_cursor"] = (current_cursor(items) - 1) % len(items)
        STATE["ui_message"] = str(items[current_cursor(items)]["label"]).upper()
    elif action == "down" and items:
        STATE["ui_cursor"] = (current_cursor(items) + 1) % len(items)
        STATE["ui_message"] = str(items[current_cursor(items)]["label"]).upper()
    elif action == "left" and path and path[-1] == "Volume":
        set_volume(int(STATE.get("volume", 70)) - 5)
        STATE["ui_message"] = "VOL DOWN"
    elif action == "right" and path and path[-1] == "Volume":
        set_volume(int(STATE.get("volume", 70)) + 5)
        STATE["ui_message"] = "VOL UP"
    elif action == "left":
        if len(path) > 1:
            set_ui_path(path[:-1])
            STATE["ui_message"] = "BACK"
        else:
            STATE["ui_message"] = "HOME"
    elif action == "right" and selected and selected.get("type") in {"screen", "playlist", "album"}:
        perform_menu_item(selected)
    elif action == "right":
        STATE["ui_message"] = str(selected.get("label", "READY")).upper() if selected else "READY"
    elif action == "select":
        if items:
            perform_menu_item(items[current_cursor(items)])
    else:
        raise ValueError(f"Unknown input action: {action}")
    save_state()
    return screen_payload()


def display_loop() -> None:
    while True:
        time.sleep(0.25)
        if not bool(getattr(DISPLAY, "enabled", True)):
            continue
        wants_forced_render = getattr(DISPLAY, "wants_forced_render", None)
        force = bool(wants_forced_render()) if callable(wants_forced_render) else False
        update_display(force=force)


def parse_m3u_playlist(path: Path) -> dict[str, object]:
    tracks = scan_library()
    by_rel = {str(track["relative_path"]): track for track in tracks}
    by_id = {str(track["id"]): track for track in tracks}
    name = path.stem.replace("_", " ")
    cover = ""
    cover_source = ""
    cover_pixel = ""
    cover_lcd = ""
    cover_lcd_path = ""
    track_ids: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#PLAYLIST:"):
            name = line.split(":", 1)[1].strip() or name
            continue
        if line.startswith("#COVER-LCD:"):
            cover_lcd = line.split(":", 1)[1].strip()
            continue
        if line.startswith("#COVER:"):
            cover_source = line.split(":", 1)[1].strip()
            continue
        if line.startswith("#"):
            continue
        rel = line.replace("\\", "/")
        if rel.startswith("../library/"):
            rel = rel.removeprefix("../library/")
        elif rel.startswith("library/"):
            rel = rel.removeprefix("library/")
        track = by_rel.get(rel)
        if track:
            track_ids.append(str(track["id"]))
    mosaic = playlist_mosaic_metadata(path.stem, track_ids, by_id)
    if mosaic.get("cover"):
        cover_source = mosaic.get("cover", "")
        cover_pixel = mosaic.get("cover_pixel", "")
        cover_lcd = mosaic.get("cover_lcd", "")
        cover_lcd_path = mosaic.get("cover_lcd_path", "")
    if cover_source:
        generated = cover_metadata_from_value(cover_source, path.stem)
        cover_pixel = cover_pixel or generated.get("cover_pixel", "")
        if not cover_lcd:
            cover_lcd = generated.get("cover_lcd", "")
            cover_lcd_path = generated.get("cover_lcd_path", "")
    if cover_lcd and not cover_lcd_path:
        cover_lcd_path = str(asset_path(cover_lcd) or "")
    if not cover_source:
        cover_source = str(next((by_id[track_id].get("album_cover") for track_id in track_ids if by_id.get(track_id) and by_id[track_id].get("album_cover")), ""))
    if not cover_pixel:
        cover_pixel = str(next((by_id[track_id].get("album_cover_pixel") for track_id in track_ids if by_id.get(track_id) and by_id[track_id].get("album_cover_pixel")), "")) or cover_source
    if not cover_lcd:
        cover_lcd = str(next((by_id[track_id].get("album_cover_lcd") for track_id in track_ids if by_id.get(track_id) and by_id[track_id].get("album_cover_lcd")), ""))
    if not cover_lcd_path:
        cover_lcd_path = str(next((by_id[track_id].get("album_cover_lcd_path") for track_id in track_ids if by_id.get(track_id) and by_id[track_id].get("album_cover_lcd_path")), ""))
    return {
        "name": name,
        "slug": path.stem,
        "filename": path.name,
        "format": "m3u8",
        "cover_source": cover_source,
        "cover": cover_pixel or cover_source,
        "cover_lcd": cover_lcd,
        "cover_lcd_path": cover_lcd_path,
        "count": len(track_ids),
        "track_ids": track_ids,
    }


def write_m3u_playlist(name: str, slug: str, track_ids: list[str], cover: str = "", cover_lcd: str = "") -> dict[str, object]:
    valid = tracks_by_id()
    clean_ids = [str(track_id) for track_id in track_ids if str(track_id) in valid]
    title = name.strip() or slug.replace("_", " ")
    lines = ["#EXTM3U", f"#PLAYLIST: {title}"]
    for track_id in clean_ids:
        track = valid[track_id]
        lines.append(f"#EXTINF:-1,{track['name']}")
        lines.append(f"../library/{track['relative_path']}")
    path = playlist_path(slug)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    old_json = PLAYLIST_DIR / f"{slug}.json"
    if old_json.exists():
        old_json.unlink()
    playlist = parse_m3u_playlist(path)
    return {
        "name": title,
        "slug": slug,
        "filename": path.name,
        "format": "m3u8",
        "cover_source": str(playlist.get("cover_source", "")),
        "cover": str(playlist.get("cover", "")),
        "cover_lcd": str(playlist.get("cover_lcd", "")),
        "cover_lcd_path": str(playlist.get("cover_lcd_path", "")),
        "count": len(clean_ids),
        "track_ids": clean_ids,
    }


def migrate_json_playlists() -> None:
    ensure_dirs()
    for path in sorted(PLAYLIST_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        track_ids = data.get("track_ids", [])
        if not isinstance(track_ids, list):
            track_ids = []
        name = str(data.get("name") or path.stem.replace("_", " "))
        write_m3u_playlist(name, path.stem, [str(item) for item in track_ids])


def list_playlists() -> list[dict[str, object]]:
    ensure_dirs()
    migrate_json_playlists()
    playlists: list[dict[str, object]] = []
    for path in sorted(PLAYLIST_DIR.glob("*.m3u8")):
        try:
            playlists.append(parse_m3u_playlist(path))
        except OSError:
            continue
    return playlists


def save_playlist(name: str, track_ids: list[str]) -> dict[str, object]:
    slug = slugify(name)
    playlist = write_m3u_playlist(name, slug, track_ids)
    return {"ok": True, "playlist": playlist}


def delete_playlists(slugs: list[str]) -> dict[str, object]:
    with LOCK:
        deleted: list[str] = []
        for raw_slug in slugs:
            slug = slugify(str(raw_slug))
            removed = False
            for suffix in (".m3u8", ".json"):
                path = PLAYLIST_DIR / f"{slug}{suffix}"
                if path.exists():
                    try:
                        path.unlink()
                        removed = True
                    except OSError:
                        pass
            if removed:
                deleted.append(slug)

        path = current_ui_path()
        if len(path) == 4 and path[:3] == ["Home", "Menu", "Playlists"] and str(path[3]) in deleted:
            set_ui_path(["Home", "Menu", "Playlists"])
            STATE["ui_message"] = "PLAYLISTS"

        save_state()
        payload = status_payload()
        payload["deleted_playlists"] = deleted
        payload["playlists"] = list_playlists()
        return payload


def cleanup_empty_library_dirs(start_dirs: set[Path]) -> None:
    for start in sorted(start_dirs, key=lambda item: len(item.parts), reverse=True):
        current = start
        while current != LIBRARY_DIR and current.is_relative_to(LIBRARY_DIR):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def delete_album_art_if_folder_is_audio_empty(folders: set[Path]) -> None:
    for folder in folders:
        if not folder.exists() or not folder.is_dir() or not folder.is_relative_to(LIBRARY_DIR):
            continue
        try:
            has_audio = any(item.is_file() and item.suffix.lower() in AUDIO_EXTENSIONS for item in folder.rglob("*"))
        except OSError:
            continue
        if has_audio:
            continue
        for item in sorted(folder.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS:
                try:
                    item.unlink()
                except OSError:
                    pass


def delete_albums(album_keys: list[str], track_ids: list[str] | None = None) -> dict[str, object]:
    albums_by_slug = {slugify(str(album["name"]), "album"): album for album in list_albums()}
    clean_track_ids: list[str] = []
    deleted_albums: list[str] = []
    parent_dirs: set[Path] = set()

    if track_ids:
        clean_track_ids = [str(track_id) for track_id in track_ids if str(track_id)]
        deleted_albums = [str(name) for name in album_keys if str(name)]
    else:
        for raw_key in album_keys:
            album = albums_by_slug.get(slugify(str(raw_key), "album"))
            if not album:
                continue
            deleted_albums.append(str(album["name"]))
            for track_id in [str(item) for item in album.get("track_ids", [])]:
                if track_id not in clean_track_ids:
                    clean_track_ids.append(track_id)

    for track_id in clean_track_ids:
        try:
            parent_dirs.add(path_for_track(track_id).parent)
        except KeyError:
            pass

    payload = delete_tracks(clean_track_ids) if clean_track_ids else status_payload()
    delete_album_art_if_folder_is_audio_empty(parent_dirs)
    cleanup_empty_library_dirs(parent_dirs)
    payload["deleted_albums"] = deleted_albums
    payload["tracks"] = scan_library()
    payload["albums"] = list_albums()
    payload["storage"] = storage_payload()
    return payload


def delete_tracks(track_ids: list[str]) -> dict[str, object]:
    with LOCK:
        valid = tracks_by_id()
        clean_ids = [str(track_id) for track_id in track_ids if str(track_id) in valid]
        deleted: list[str] = []
        for track_id in clean_ids:
            try:
                path_for_track(track_id).unlink()
                deleted.append(track_id)
            except OSError:
                continue

        if str(STATE.get("current_track_id") or "") in deleted:
            if PLAYER.available:
                PLAYER.stop()
            STATE["current_track_id"] = None
            STATE["paused"] = False

        queue = [str(item) for item in STATE.get("queue", [])]
        STATE["queue"] = [track_id for track_id in queue if track_id not in deleted]
        invalidate_library_cache()
        invalidate_storage_cache()

        migrate_json_playlists()
        for playlist_path_item in PLAYLIST_DIR.glob("*.m3u8"):
            try:
                playlist = parse_m3u_playlist(playlist_path_item)
            except OSError:
                continue
            original = [str(item) for item in playlist.get("track_ids", []) if str(item)]
            updated = [track_id for track_id in original if track_id not in deleted]
            if updated != original:
                write_m3u_playlist(str(playlist.get("name") or playlist_path_item.stem), playlist_path_item.stem, updated)

        save_state()
        payload = status_payload()
        payload["deleted"] = deleted
        payload["tracks"] = scan_library()
        payload["playlists"] = list_playlists()
        return payload


def safe_path_component(value: str, fallback: str) -> str:
    clean = re.sub(r'[<>:"|?*\x00-\x1f]+', "", value).strip().strip(".")
    clean = clean.replace("\\", "").replace("/", "")
    return clean[:90] or fallback


def upload_path(filename: str, target_album: str = "") -> Path:
    raw_parts = [part for part in filename.replace("\\", "/").split("/") if part and part not in {".", ".."}]
    if not raw_parts:
        raise ValueError("Missing upload filename")

    final_name = raw_parts[-1]
    suffix = Path(final_name).suffix.lower()
    if suffix not in AUDIO_EXTENSIONS and suffix not in IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported file: {suffix or 'no extension'}")

    dirs = [safe_path_component(part, "Folder") for part in raw_parts[:-1]]
    if target_album:
        album = safe_path_component(target_album, "Album")
        if dirs and dirs[0].casefold() == album.casefold():
            dirs = dirs[1:]
        dirs = [album] + dirs

    stem = safe_path_component(Path(final_name).stem, "track")
    candidate_dir = (LIBRARY_DIR / Path(*dirs)).resolve() if dirs else LIBRARY_DIR
    if not candidate_dir.is_relative_to(LIBRARY_DIR):
        raise ValueError("Upload path escaped library")
    candidate_dir.mkdir(parents=True, exist_ok=True)
    candidate = candidate_dir / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = candidate_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def manage_page() -> str:
    return Path(__file__).with_name("manage.html").read_text(encoding="utf-8")

class Handler(BaseHTTPRequestHandler):
    server_version = "Player/0.1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_html(html_page())
            elif parsed.path == "/manage":
                self.send_html(manage_page())
            elif parsed.path == "/api/screen":
                self.send_json(screen_payload())
            elif parsed.path == "/api/status":
                self.send_json(status_payload())
            elif parsed.path == "/_sym/health":
                self.send_json({"ok": True, "status": "healthy", "version": __version__})
            elif parsed.path == "/mini-sym":
                self.send_html(mini_sym_page())
            elif parsed.path == "/api/reset":
                self.send_json(reset_player_state())
            elif parsed.path == "/api/display/reinit":
                self.send_json(reinitialize_display())
            elif parsed.path == "/api/library":
                self.send_json({"ok": True, "tracks": scan_library()})
            elif parsed.path == "/api/playlists":
                self.send_json({"ok": True, "playlists": list_playlists()})
            elif parsed.path.startswith("/media/"):
                self.serve_media(unquote(parsed.path.removeprefix("/media/")))
            elif parsed.path.startswith("/library-art/"):
                self.serve_library_art(unquote(parsed.path.removeprefix("/library-art/")))
            elif parsed.path.startswith("/assets/"):
                self.serve_asset(unquote(parsed.path.removeprefix("/assets/")))
            elif parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
            else:
                self.send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/upload":
                self.handle_upload()
            elif parsed.path == "/api/display/reinit":
                self.send_json(reinitialize_display())
            elif parsed.path == "/api/play":
                body = self.read_json()
                track_id = str(body.get("track_id", ""))
                queue = body.get("queue")
                queue_name = str(body.get("queue_name", "Queue"))
                output = bool(body.get("output", True))
                if queue is not None and not isinstance(queue, list):
                    queue = None
                self.send_json(play_track(track_id, [str(item) for item in queue] if queue else None, queue_name, output=output))
            elif parsed.path == "/api/input":
                body = self.read_json()
                maybe_reinitialize_display_for_input()
                self.send_json(handle_input_action(str(body.get("action", ""))))
            elif parsed.path == "/api/pause":
                body = self.read_json()
                self.send_json(pause_playback(bool(body.get("paused", True)), output=bool(body.get("output", True))))
            elif parsed.path == "/api/stop":
                body = self.read_json()
                self.send_json(stop_playback(output=bool(body.get("output", True))))
            elif parsed.path == "/api/reset":
                body = self.read_json()
                self.send_json(reset_player_state(output=bool(body.get("output", True))))
            elif parsed.path == "/api/next":
                body = self.read_json()
                self.send_json(step_track(1, output=bool(body.get("output", True))))
            elif parsed.path == "/api/previous":
                body = self.read_json()
                self.send_json(step_track(-1, output=bool(body.get("output", True))))
            elif parsed.path == "/api/volume":
                body = self.read_json()
                self.send_json(set_volume(int(body.get("volume", STATE.get("volume", 70))), output=bool(body.get("output", True))))
            elif parsed.path == "/api/playlists/save":
                body = self.read_json()
                name = str(body.get("name", "Playlist"))
                track_ids = body.get("track_ids", [])
                if not isinstance(track_ids, list):
                    track_ids = []
                self.send_json(save_playlist(name, [str(item) for item in track_ids]))
            elif parsed.path == "/api/playlists/delete":
                body = self.read_json()
                slugs = body.get("slugs", [])
                if isinstance(slugs, str):
                    slugs = [slugs]
                if not isinstance(slugs, list):
                    slugs = []
                self.send_json(delete_playlists([str(item) for item in slugs]))
            elif parsed.path == "/api/albums/delete":
                body = self.read_json()
                albums = body.get("albums", [])
                track_ids = body.get("track_ids", [])
                if isinstance(albums, str):
                    albums = [albums]
                if not isinstance(albums, list):
                    albums = []
                if not isinstance(track_ids, list):
                    track_ids = []
                self.send_json(delete_albums([str(item) for item in albums], [str(item) for item in track_ids]))
            elif parsed.path == "/api/delete":
                body = self.read_json()
                track_ids = body.get("track_ids", [])
                if not isinstance(track_ids, list):
                    track_ids = []
                self.send_json(delete_tracks([str(item) for item in track_ids]))
            elif parsed.path == "/api/playlists/play":
                body = self.read_json()
                slug = slugify(str(body.get("slug", "")))
                self.send_json(self.play_playlist(slug))
            else:
                self.send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_upload(self) -> None:
        ensure_dirs()
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_json({"ok": False, "error": "Expected multipart upload"}, HTTPStatus.BAD_REQUEST)
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw_body
        )
        if not message.is_multipart():
            self.send_json({"ok": False, "error": "Upload body was not multipart"}, HTTPStatus.BAD_REQUEST)
            return

        fields: dict[str, list[str]] = {}
        file_parts = []
        for item in message.iter_parts():
            field_name = item.get_param("name", header="content-disposition")
            if field_name != "files":
                payload = item.get_payload(decode=True)
                if field_name and payload is not None:
                    fields.setdefault(field_name, []).append(payload.decode(item.get_content_charset() or "utf-8", errors="replace"))
                continue
            filename = item.get_filename()
            if not filename:
                continue
            file_parts.append((filename, item))

        saved = []
        saved_assets = []
        target_album = (fields.get("target_album") or [""])[0].strip()
        relative_paths = fields.get("relative_paths") or []
        for index, (filename, item) in enumerate(file_parts):
            payload = item.get_payload(decode=True) or b""
            upload_name = relative_paths[index].strip() if index < len(relative_paths) and relative_paths[index].strip() else filename
            path = upload_path(upload_name, target_album=target_album)
            with path.open("wb") as output:
                output.write(payload)
            rel = path.relative_to(LIBRARY_DIR).as_posix()
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                track_id = track_id_for(path)
                saved.append({"filename": path.name, "relative_path": rel, "id": track_id})
            else:
                saved_assets.append({"filename": path.name, "relative_path": rel, "url": media_url(rel)})
        invalidate_library_cache()
        invalidate_storage_cache()
        tracks = scan_library(force=True)
        update_display()
        self.send_json({"ok": True, "saved": saved, "saved_assets": saved_assets, "tracks": tracks, "playlists": list_playlists()})

    def play_playlist(self, slug: str) -> dict[str, object]:
        migrate_json_playlists()
        path = playlist_path(slug)
        if not path.exists():
            raise KeyError(f"Playlist not found: {slug}")
        playlist = parse_m3u_playlist(path)
        track_ids = [str(item) for item in playlist.get("track_ids", [])]
        if not track_ids:
            return status_payload()
        return play_track(track_ids[0], track_ids, str(playlist.get("name", slug)))

    def serve_media(self, track_id: str) -> None:
        path = path_for_track(track_id)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        start = 0
        end = size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range", "")

        if range_header.startswith("bytes="):
            spec = range_header.removeprefix("bytes=").split(",", 1)[0].strip()
            try:
                if spec.startswith("-"):
                    suffix = int(spec[1:])
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, size - suffix)
                else:
                    left, _, right = spec.partition("-")
                    start = int(left)
                    end = int(right) if right else size - 1
                if start < 0 or end < start or start >= size:
                    raise ValueError
                end = min(end, size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition", f'inline; filename="{html.escape(path.name)}"')
        self.end_headers()
        with path.open("rb") as file:
            file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def serve_library_art(self, relative_path: str) -> None:
        path = (LIBRARY_DIR / relative_path).resolve()
        if not path.is_file() or not path.is_relative_to(LIBRARY_DIR) or path.suffix.lower() not in IMAGE_EXTENSIONS:
            self.send_json({"ok": False, "error": "Artwork not found"}, HTTPStatus.NOT_FOUND)
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                self.wfile.write(chunk)

    def serve_asset(self, relative_path: str) -> None:
        path = (ASSETS_DIR / relative_path).resolve()
        if not path.is_file() or not path.is_relative_to(ASSETS_DIR):
            self.send_json({"ok": False, "error": "Asset not found"}, HTTPStatus.NOT_FOUND)
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                self.wfile.write(chunk)


def html_page() -> str:
    from .control_page import HTML

    return HTML

    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Player</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #070809;
      --case: #151719;
      --case-edge: #333a3f;
      --screen: #020304;
      --pixel: #dff6ff;
      --dim: #668c98;
      font-family: ui-monospace, "Cascadia Mono", "Consolas", monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--pixel);
      display: grid;
      place-items: center;
      overflow: hidden;
    }
    main {
      width: min(96vw, 920px);
      display: grid;
      gap: 18px;
      justify-items: center;
    }
    .case {
      background: var(--case);
      border: 1px solid var(--case-edge);
      border-radius: 8px;
      padding: 24px;
      box-shadow: 0 18px 70px rgba(0, 0, 0, .5);
    }
    canvas {
      width: min(90vw, 768px);
      aspect-ratio: 2 / 1;
      display: block;
      background: var(--screen);
      border: 1px solid #3b464b;
      image-rendering: pixelated;
    }
    .hint {
      color: var(--dim);
      font-size: 13px;
      line-height: 1.4;
      text-align: center;
      user-select: none;
    }
    @media (max-width: 760px) {
      .case { padding: 12px; }
      canvas { width: calc(100vw - 36px); }
    }
  </style>
</head>
<body>
  <main>
    <div class="case">
      <canvas id="lcd" width="128" height="64" aria-label="Player LCD screen"></canvas>
    </div>
    <div class="hint">Up / Down moves. Enter selects. M opens file manager.</div>
  </main>

<script>
const canvas = document.getElementById("lcd");
const ctx = canvas.getContext("2d");
ctx.imageSmoothingEnabled = false;
let screen = null;
const coverCache = new Map();
let requestId = 0;
let inputBusy = false;
let nextPollAt = 0;
let lastRenderSignature = "";

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(data.error || response.statusText);
  return data;
}

function clear() {
  ctx.fillStyle = "#020304";
  ctx.fillRect(0, 0, 128, 64);
  ctx.strokeStyle = "#dff6ff";
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, 127, 63);
}

function label(value, x, y) {
  ctx.fillStyle = "#dff6ff";
  ctx.font = "8px monospace";
  ctx.fillText(String(value), x, y);
}

function clip(value, length) {
  value = String(value || "");
  return value.length > length ? value.slice(0, Math.max(0, length - 1)) + "~" : value;
}

function marquee(value, length, frame) {
  value = String(value || "").toUpperCase();
  if (value.length <= length) return value;
  const loop = value + "   ";
  const offset = Math.floor(frame / 2) % loop.length;
  return (loop + loop).slice(offset, offset + length);
}

function rowText(value, selected, totalLength, frame) {
  const marker = selected ? ">" : " ";
  const bodyLength = Math.max(0, totalLength - 1);
  const body = selected
    ? marquee(value, bodyLength, frame)
    : clip(String(value || "").toUpperCase(), bodyLength);
  return marker + body;
}

function assetUrl(path) {
  if (!path) return "";
  return String(path).replace(/^\.\.\/assets\//, "/assets/");
}

function coverImage(path) {
  const url = assetUrl(path);
  if (!url) return null;
  if (coverCache.has(url)) return coverCache.get(url);
  const image = new Image();
  image.onload = render;
  image.src = url;
  coverCache.set(url, image);
  return image;
}

function screenSignature(data) {
  if (!data) return "";
  const items = Array.isArray(data.items) ? data.items.map(item => ({
    label: item.label,
    type: item.type,
    slug: item.slug,
    track_id: item.track_id
  })) : [];
  const state = data.state || {};
  const coverItem = data.current_playlist || data.current_album || {};
  const current = data.current_track || {};
  return JSON.stringify({
    path: data.path || [],
    breadcrumb: data.breadcrumb || "",
    cursor: data.cursor || 0,
    items,
    message: data.message || "",
    current_track_id: current.id || "",
    cover: coverItem.cover_lcd || "",
    paused: !!state.paused,
    volume: Number(state.volume || 0),
    library_count: Number(data.library_count || 0)
  });
}

function hasPlaylistCoverView(data) {
  const path = data && data.path ? data.path : [];
  const coverItem = data && (data.current_playlist || data.current_album) ? (data.current_playlist || data.current_album) : null;
  return (
    (path.length === 3 || path.length === 4) &&
    path[0] === "Home" &&
    path[1] === "Menu" &&
    (path[2] === "Playlists" || path[2] === "Albums") &&
    coverItem &&
    coverItem.cover_lcd
  );
}

function selectedLabelOverflows(data) {
  if (!data || !Array.isArray(data.items) || data.items.length === 0) return false;
  const cursor = Number(data.cursor || 0);
  const item = data.items[cursor];
  if (!item) return false;
  const bodyLength = hasPlaylistCoverView(data) ? 11 : 19;
  return String(item.label || "").toUpperCase().length > bodyLength;
}

function breadcrumbOverflows(data) {
  return String((data && data.breadcrumb) || "").toUpperCase().length > 20;
}

function shouldAnimate(data) {
  const path = data && data.path ? data.path : [];
  return path.length === 1 || selectedLabelOverflows(data) || breadcrumbOverflows(data);
}

function commitScreen(data, force = false) {
  screen = data;
  const signature = screenSignature(data);
  if (force || shouldAnimate(data) || signature !== lastRenderSignature) {
    render();
    lastRenderSignature = signature;
  }
}

function renderLoading(text) {
  clear();
  label("PLAYER", 3, 10);
  ctx.beginPath();
  ctx.moveTo(0, 13.5);
  ctx.lineTo(128, 13.5);
  ctx.stroke();
  label(text, 40, 35);
}

function render() {
  if (!screen) {
    renderLoading("LOADING");
    return;
  }
  clear();
  const state = screen.state || {};
  const current = screen.current_track;
  const items = screen.items || [];
  const cursor = Number(screen.cursor || 0);
  const path = screen.path || ["Home"];
  const title = current ? current.name : "NO TRACK";
  const volume = Number(state.volume || 0);
  const mode = state.paused ? "PAUSE" : "PLAY";
  const frame = Math.floor(Date.now() / 150);
  const coverItem = screen.current_playlist || screen.current_album || null;
  const hasPlaylistCover = hasPlaylistCoverView(screen);

  if (path.length === 1) {
    label(frame % 8 < 6 ? "PLAYER" : "PL>YER", 3, 10);
    label(mode, 92, 10);
    ctx.beginPath();
    ctx.moveTo(0, 13.5);
    ctx.lineTo(128, 13.5);
    ctx.stroke();

    ctx.strokeStyle = "#dff6ff";
    ctx.strokeRect(5.5, 14.5, 117, 24);
    ctx.strokeRect(13.5, 19.5, 101, 14);
    ctx.beginPath();
    ctx.arc(29, 26, 7, 0, Math.PI * 2);
    ctx.arc(98, 26, 7, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = "#dff6ff";
    ctx.beginPath();
    ctx.arc(29, 26, 2, 0, Math.PI * 2);
    ctx.arc(98, 26, 2, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(38, 27.5);
    ctx.lineTo(89, 27.5);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(58, 21);
    ctx.lineTo(58, 33);
    ctx.lineTo(69, 27);
    ctx.closePath();
    if (state.paused) ctx.stroke(); else ctx.fill();

    for (let i = 0; i < 10; i++) {
      const raw = (frame * (i + 3) + i * 5) % 19;
      const height = state.paused ? 2 : 2 + Math.floor(raw / 3);
      const x = 8 + i * 5;
      ctx.fillRect(x, 62 - height, 3, height);
    }

    const source = ("                   " + title.toUpperCase() + "   ");
    const scroll = Math.floor(frame / 2) % Math.max(1, title.length + 19);
    label(source.slice(scroll, scroll + 19).padEnd(19, " "), 3, 48);
    label(`${String(screen.library_count || 0).padStart(2, "0")} SONGS`, 78, 60);
  } else {
    label(marquee(String(screen.breadcrumb || "Home"), 20, frame), 3, 10);
    ctx.beginPath();
    ctx.moveTo(0, 13.5);
    ctx.lineTo(128, 13.5);
    ctx.stroke();

    const isVolume = path[path.length - 1] === "Volume";
    if (hasPlaylistCover) {
      ctx.strokeStyle = "#dff6ff";
      ctx.strokeRect(2.5, 16.5, 45, 45);
      const art = coverImage(coverItem.cover_lcd);
      if (art && art.complete) {
        ctx.drawImage(art, 3, 17, 44, 44);
      } else {
        label("NO ART", 8, 39);
      }
      const maxRows = 4;
      const start = Math.max(0, Math.min(cursor - 1, Math.max(0, items.length - maxRows)));
      for (let row = 0; row < maxRows; row++) {
        const itemIndex = start + row;
        const item = items[itemIndex];
        if (!item) continue;
        const y = 25 + row * 11;
        const selected = itemIndex === cursor;
        if (selected) {
          ctx.fillStyle = "#dff6ff";
          ctx.fillRect(50, y - 8, 76, 10);
          ctx.fillStyle = "#020304";
        } else {
          ctx.fillStyle = "#dff6ff";
        }
        ctx.font = "8px monospace";
        ctx.fillText(rowText(item.label, selected, 12, frame), 52, y);
      }
      return;
    }

    const maxRows = isVolume ? 2 : 4;
    const start = Math.max(0, Math.min(cursor - 1, Math.max(0, items.length - maxRows)));
    for (let row = 0; row < maxRows; row++) {
      const itemIndex = start + row;
      const item = items[itemIndex];
      if (!item) continue;
      const y = 25 + row * 11;
      const selected = itemIndex === cursor;
      if (selected) {
        ctx.fillStyle = "#dff6ff";
        ctx.fillRect(1, y - 8, 126, 10);
        ctx.fillStyle = "#020304";
      } else {
        ctx.fillStyle = "#dff6ff";
      }
      ctx.font = "8px monospace";
      ctx.fillText(rowText(item.label, selected, 20, frame), 3, y);
    }
    if (isVolume) {
      label(`VOL ${String(volume).padStart(3, "0")}`, 3, 53);
      ctx.strokeRect(58.5, 47.5, 62, 6);
      ctx.fillStyle = "#dff6ff";
      ctx.fillRect(60, 49, Math.max(0, Math.min(58, Math.round(volume * 0.58))), 3);
    }
  }
}

async function refresh(force = false) {
  if (inputBusy && !force) return;
  const id = ++requestId;
  const data = await api("/api/screen");
  if (id !== requestId) return;
  commitScreen(data);
}

async function input(action) {
  inputBusy = true;
  const id = ++requestId;
  try {
    const data = await api("/api/input", {
      method: "POST",
      body: JSON.stringify({ action })
    });
    if (id === requestId) commitScreen(data, true);
    nextPollAt = Date.now() + 1200;
  } finally {
    inputBusy = false;
  }
}

window.addEventListener("keydown", event => {
  if (event.repeat) return;
  if (event.key === "ArrowUp") { event.preventDefault(); input("up"); }
  else if (event.key === "ArrowDown") { event.preventDefault(); input("down"); }
  else if (event.key === "ArrowLeft") { event.preventDefault(); input("left"); }
  else if (event.key === "ArrowRight") { event.preventDefault(); input("right"); }
  else if (event.key === "Enter") { event.preventDefault(); input("select"); }
  else if (event.key.toLowerCase() === "m") { event.preventDefault(); window.location.href = "/manage"; }
});

renderLoading("LOADING");
refresh().catch(error => renderLoading(error.message.slice(0, 12).toUpperCase()));
setInterval(() => {
  if (screen && shouldAnimate(screen)) render();
  if (Date.now() >= nextPollAt && !inputBusy) {
    nextPollAt = Date.now() + 2000;
    refresh().catch(() => {});
  }
}, 150);
</script>
</body>
</html>"""


MINI_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Player</title>
<style>
:root{color-scheme:dark;}
*{box-sizing:border-box;margin:0;}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#050507;color:#ece9f5;
  padding:11px 13px;min-height:100vh;display:flex;flex-direction:column;gap:5px;justify-content:center;}
.badge{font-size:10px;letter-spacing:.11em;text-transform:uppercase;color:#a855f7;font-weight:700;
  display:flex;align-items:center;gap:6px;}
.dot{width:7px;height:7px;border-radius:50%;background:#5fd49a;flex:0 0 auto;}
.dot.idle{background:#6b6580;}
.dot.paused{background:#e0a36b;}
.title{font-size:15px;font-weight:700;line-height:1.15;overflow:hidden;text-overflow:ellipsis;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;}
.artist{font-size:11px;color:#9a93b3;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}
</style></head>
<body>
<div class="badge"><span class="dot __DOT__" id="dot"></span><span id="status">Player · __STATUS__</span></div>
<div class="title" id="title">__TITLE__</div>
<div class="artist" id="artist">__ARTIST__</div>
<script>
async function tick(){
  try{
    const r=await fetch('/api/status',{cache:'no-store'});
    const d=await r.json();
    const tr=d.current_track, st=d.state||{};
    const dot=document.getElementById('dot'), status=document.getElementById('status');
    if(tr){
      const paused=!!st.paused;
      document.getElementById('title').textContent=tr.title||'Unknown';
      document.getElementById('artist').textContent=tr.artist||'';
      status.textContent='Player · '+(paused?'Paused':'Now Playing');
      dot.className='dot'+(paused?' paused':'');
    }else{
      document.getElementById('title').textContent='Nothing playing';
      document.getElementById('artist').textContent=(d.library_count||0)+' tracks in library';
      status.textContent='Player · Idle';
      dot.className='dot idle';
    }
  }catch(e){}
}
setInterval(tick,5000);
</script>
</body></html>"""


def mini_sym_page() -> str:
    payload = status_payload()
    track = payload.get("current_track")
    track = track if isinstance(track, dict) else None
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    count = int(payload.get("library_count", 0) or 0)
    if track:
        paused = bool(state.get("paused"))
        status = "Paused" if paused else "Now Playing"
        dot = "paused" if paused else ""
        title = str(track.get("title") or "Unknown")
        artist = str(track.get("artist") or "")
    else:
        status = "Idle"
        dot = "idle"
        title = "Nothing playing"
        artist = f"{count} tracks in library"
    return (
        MINI_TEMPLATE
        .replace("__DOT__", html.escape(dot))
        .replace("__STATUS__", html.escape(status))
        .replace("__TITLE__", html.escape(title))
        .replace("__ARTIST__", html.escape(artist))
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Player MP3 module server")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8010")))
    args = parser.parse_args()

    ensure_dirs()
    load_state()
    print(f"Player {__version__} serving {HOME} on http://{args.host}:{args.port}")
    print(f"Audio backend: mpv {'found' if PLAYER.available else 'missing'}")
    update_display(force=True)
    threading.Thread(target=display_loop, name="player-display-loop", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        PLAYER.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
