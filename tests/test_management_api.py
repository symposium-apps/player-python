from __future__ import annotations

import base64
from contextlib import ExitStack
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import wave
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "test-only-jukebox-password"


class ManagementApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="jukebox-api-test-")
        cls.root = Path(cls.temp.name)
        cls.user_data = cls.root / "UserData"
        cls.state = cls.root / "State"
        cls.user_data.mkdir()
        cls.state.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        env = os.environ.copy()
        env.update({
            "HOST": "127.0.0.1",
            "PORT": str(cls.port),
            "SYM_APP_STATE_DIR": str(cls.state),
            "SYM_APP_USER_DATA_DIR": str(cls.user_data),
            "JUKEBOX_FAST_STORAGE": "0",
        })
        cls.process = subprocess.Popen(
            [sys.executable, "-m", "jukebox.server"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if cls.request("GET", "/_sym/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            stderr = cls.process.stderr.read() if cls.process.stderr else ""
            raise RuntimeError(f"Jukebox test server did not start: {stderr}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)
        cls.temp.cleanup()

    @classmethod
    def request(cls, method: str, path: str, body: bytes | str | dict | None = None, headers: dict[str, str] | None = None):
        request_headers = dict(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        elif isinstance(body, str):
            body = body.encode()
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=20)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        connection.close()
        return result

    @classmethod
    def json_request(cls, method: str, path: str, body: dict | None = None, password: str | None = None):
        headers = {"Authorization": f"Bearer {password}"} if password is not None else {}
        status, response_headers, raw = cls.request(method, path, body, headers)
        return status, response_headers, json.loads(raw)

    def test_complete_authenticated_management_flow(self) -> None:
        status, _, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<link rel="icon" href="/favicon-32x32.png?v=20260720-1" type="image/png" sizes="32x32">', page)
        status, headers, favicon = self.request("GET", "/favicon.ico")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/x-icon")
        self.assertEqual(favicon[:4], b"\x00\x00\x01\x00")
        status, headers, favicon_png = self.request("GET", "/favicon-32x32.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(favicon_png[:8], b"\x89PNG\r\n\x1a\n")
        status, headers, apple_icon = self.request("GET", "/apple-touch-icon.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(apple_icon[:8], b"\x89PNG\r\n\x1a\n")
        missing = self.json_request("GET", "/api/v1/context")
        wrong_without_config = self.json_request("GET", "/api/v1/context", password="wrong")
        self.assertEqual(missing[0], 401)
        self.assertEqual(missing[2], {"ok": False, "error": "Unauthorized"})
        self.assertEqual(wrong_without_config[2], missing[2])

        api_dir = self.user_data / "Jukebox API"
        self.assertTrue((api_dir / "README.txt").is_file())
        (api_dir / "password.txt").write_text(PASSWORD + "\n", encoding="utf-8")

        status, _, login_page = self.request("GET", "/")
        self.assertEqual(status, 401)
        self.assertIn(b'<link rel="icon" href="/favicon-32x32.png?v=20260720-1" type="image/png" sizes="32x32">', login_page)
        wrong = self.json_request("GET", "/api/v1/context", password="wrong")
        self.assertEqual(wrong[0], 401)
        self.assertEqual(wrong[2], missing[2])
        self.assertEqual(self.json_request("GET", "/api/v1/context", password=PASSWORD)[0], 200)
        self.assertEqual((api_dir / "password.txt").stat().st_mode & 0o777, 0o600)

        form = urllib.parse.urlencode({"password": PASSWORD})
        status, headers, _ = self.request("POST", "/auth/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("GET", "/", headers={"Cookie": cookie})[0], 200)

        wav_path = self.root / "fake.wav"
        with wave.open(str(wav_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\0\0" * 800)
        cover_path = self.root / "cover.png"
        cover_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))

        wav_bytes = wav_path.read_bytes()
        auth = {"Authorization": f"Bearer {PASSWORD}"}
        status, _, raw = self.request(
            "PUT",
            "/api/v1/files/Test%20Artist%20-%20Fake%20Album/01%20Fake%20Track.wav",
            wav_bytes,
            {**auth, "Content-Length": str(len(wav_bytes)), "X-Content-SHA256": hashlib.sha256(wav_bytes).hexdigest()},
        )
        self.assertEqual(status, 201, raw)
        track_id = json.loads(raw)["data"]["track_id"]

        cover_bytes = cover_path.read_bytes()
        status, _, raw = self.request(
            "PUT",
            "/api/v1/files/Test%20Artist%20-%20Fake%20Album/cover.png",
            cover_bytes,
            {**auth, "Content-Length": str(len(cover_bytes))},
        )
        self.assertEqual(status, 201, raw)

        status, _, raw = self.request(
            "PUT",
            "/api/v1/files/%2e%2e/escaped.wav",
            wav_bytes,
            {**auth, "Content-Length": str(len(wav_bytes))},
        )
        self.assertEqual(status, 400, raw)
        self.assertFalse((self.user_data / "escaped.wav").exists())

        status, _, raw = self.request(
            "PUT",
            "/api/v1/files/Test%20Artist%20-%20Fake%20Album/bad.wav",
            wav_bytes,
            {**auth, "Content-Length": str(len(wav_bytes)), "X-Content-SHA256": "0" * 64},
        )
        self.assertEqual(status, 400, raw)
        self.assertFalse((self.user_data / "Music" / "Test Artist - Fake Album" / "bad.wav").exists())

        status, _, scan = self.json_request("POST", "/api/v1/library/rescan", {}, PASSWORD)
        self.assertEqual(status, 200)
        self.assertEqual(len(scan["data"]["tracks"]), 1)
        self.assertEqual(len(scan["data"]["albums"]), 1)
        album = scan["data"]["albums"][0]
        self.assertEqual(album["name"], "Fake Album")
        self.assertTrue(album["cover"])
        self.assertEqual(self.request("GET", album["cover"])[0], 401)
        self.assertEqual(self.request("GET", album["cover"], headers=auth)[0], 200)
        self.assertEqual(self.request("GET", f"/media/{track_id}")[0], 401)
        self.assertEqual(self.request("GET", f"/media/{track_id}", headers=auth)[0], 200)

        status, _, playlist = self.json_request("POST", "/api/v1/playlists", {"name": "Fake Playlist", "track_ids": [track_id]}, PASSWORD)
        self.assertEqual(status, 201)
        slug = playlist["data"]["playlist"]["slug"]
        self.assertEqual(self.json_request("GET", f"/api/v1/playlists/{slug}", password=PASSWORD)[2]["data"]["count"], 1)

        mcp_request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        self.assertEqual(self.json_request("POST", "/mcp", mcp_request, "wrong")[0], 401)
        tools = self.json_request("POST", "/mcp", mcp_request, PASSWORD)[2]["result"]["tools"]
        self.assertGreaterEqual(len(tools), 15)

        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "jukebox_list_albums", "arguments": {}}}
        mcp = self.json_request("POST", "/mcp", call, PASSWORD)[2]
        self.assertFalse(mcp["result"]["isError"])
        self.assertEqual(len(mcp["result"]["structuredContent"]), 1)

        self.assertEqual(self.json_request("DELETE", f"/api/v1/playlists/{slug}", password=PASSWORD)[0], 200)
        self.assertEqual(self.json_request("DELETE", f"/api/v1/albums/{album['slug']}", password=PASSWORD)[0], 200)
        self.assertEqual(self.json_request("GET", "/api/v1/tracks", password=PASSWORD)[2]["data"], [])
        self.assertFalse(list((self.user_data / "Music").rglob("*.wav")))
        self.assertFalse(list((self.user_data / "Music").rglob("*.png")))


class StartupCompatibilityTest(unittest.TestCase):
    def test_embedded_tags_and_artwork_are_extracted(self) -> None:
        from mutagen.id3 import APIC, TALB, TIT2, TPE1  # type: ignore[import-not-found]
        from mutagen.mp3 import MP3  # type: ignore[import-not-found]
        from jukebox import server

        with tempfile.TemporaryDirectory(prefix="jukebox-metadata-test-") as temporary:
            root = Path(temporary).resolve()
            music = root / "Music"
            artwork = root / "Artwork"
            music.mkdir()
            track = music / "tagged.mp3"
            track.write_bytes(
                base64.b64decode(
                    "SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYxLjcuMTAwAAAAAAAAAAAAAAD/4xjEAAAAA0gAAAAATEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/4xjEOwAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/4xjEdgAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/4xjEsQAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU="
                )
            )
            tagged = MP3(track)
            if tagged.tags is None:
                tagged.add_tags()
            assert tagged.tags is not None
            tagged.tags.add(TIT2(encoding=3, text=["Tagged Track"]))
            tagged.tags.add(TPE1(encoding=3, text=["Tagged Artist"]))
            tagged.tags.add(TALB(encoding=3, text=["Tagged Album"]))
            tagged.tags.add(
                APIC(
                    encoding=3,
                    mime="image/png",
                    type=3,
                    desc="Cover",
                    data=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="),
                )
            )
            tagged.save()

            with mock.patch.object(server, "LIBRARY_DIR", music), mock.patch.object(server, "ASSETS_DIR", artwork):
                server.METADATA_CACHE.clear()
                metadata = server.audio_metadata(track)
                self.assertEqual(metadata["title"], "Tagged Track")
                self.assertEqual(metadata["artist"], "Tagged Artist")
                self.assertEqual(metadata["album"], "Tagged Album")
                self.assertTrue(metadata["cover"])
                self.assertTrue(metadata["cover_pixel"])
                self.assertTrue(metadata["cover_lcd"])
                cover_path = server.cover_file_path(metadata["cover"])
                self.assertIsNotNone(cover_path)
                assert cover_path is not None
                self.assertTrue(cover_path.is_file())

    def test_unwritable_api_readme_does_not_crash_startup(self) -> None:
        from jukebox import server

        with tempfile.TemporaryDirectory(prefix="jukebox-startup-test-") as temporary:
            root = Path(temporary)
            api_dir = root / "UserData" / "Jukebox API"
            replacements = {
                "LIBRARY_DIR": root / "UserData" / "Music",
                "PLAYLIST_DIR": root / "UserData" / "Playlists",
                "ASSETS_DIR": root / "UserData" / "Artwork",
                "API_CONFIG_DIR": api_dir,
                "API_README_FILE": api_dir / "README.txt",
            }
            with ExitStack() as stack:
                for name, value in replacements.items():
                    stack.enter_context(mock.patch.object(server, name, value))
                stack.enter_context(mock.patch.object(Path, "write_text", side_effect=PermissionError("read-only UserData child")))
                server.ensure_dirs()
            self.assertTrue(api_dir.is_dir())
            self.assertFalse((api_dir / "README.txt").exists())


if __name__ == "__main__":
    unittest.main()
