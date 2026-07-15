from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("JUKEBOX_HOME", str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image, ImageEnhance, ImageFilter, ImageOps  # noqa: E402

from jukebox import server  # noqa: E402


def relative_from_playlist(path: Path) -> str:
    return Path(os.path.relpath(path, server.PLAYLIST_DIR)).as_posix()


def crop_square(image: Image.Image) -> Image.Image:
    width, height = image.size
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    return image.crop((left, top, left + edge, top + edge))


def prepare_art(source: Path, stem: str) -> dict[str, str]:
    source = source.resolve()
    lcd_dir = PROJECT_ROOT / "assets" / "covers" / "lcd"
    preview_dir = PROJECT_ROOT / "assets" / "covers" / "preview"
    lcd_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)

    image = Image.open(source).convert("RGB")
    square = crop_square(image)

    color_16 = square.resize((64, 64), Image.Resampling.BOX).quantize(colors=16, method=Image.Quantize.MEDIANCUT)
    color_preview = color_16.convert("RGB").resize((512, 512), Image.Resampling.NEAREST)
    color_preview_path = preview_dir / f"{stem}_16color_512.png"
    color_preview.save(color_preview_path)

    gray = ImageOps.grayscale(square)
    gray = ImageOps.autocontrast(gray, cutoff=2)
    gray = ImageEnhance.Contrast(gray).enhance(1.8)
    gray = gray.filter(ImageFilter.UnsharpMask(radius=1, percent=180, threshold=2))

    lcd_outputs: dict[str, str] = {}
    for size in (48, 64):
        small = gray.resize((size, size), Image.Resampling.BOX)
        mono = small.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
        output_path = lcd_dir / f"{stem}_{size}x{size}_mono.png"
        mono.save(output_path)
        preview_path = preview_dir / f"{stem}_{size}x{size}_mono_preview.png"
        mono.resize((size * 8, size * 8), Image.Resampling.NEAREST).save(preview_path)
        lcd_outputs[f"mono_{size}"] = str(output_path.relative_to(PROJECT_ROOT).as_posix())
        lcd_outputs[f"mono_{size}_preview"] = str(preview_path.relative_to(PROJECT_ROOT).as_posix())

    return {
        "source": str(source.relative_to(PROJECT_ROOT).as_posix()),
        "preview_16color": str(color_preview_path.relative_to(PROJECT_ROOT).as_posix()),
        **lcd_outputs,
    }


def assign_to_playlist(playlist_slug: str, source_path: Path, lcd_path: Path) -> dict[str, object]:
    playlist = server.playlist_by_slug(playlist_slug)
    if not playlist:
        raise SystemExit(f"Playlist not found: {playlist_slug}")
    track_ids = [str(track_id) for track_id in playlist.get("track_ids", [])]
    return server.write_m3u_playlist(
        str(playlist["name"]),
        str(playlist["slug"]),
        track_ids,
        cover=relative_from_playlist(source_path),
        cover_lcd=relative_from_playlist(lcd_path),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build pixel cover art for Jukebox playlists and albums")
    parser.add_argument("source", type=Path)
    parser.add_argument("--stem", default="")
    parser.add_argument("--playlist", default="")
    args = parser.parse_args()

    stem = args.stem or args.source.stem
    outputs = prepare_art(args.source, stem)
    result: dict[str, object] = {"ok": True, "outputs": outputs}

    if args.playlist:
        lcd_path = PROJECT_ROOT / outputs["mono_48"]
        result["playlist"] = assign_to_playlist(args.playlist, args.source.resolve(), lcd_path.resolve())

    manifest_path = PROJECT_ROOT / "assets" / "covers" / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[stem] = result
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
