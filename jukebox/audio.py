from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path


class MpvJukebox:
    def __init__(self, ipc_path: Path):
        self.ipc_path = ipc_path
        self.audio_driver = os.environ.get("JUKEBOX_AUDIO_DRIVER", "").strip()
        self.audio_device = os.environ.get("JUKEBOX_AUDIO_DEVICE", "").strip()
        self.alsa_card = os.environ.get("JUKEBOX_ALSA_CARD", "").strip()
        self.alsa_control = os.environ.get("JUKEBOX_ALSA_CONTROL", "Headphone").strip()
        self.alsa_volume = os.environ.get("JUKEBOX_ALSA_VOLUME", "").strip()
        self.process: subprocess.Popen[bytes] | None = None
        self.request_id = 0

    @property
    def available(self) -> bool:
        return shutil.which("mpv") is not None

    def ensure_started(self) -> None:
        if not self.available:
            raise RuntimeError("mpv is not installed. Run: sudo apt install -y mpv")
        if self.process and self.process.poll() is None and self.ipc_path.exists():
            return

        if self.process and self.process.poll() is None:
            self.process.terminate()
        try:
            self.ipc_path.unlink()
        except FileNotFoundError:
            pass
        self.configure_mixer()

        args = [
            "mpv",
            "--no-video",
            "--idle=yes",
            "--force-window=no",
            "--really-quiet",
            f"--input-ipc-server={self.ipc_path}",
        ]
        if self.audio_driver:
            args.append(f"--ao={self.audio_driver}")
        if self.audio_device:
            args.append(f"--audio-device={self.audio_device}")
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        deadline = time.time() + 3
        while time.time() < deadline:
            if self.ipc_path.exists():
                return
            if self.process.poll() is not None:
                raise RuntimeError("mpv exited while starting")
            time.sleep(0.05)
        raise RuntimeError("mpv IPC socket did not appear")

    def configure_mixer(self) -> None:
        if not self.alsa_card or not self.alsa_volume or shutil.which("amixer") is None:
            return
        subprocess.run(
            ["amixer", "-c", self.alsa_card, "set", self.alsa_control, self.alsa_volume, "unmute"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def command(self, command: list[object]) -> dict[str, object]:
        self.ensure_started()
        self.request_id += 1
        request_id = self.request_id
        payload = json.dumps({"command": command, "request_id": request_id}).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.ipc_path))
            client.sendall(payload)
            pending = b""
            deadline = time.time() + 2
            while time.time() < deadline:
                chunk = client.recv(65536)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not line.strip():
                        continue
                    response = json.loads(line.decode("utf-8", errors="replace"))
                    if response.get("request_id") == request_id:
                        return response
        return {"error": "timeout waiting for mpv response"}

    def play(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(path)
        self.command(["loadfile", str(path), "replace"])
        self.command(["set_property", "pause", False])

    def pause(self, paused: bool) -> None:
        self.command(["set_property", "pause", bool(paused)])

    def stop(self) -> None:
        self.command(["stop"])

    def volume(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        self.command(["set_property", "volume", value])

    def shutdown(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.command(["quit"])
            except Exception:
                self.process.terminate()
        self.process = None
        try:
            self.ipc_path.unlink()
        except FileNotFoundError:
            pass

    def snapshot(self) -> dict[str, object]:
        return {
            "available": self.available,
            "running": bool(self.process and self.process.poll() is None),
            "audio_driver": self.audio_driver,
            "audio_device": self.audio_device,
            "alsa_card": self.alsa_card,
            "alsa_control": self.alsa_control,
            "alsa_volume": self.alsa_volume,
            "ipc_path": str(self.ipc_path),
        }


def default_ipc_path(home: Path) -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "jukebox-mpv.sock"
    return home / ".jukebox-mpv.sock"
