from __future__ import annotations

import json
import os
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("JUKEBOX_HOME", str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from jukebox import server  # noqa: E402
from scripts.build_cover_art import prepare_art, relative_from_playlist  # noqa: E402


USER_AGENT = "JukeboxPrototype/0.1.0 (local demo library)"
SAMPLE_RATE = 8_000


@dataclass(frozen=True)
class DemoAlbum:
    artist: str
    title: str
    slug: str
    release_group_id: str
    tracks: tuple[str, ...]


ALBUMS = (
    DemoAlbum(
        "The Beatles",
        "Abbey Road",
        "the_beatles_abbey_road",
        "9162580e-5df4-32de-80cc-f45a8d8a9b1d",
        ("Come Together", "Something", "Maxwell's Silver Hammer", "Oh! Darling", "Here Comes the Sun"),
    ),
    DemoAlbum(
        "Pink Floyd",
        "The Dark Side of the Moon",
        "pink_floyd_dark_side_of_the_moon",
        "f5093c06-23e3-404f-aeaa-40f72885ee3a",
        ("Speak to Me", "Breathe", "Time", "Money", "Us and Them"),
    ),
    DemoAlbum(
        "Michael Jackson",
        "Thriller",
        "michael_jackson_thriller",
        "f32fab67-77dd-3937-addc-9062e28e4c37",
        ("Wanna Be Startin' Somethin'", "Thriller", "Beat It", "Billie Jean", "Human Nature"),
    ),
    DemoAlbum(
        "Nirvana",
        "Nevermind",
        "nirvana_nevermind",
        "1b022e01-4da6-387b-8658-8678046e4cef",
        ("Smells Like Teen Spirit", "In Bloom", "Come as You Are", "Lithium", "Drain You"),
    ),
    DemoAlbum(
        "Fleetwood Mac",
        "Rumours",
        "fleetwood_mac_rumours",
        "416bb5e5-c7d1-3977-8fd7-7c9daf6c2be6",
        ("Second Hand News", "Dreams", "Never Going Back Again", "Don't Stop", "Go Your Own Way"),
    ),
)


OLD_FAKE_TRACKS = (
    "01 neon sidewalk.wav",
    "02 cassette ghost.wav",
    "03 elbow servo blues.wav",
    "04 midnight bootloader.wav",
    "05 tiny screen anthem.wav",
    "06 wired headphones.wav",
)


def safe_filename(value: str) -> str:
    return server.slugify(value, "track").replace("_", " ")


def write_silent_wav(path: Path, seconds: float = 1.5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = int(SAMPLE_RATE * seconds)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(b"\x00\x00" * frame_count)


def download_cover(album: DemoAlbum) -> Path:
    source_dir = PROJECT_ROOT / "assets" / "covers" / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    output_path = source_dir / f"{album.slug}.jpg"
    if output_path.exists() and output_path.stat().st_size > 1024:
        return output_path

    urls = (
        f"https://coverartarchive.org/release-group/{album.release_group_id}/front-500",
        f"https://coverartarchive.org/release-group/{album.release_group_id}/front",
    )
    last_error: Exception | None = None
    for attempt in range(3):
        for url in urls:
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT})
                with urlopen(request, timeout=90) as response:
                    output_path.write_bytes(response.read())
                    return output_path
            except Exception as exc:
                last_error = exc
        sleep(1 + attempt)
    raise RuntimeError(f"Could not download cover for {album.artist} - {album.title}: {last_error}")
    return output_path


def remove_old_demo_content() -> None:
    server.ensure_dirs()
    for path in server.PLAYLIST_DIR.glob("*"):
        if path.is_file() and path.suffix.lower() in {".json", ".m3u8"}:
            path.unlink()
    for filename in OLD_FAKE_TRACKS:
        path = server.LIBRARY_DIR / filename
        if path.exists():
            path.unlink()


def seed() -> dict[str, object]:
    remove_old_demo_content()
    seeded: list[dict[str, object]] = []
    manifest: dict[str, object] = {}

    for album in ALBUMS:
        source = download_cover(album)
        outputs = prepare_art(source, album.slug)
        lcd_path = PROJECT_ROOT / outputs["mono_48"]

        track_ids: list[str] = []
        for index, track_name in enumerate(album.tracks, start=1):
            filename = f"{index:02d} {safe_filename(track_name)}.wav"
            track_path = server.LIBRARY_DIR / f"{album.artist} - {album.title}" / filename
            write_silent_wav(track_path)
            track_ids.append(server.track_id_for(track_path))

        playlist = server.write_m3u_playlist(
            f"{album.artist} - {album.title}",
            album.slug,
            track_ids,
            cover=relative_from_playlist(source),
            cover_lcd=relative_from_playlist(lcd_path),
        )
        seeded.append(playlist)
        manifest[album.slug] = {
            "artist": album.artist,
            "title": album.title,
            "release_group_id": album.release_group_id,
            "cover_archive": f"https://coverartarchive.org/release-group/{album.release_group_id}",
            "outputs": outputs,
            "playlist": playlist,
        }

    manifest_path = PROJECT_ROOT / "assets" / "covers" / "demo_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"ok": True, "albums": seeded}


def main() -> None:
    print(json.dumps(seed(), indent=2))


if __name__ == "__main__":
    main()
