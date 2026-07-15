from __future__ import annotations

import os
import socket
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WIDTH = 128
HEIGHT = 64
DEFAULT_I2C_BUS = 1
DEFAULT_I2C_ADDRESS = 0x3C


def clip_text(value: str, width: int) -> str:
    text = str(value or "").upper()
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "~"


def marquee_text(value: str, width: int, frame: int) -> str:
    text = str(value or "").upper()
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    loop = text + "   "
    offset = (frame // 2) % len(loop)
    return (loop + loop)[offset : offset + width]


def row_text(value: str, selected: bool, total_width: int, frame: int) -> str:
    marker = ">" if selected else " "
    body_width = max(0, total_width - 1)
    body = marquee_text(value, body_width, frame) if selected else clip_text(value, body_width)
    return marker + body


def text_size(draw: Any, value: str, font: Any) -> tuple[int, int]:
    box = draw.textbbox((0, 0), value, font=font)
    return box[2] - box[0], box[3] - box[1]


def hex_to_rgb(value: object, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    text = str(value or "").strip()
    if text.startswith("#") and len(text) == 7:
        try:
            return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
        except ValueError:
            return fallback
    return fallback


def draw_clipped_text(draw: Any, xy: tuple[int, int], value: str, font: Any, fill: Any, max_width: int) -> None:
    text = str(value or "")
    if text_size(draw, text, font)[0] <= max_width:
        draw.text(xy, text, font=font, fill=fill)
        return
    while text and text_size(draw, text + "...", font)[0] > max_width:
        text = text[:-1]
    draw.text(xy, (text + "...") if text else "...", font=font, fill=fill)


def draw_marquee_pixels(
    draw: Any,
    xy: tuple[int, int],
    value: str,
    font: Any,
    fill: Any,
    max_width: int,
    frame: int,
) -> None:
    text = str(value or "")
    width, _ = text_size(draw, text, font)
    if width <= max_width:
        draw.text(xy, text, font=font, fill=fill)
        return
    loop = text + "    "
    loop_width, _ = text_size(draw, loop, font)
    offset = (frame * 3) % max(1, loop_width)
    x, y = xy
    draw.text((x - offset, y), loop + loop, font=font, fill=fill)


def load_font(size: int, bold: bool = False) -> Any:
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/consolab.ttf" if bold else "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


class SSD1306:
    def __init__(
        self,
        bus_id: int = DEFAULT_I2C_BUS,
        address: int = DEFAULT_I2C_ADDRESS,
        width: int = WIDTH,
        height: int = HEIGHT,
    ) -> None:
        import smbus  # type: ignore[import-not-found]

        self.bus_id = bus_id
        self.address = address
        self.width = width
        self.height = height
        self.bus = smbus.SMBus(bus_id)
        self._init_display()

    def command(self, value: int) -> None:
        self.bus.write_byte_data(self.address, 0x00, value)

    def data(self, values: list[int]) -> None:
        for index in range(0, len(values), 32):
            self.bus.write_i2c_block_data(self.address, 0x40, values[index : index + 32])

    def _init_display(self) -> None:
        for value in (
            0xAE,
            0x20,
            0x00,
            0xB0,
            0xC8,
            0x00,
            0x10,
            0x40,
            0x81,
            0xCF,
            0xA1,
            0xA6,
            0xA8,
            self.height - 1,
            0xA4,
            0xD3,
            0x00,
            0xD5,
            0x80,
            0xD9,
            0xF1,
            0xDA,
            0x12,
            0xDB,
            0x40,
            0x8D,
            0x14,
            0xAF,
        ):
            self.command(value)

    def show(self, image: Any) -> None:
        mono = image.convert("1")
        pixels = mono.load()
        pages = self.height // 8
        buffer: list[int] = []
        for page in range(pages):
            for x in range(self.width):
                byte = 0
                for bit in range(8):
                    y = page * 8 + bit
                    if pixels[x, y]:
                        byte |= 1 << bit
                buffer.append(byte)

        self.command(0x21)
        self.command(0)
        self.command(self.width - 1)
        self.command(0x22)
        self.command(0)
        self.command(pages - 1)
        self.data(buffer)


class GpioOutput:
    def __init__(self, pin: int, initial: bool = False) -> None:
        self.pin = pin
        self.kind = ""
        self._device: Any = None
        self._chip: Any = None
        self._lgpio: Any = None
        self._gpio: Any = None

        try:
            from gpiozero import OutputDevice  # type: ignore[import-not-found]

            self._device = OutputDevice(pin, active_high=True, initial_value=initial)
            self.kind = "gpiozero"
            return
        except Exception:
            pass

        try:
            import lgpio  # type: ignore[import-not-found]

            self._lgpio = lgpio
            self._chip = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_output(self._chip, pin, 1 if initial else 0)
            self.kind = "lgpio"
            return
        except Exception:
            self._chip = None
            self._lgpio = None

        try:
            import RPi.GPIO as GPIO  # type: ignore[import-not-found]

            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH if initial else GPIO.LOW)
            self._gpio = GPIO
            self.kind = "RPi.GPIO"
            return
        except Exception as exc:
            raise RuntimeError(f"GPIO output {pin} unavailable: {exc}") from exc

    def on(self) -> None:
        if self._device:
            self._device.on()
        elif self._lgpio and self._chip is not None:
            self._lgpio.gpio_write(self._chip, self.pin, 1)
        elif self._gpio:
            self._gpio.output(self.pin, self._gpio.HIGH)

    def off(self) -> None:
        if self._device:
            self._device.off()
        elif self._lgpio and self._chip is not None:
            self._lgpio.gpio_write(self._chip, self.pin, 0)
        elif self._gpio:
            self._gpio.output(self.pin, self._gpio.LOW)


class ILI9341:
    def __init__(
        self,
        bus: int = 0,
        device: int = 0,
        dc_pin: int = 25,
        rst_pin: int = 24,
        width: int = 240,
        height: int = 320,
        speed_hz: int = 24_000_000,
    ) -> None:
        import spidev  # type: ignore[import-not-found]

        self.bus = bus
        self.device = device
        self.dc_pin = dc_pin
        self.rst_pin = rst_pin
        self.width = width
        self.height = height
        self.speed_hz = speed_hz
        self.dc = GpioOutput(dc_pin, initial=False)
        self.rst = GpioOutput(rst_pin, initial=True)
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0
        self.spi.no_cs = False
        self._init_display()

    def _write(self, values: bytes | bytearray | list[int]) -> None:
        data = bytes(values)
        for index in range(0, len(data), 4096):
            chunk = data[index : index + 4096]
            if hasattr(self.spi, "writebytes2"):
                self.spi.writebytes2(chunk)
            else:
                self.spi.xfer2(list(chunk))

    def command(self, value: int, data: bytes | bytearray | list[int] | None = None, delay: float = 0) -> None:
        self.dc.off()
        self._write([value & 0xFF])
        if data:
            self.dc.on()
            self._write(data)
        if delay:
            time.sleep(delay)

    def reset(self) -> None:
        self.rst.on()
        time.sleep(0.05)
        self.rst.off()
        time.sleep(0.05)
        self.rst.on()
        time.sleep(0.12)

    def _init_display(self) -> None:
        self.reset()
        self.command(0x01, delay=0.12)
        self.command(0x28)
        self.command(0xCF, [0x00, 0xC1, 0x30])
        self.command(0xED, [0x64, 0x03, 0x12, 0x81])
        self.command(0xE8, [0x85, 0x00, 0x78])
        self.command(0xCB, [0x39, 0x2C, 0x00, 0x34, 0x02])
        self.command(0xF7, [0x20])
        self.command(0xEA, [0x00, 0x00])
        self.command(0xC0, [0x23])
        self.command(0xC1, [0x10])
        self.command(0xC5, [0x3E, 0x28])
        self.command(0xC7, [0x86])
        self.command(0x36, [0x48])
        self.command(0x3A, [0x55])
        self.command(0xB1, [0x00, 0x18])
        self.command(0xB6, [0x08, 0x82, 0x27])
        self.command(0xF2, [0x00])
        self.command(0x26, [0x01])
        self.command(0xE0, [0x0F, 0x31, 0x2B, 0x0C, 0x0E, 0x08, 0x4E, 0xF1, 0x37, 0x07, 0x10, 0x03, 0x0E, 0x09, 0x00])
        self.command(0xE1, [0x00, 0x0E, 0x14, 0x03, 0x11, 0x07, 0x31, 0xC1, 0x48, 0x08, 0x0F, 0x0C, 0x31, 0x36, 0x0F])
        self.command(0x11, delay=0.12)
        self.command(0x29, delay=0.02)

    def set_window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self.command(0x2A, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self.command(0x2B, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self.command(0x2C)

    def _rgb565(self, image: Any) -> bytearray:
        rgb = image.convert("RGB")
        if rgb.size != (self.width, self.height):
            rgb = rgb.resize((self.width, self.height))
        raw = rgb.tobytes()
        output = bytearray(self.width * self.height * 2)
        out_index = 0
        for index in range(0, len(raw), 3):
            red = raw[index]
            green = raw[index + 1]
            blue = raw[index + 2]
            value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
            output[out_index] = value >> 8
            output[out_index + 1] = value & 0xFF
            out_index += 2
        return output

    def show(self, image: Any) -> None:
        self.set_window(0, 0, self.width - 1, self.height - 1)
        self.dc.on()
        self._write(self._rgb565(image))


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "no wifi"


@dataclass
class OledJukeboxDisplay:
    enabled: bool = True
    bus_id: int = DEFAULT_I2C_BUS
    address: int = DEFAULT_I2C_ADDRESS

    def __post_init__(self) -> None:
        self.display: SSD1306 | None = None
        self.error = ""
        self.frames = 0
        self.cover_cache: dict[str, Any] = {}

    def ensure(self) -> bool:
        if not self.enabled:
            return False
        if self.display:
            return True
        try:
            self.display = SSD1306(bus_id=self.bus_id, address=self.address)
            self.error = ""
            return True
        except Exception as exc:
            self.display = None
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "ready": self.display is not None,
            "bus": self.bus_id,
            "address": f"0x{self.address:02x}",
            "frames": self.frames,
            "error": self.error,
        }

    def wants_forced_render(self) -> bool:
        return False

    def reinitialize(self) -> bool:
        self.display = None
        return self.ensure()

    def render(self, payload: dict[str, object]) -> None:
        if not self.ensure() or not self.display:
            return

        from PIL import Image, ImageDraw, ImageFont

        if "breadcrumb" in payload and "items" in payload:
            self.render_menu_payload(payload, Image, ImageDraw, ImageFont)
            return

        state = payload.get("state", {})
        current = payload.get("current_track")
        jukebox = payload.get("jukebox", {})

        if not isinstance(state, dict):
            state = {}
        if not isinstance(jukebox, dict):
            jukebox = {}

        title = "NO TRACK"
        filename = ""
        if isinstance(current, dict):
            title = str(current.get("name") or "NO TRACK")
            filename = str(current.get("filename") or "")

        volume = int(state.get("volume", 0) or 0)
        paused = bool(state.get("paused", False))
        queue_name = str(state.get("queue_name") or "All Tracks")
        track_count = int(payload.get("library_count", 0) or 0)
        audio_ok = bool(jukebox.get("available", False))

        image = Image.new("1", (WIDTH, HEIGHT), 0)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()

        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=1, fill=0)
        draw.text((3, 2), "JUKEBOX", font=font, fill=1)
        draw.text((79, 2), "PAUSE" if paused else "PLAY", font=font, fill=1)
        draw.line((0, 12, WIDTH - 1, 12), fill=1)

        lines = textwrap.wrap(title.upper(), width=19)[:2] or ["NO TRACK"]
        draw.text((3, 16), lines[0], font=font, fill=1)
        if len(lines) > 1:
            draw.text((3, 26), lines[1], font=font, fill=1)
        elif filename:
            draw.text((3, 26), filename[:19], font=font, fill=1)

        draw.text((3, 39), f"VOL {volume:03d}", font=font, fill=1)
        draw.rectangle((47, 42, 118, 47), outline=1, fill=0)
        draw.rectangle((48, 43, 48 + int(69 * max(0, min(100, volume)) / 100), 46), fill=1)

        footer = f"{track_count} SONGS"
        if not audio_ok:
            footer = "MPV MISSING"
        elif queue_name:
            footer = queue_name.upper()[:19]
        draw.text((3, 53), footer, font=font, fill=1)

        self.display.show(image)
        self.frames += 1

    def render_menu_payload(self, payload: dict[str, object], Image: Any, ImageDraw: Any, ImageFont: Any) -> None:
        state = payload.get("state", {})
        current = payload.get("current_track")
        items = payload.get("items", [])
        path = payload.get("path", ["Home"])

        if not isinstance(state, dict):
            state = {}
        if not isinstance(items, list):
            items = []
        if not isinstance(path, list):
            path = ["Home"]

        title = "NO TRACK"
        if isinstance(current, dict):
            title = str(current.get("name") or "NO TRACK")

        volume = int(state.get("volume", 0) or 0)
        paused = bool(state.get("paused", False))
        cursor = int(payload.get("cursor", 0) or 0)
        breadcrumb = str(payload.get("breadcrumb") or "Home").upper()
        track_count = int(payload.get("library_count", 0) or 0)
        frame = int(payload.get("frame", self.frames) or 0)

        image = Image.new("1", (WIDTH, HEIGHT), 0)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()

        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=1, fill=0)

        if path == ["Home"]:
            logo = "JUKEBOX" if frame % 4 else "PL>YER"
            mode = "PAUSE" if paused else "PLAY"
            draw.text((3, 2), logo, font=font, fill=1)
            draw.text((91, 2), mode, font=font, fill=1)
            draw.line((0, 12, WIDTH - 1, 12), fill=1)

            deck = (5, 14, 122, 38)
            draw.rectangle(deck, outline=1, fill=0)
            draw.rectangle((13, 19, 114, 33), outline=1, fill=0)
            draw.ellipse((22, 19, 36, 33), outline=1, fill=0)
            draw.ellipse((91, 19, 105, 33), outline=1, fill=0)
            draw.ellipse((27, 24, 31, 28), outline=1, fill=1)
            draw.ellipse((96, 24, 100, 28), outline=1, fill=1)
            draw.line((38, 27, 89, 27), fill=1)
            draw.polygon([(58, 21), (58, 33), (69, 27)], outline=1, fill=1 if not paused else 0)

            for index in range(10):
                raw = (frame * (index + 3) + index * 5) % 19
                height = 2 if paused else 2 + raw // 3
                x = 8 + index * 5
                draw.rectangle((x, 62 - height, x + 2, 62), outline=1, fill=1)

            scroll = (frame // 2) % max(1, len(title) + 19)
            marquee = (" " * 19 + title.upper() + "   ")[scroll : scroll + 19]
            if len(marquee) < 19:
                marquee = (marquee + " " * 19)[:19]
            draw.text((3, 43), marquee, font=font, fill=1)
            draw.text((78, 55), f"{track_count:02d} SONGS", font=font, fill=1)
            self.display.show(image)
            self.frames += 1
            return

        draw.text((3, 2), marquee_text(breadcrumb, 20, frame), font=font, fill=1)
        draw.line((0, 12, WIDTH - 1, 12), fill=1)

        is_volume = bool(path and path[-1] == "Volume")
        playlist = payload.get("current_playlist")
        album = payload.get("current_album")
        cover_item = playlist if isinstance(playlist, dict) else album
        cover_path = ""
        if isinstance(cover_item, dict):
            cover_path = str(cover_item.get("cover_lcd_path") or "")

        is_playlist_cover_view = (
            (
                path == ["Home", "Menu", "Playlists"]
                or path == ["Home", "Menu", "Albums"]
                or (len(path) == 4 and path[:3] in (["Home", "Menu", "Playlists"], ["Home", "Menu", "Albums"]))
            )
            and cover_path
        )

        if is_playlist_cover_view:
            draw.rectangle((2, 16, 47, 61), outline=1, fill=0)
            try:
                cover = self.cover_cache.get(cover_path)
                if cover is None:
                    cover = Image.open(Path(cover_path)).convert("1").resize((44, 44))
                    self.cover_cache[cover_path] = cover
                image.paste(cover, (3, 17))
            except Exception:
                draw.text((8, 33), "NO ART", font=font, fill=1)

            max_rows = 4
            start = max(0, min(cursor - 1, max(0, len(items) - max_rows)))
            for row in range(max_rows):
                index = start + row
                if index >= len(items):
                    continue
                item = items[index]
                label = str(item.get("label", "")) if isinstance(item, dict) else str(item)
                y = 17 + row * 11
                selected = index == cursor
                text = row_text(label, selected, 12, frame)
                if selected:
                    draw.rectangle((50, y - 1, WIDTH - 2, y + 8), fill=1)
                    draw.text((52, y), text, font=font, fill=0)
                else:
                    draw.text((52, y), text, font=font, fill=1)

            self.display.show(image)
            self.frames += 1
            return

        max_rows = 2 if is_volume else 4
        start = max(0, min(cursor - 1, max(0, len(items) - max_rows)))
        for row in range(max_rows):
            index = start + row
            if index >= len(items):
                continue
            item = items[index]
            label = str(item.get("label", "")) if isinstance(item, dict) else str(item)
            y = 17 + row * 11
            selected = index == cursor
            text = row_text(label, selected, 20, frame)
            if selected:
                draw.rectangle((1, y - 1, WIDTH - 2, y + 8), fill=1)
                draw.text((3, y), text, font=font, fill=0)
            else:
                draw.text((3, y), text, font=font, fill=1)

        if is_volume:
            draw.text((3, 49), f"VOL {volume:03d}", font=font, fill=1)
            draw.rectangle((58, 48, 120, 54), outline=1, fill=0)
            draw.rectangle((60, 50, 60 + int(58 * max(0, min(100, volume)) / 100), 52), fill=1)

        self.display.show(image)
        self.frames += 1

    def render_boot(self) -> None:
        if not self.ensure() or not self.display:
            return

        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("1", (WIDTH, HEIGHT), 0)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=1, fill=0)
        draw.text((3, 4), "JUKEBOX", font=font, fill=1)
        draw.text((3, 20), "starting suit", font=font, fill=1)
        draw.text((3, 35), f"ip {local_ip()}", font=font, fill=1)
        draw.text((3, 50), "port 8010", font=font, fill=1)
        self.display.show(image)
        self.frames += 1


@dataclass
class TftJukeboxDisplay:
    enabled: bool = True
    bus: int = 0
    device: int = 0
    dc_pin: int = 25
    rst_pin: int = 24
    width: int = 240
    height: int = 320
    speed_hz: int = 24_000_000

    def __post_init__(self) -> None:
        self.display: ILI9341 | None = None
        self.error = ""
        self.frames = 0
        self.reinit_count = 0
        self.last_reinit_at = 0.0
        self.reinit_interval = env_int("JUKEBOX_TFT_REINIT_SECONDS", 0)
        self.cover_cache: dict[str, Any] = {}

    def ensure(self) -> bool:
        if not self.enabled:
            return False
        if self.display:
            return True
        try:
            self.display = ILI9341(
                bus=self.bus,
                device=self.device,
                dc_pin=self.dc_pin,
                rst_pin=self.rst_pin,
                width=self.width,
                height=self.height,
                speed_hz=self.speed_hz,
            )
            self.error = ""
            self.last_reinit_at = time.monotonic()
            return True
        except Exception as exc:
            self.display = None
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "ready": self.display is not None,
            "kind": "ili9341",
            "spi_bus": self.bus,
            "spi_device": self.device,
            "dc_gpio": self.dc_pin,
            "rst_gpio": self.rst_pin,
            "size": f"{self.width}x{self.height}",
            "speed_hz": self.speed_hz,
            "frames": self.frames,
            "reinit_seconds": self.reinit_interval,
            "reinit_count": self.reinit_count,
            "error": self.error,
        }

    def wants_forced_render(self) -> bool:
        return bool(
            self.enabled
            and self.reinit_interval > 0
            and time.monotonic() - self.last_reinit_at >= self.reinit_interval
        )

    def reinitialize(self) -> bool:
        if not self.ensure() or not self.display:
            return False
        try:
            self.display._init_display()
            self.last_reinit_at = time.monotonic()
            self.reinit_count += 1
            self.error = ""
            return True
        except Exception as exc:
            self.display = None
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def _asset_root(self) -> Path:
        home = Path(os.environ.get("JUKEBOX_HOME", Path.cwd())).resolve()
        return Path(os.environ.get("JUKEBOX_ASSETS", home / "assets")).resolve()

    def _library_root(self) -> Path:
        home = Path(os.environ.get("JUKEBOX_HOME", Path.cwd())).resolve()
        return Path(os.environ.get("JUKEBOX_LIBRARY", home / "library")).resolve()

    def _path_from_source(self, value: object) -> Path | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.startswith("/assets/"):
            return (self._asset_root() / text.removeprefix("/assets/")).resolve()
        if text.startswith("/library-art/"):
            return (self._library_root() / text.removeprefix("/library-art/")).resolve()
        path = Path(text)
        if path.is_file():
            return path.resolve()
        return None

    def _load_cover(self, source: object, size: int, Image: Any) -> Any | None:
        path = self._path_from_source(source)
        if not path or not path.is_file():
            return None
        cache_key = f"{path}:{size}"
        cached = self.cover_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            image = Image.open(path).convert("RGB")
            edge = min(image.size)
            left = (image.width - edge) // 2
            top = (image.height - edge) // 2
            image = image.crop((left, top, left + edge, top + edge))
            image = image.resize((size, size), Image.Resampling.NEAREST)
            self.cover_cache[cache_key] = image
            return image
        except Exception:
            return None

    def _cover_from_item(self, item: object, size: int, Image: Any) -> Any | None:
        if not isinstance(item, dict):
            return None
        for key in ("album_cover_pixel", "cover_pixel", "cover", "album_cover", "cover_lcd_path", "cover_lcd"):
            cover = self._load_cover(item.get(key), size, Image)
            if cover is not None:
                return cover
        return None

    def _draw_cover(
        self,
        image: Any,
        draw: Any,
        item: object,
        box: tuple[int, int, int, int],
        Image: Any,
        outline: tuple[int, int, int] = (151, 85, 255),
        text_fill: tuple[int, int, int] = (226, 214, 255),
        fallback_outline: tuple[int, int, int] = (100, 64, 172),
        panel_fill: tuple[int, int, int] = (18, 15, 27),
    ) -> None:
        x0, y0, x1, y1 = box
        size = min(x1 - x0, y1 - y0)
        draw.rectangle((x0, y0, x0 + size, y0 + size), fill=panel_fill, outline=outline)
        cover = self._cover_from_item(item, size - 6, Image)
        if cover is not None:
            image.paste(cover, (x0 + 3, y0 + 3))
            draw.rectangle((x0, y0, x0 + size, y0 + size), outline=text_fill, width=2)
            return
        draw.rectangle((x0 + 8, y0 + 8, x0 + size - 8, y0 + size - 8), outline=fallback_outline)
        draw.text((x0 + 14, y0 + size // 2 - 7), "NO ART", font=load_font(10, True), fill=text_fill)

    def _draw_marquee_box(
        self,
        image: Any,
        box: tuple[int, int, int, int],
        value: str,
        font: Any,
        fill: tuple[int, int, int],
        background: tuple[int, int, int],
        frame: int,
    ) -> None:
        from PIL import Image, ImageDraw

        x0, y0, x1, y1 = box
        width = max(1, x1 - x0)
        height = max(1, y1 - y0)
        temp = Image.new("RGB", (width, height), background)
        temp_draw = ImageDraw.Draw(temp)
        text = str(value or "")
        text_width, _ = text_size(temp_draw, text, font)
        if text_width <= width:
            temp_draw.text((0, 0), text, font=font, fill=fill)
        else:
            loop = text + "    "
            loop_width, _ = text_size(temp_draw, loop, font)
            offset = (frame * 3) % max(1, loop_width)
            temp_draw.text((-offset, 0), loop + loop, font=font, fill=fill)
        image.paste(temp, (x0, y0))

    def render(self, payload: dict[str, object]) -> None:
        if not self.ensure() or not self.display:
            return

        from PIL import Image, ImageDraw

        if self.wants_forced_render():
            self.reinitialize()
            if not self.display:
                return

        image = Image.new("RGB", (self.width, self.height), (5, 5, 10))
        draw = ImageDraw.Draw(image)
        if "breadcrumb" in payload and "items" in payload:
            self.render_menu_payload(payload, image, draw, Image)
        else:
            self.render_home(payload, image, draw, Image)

        self.display.show(image)
        self.frames += 1

    def render_home(self, payload: dict[str, object], image: Any, draw: Any, Image: Any) -> None:
        state = payload.get("state", {})
        current = payload.get("current_track")
        if not isinstance(state, dict):
            state = {}
        if not isinstance(current, dict):
            current = {}

        frame = int(payload.get("frame", self.frames) or 0)
        title = str(current.get("name") or "No track")
        artist = str(current.get("artist") or current.get("album") or "Jukebox")
        queue_name = str(state.get("queue_name") or "All Songs")
        paused = bool(state.get("paused", False))
        volume = int(state.get("volume", 0) or 0)
        song_count = int(payload.get("library_count", 0) or 0)
        try:
            focus = int(payload.get("cursor", state.get("ui_cursor", 0)) or 0)
        except (TypeError, ValueError):
            focus = 0
        focus = max(0, min(3, focus))
        theme = payload.get("theme", {})
        if not isinstance(theme, dict):
            theme = {}
        bg = hex_to_rgb(theme.get("bg"), (5, 5, 10))
        panel = hex_to_rgb(theme.get("panel"), (13, 14, 22))
        header = hex_to_rgb(theme.get("header"), (17, 11, 31))
        row_hot = hex_to_rgb(theme.get("rowHot"), (71, 35, 123))
        edge = hex_to_rgb(theme.get("edge"), (190, 122, 255))
        text_color = hex_to_rgb(theme.get("text"), (245, 239, 255))
        muted = hex_to_rgb(theme.get("muted"), (160, 143, 188))
        cyan = hex_to_rgb(theme.get("cyan"), (93, 222, 255))
        purple = hex_to_rgb(theme.get("purple"), (190, 122, 255))
        grid = hex_to_rgb(theme.get("grid"), (25, 17, 40))

        draw.rectangle((0, 0, self.width, self.height), fill=bg)
        for y in range(48, 276, 12):
            draw.line((12, y, 228, y), fill=grid)

        draw.rectangle((0, 0, self.width, 40), fill=header)
        draw.text((12, 8), "JUKEBOX", font=load_font(15, True), fill=text_color)
        draw.rounded_rectangle(
            (98, 7, 152, 27),
            radius=4,
            fill=row_hot if focus == 0 else panel,
            outline=cyan if focus == 0 else edge,
            width=2 if focus == 0 else 1,
        )
        draw.text((109, 12), "MENU", font=load_font(9, True), fill=text_color)

        draw.rounded_rectangle((29, 43, 211, 225), radius=7, fill=panel, outline=grid, width=1)
        self._draw_cover(image, draw, current, (40, 52, 200, 212), Image, outline=edge, text_fill=text_color, fallback_outline=purple, panel_fill=panel)
        draw.line((31, 74, 31, 51, 54, 51), fill=cyan, width=2)
        draw.line((209, 74, 209, 51, 186, 51), fill=cyan, width=2)
        draw.line((31, 202, 31, 225, 54, 225), fill=cyan, width=2)
        draw.line((209, 202, 209, 225, 186, 225), fill=cyan, width=2)

        draw.text((18, 230), "NOW PLAYING", font=load_font(9, True), fill=purple)
        self._draw_marquee_box(image, (14, 244, 226, 266), title, load_font(17, True), text_color, bg, frame)
        self._draw_marquee_box(image, (14, 265, 226, 282), f"{artist} / {queue_name}", load_font(10), muted, bg, frame // 2)
        controls = [("|<", 14, 58, False, 1), (">" if paused else "||", 80, 80, True, 2), (">|", 168, 58, False, 3)]
        icon_font = load_font(14, True)
        for label, x, width, active, target in controls:
            focused = focus == target
            draw.rounded_rectangle(
                (x, 288, x + width, 314),
                radius=5,
                fill=row_hot if focused else row_hot if active else panel,
                outline=cyan if focused else text_color if active else edge,
                width=2 if focused else 1,
            )
            try:
                label_width = int(draw.textlength(label, font=icon_font))
            except Exception:
                label_width = len(label) * 8
            draw.text((x + max(4, (width - label_width) // 2), 294), label, font=icon_font, fill=text_color)

        base_y = 224
        draw.line((18, base_y + 2, 222, base_y + 2), fill=grid)
        for index in range(30):
            phase = frame * (index + 4) + index * 13
            raw = (phase % 17) + ((phase // 3) % 11)
            height = 3 if paused else min(30, 7 + raw)
            x = 17 + index * 7
            fill = purple if index % 4 == 0 else text_color if index % 7 == 0 else cyan
            draw.rectangle((x, base_y - height, x + 3, base_y), fill=fill)
            if height > 8:
                draw.rectangle((x + 1, base_y - height + 2, x + 2, base_y - 3), fill=bg)
            draw.rectangle((x, base_y + 5, x + 3, base_y + 6), fill=fill)
        wave_points = []
        for x in range(18, 223, 6):
            y = base_y - 15 + (0 if paused else ((frame + x) % 9) - 4)
            wave_points.append((x, y))
        if len(wave_points) > 1:
            draw.line(wave_points, fill=text_color, width=1)

        draw.text((14, 42), f"{song_count} SONGS", font=load_font(9, True), fill=muted)
        draw.text((180, 42), f"VOL {volume:02d}", font=load_font(9, True), fill=muted)

    def render_menu_payload(self, payload: dict[str, object], image: Any, draw: Any, Image: Any) -> None:
        path = payload.get("path", ["Home"])
        items = payload.get("items", [])
        state = payload.get("state", {})
        selected = payload.get("selected")
        current = payload.get("current_track")
        current_playlist = payload.get("current_playlist")
        current_album = payload.get("current_album")

        if not isinstance(path, list):
            path = ["Home"]
        if not isinstance(items, list):
            items = []
        if not isinstance(state, dict):
            state = {}
        if path == ["Home"]:
            self.render_home(payload, image, draw, Image)
            return

        frame = int(payload.get("frame", self.frames) or 0)
        cursor = int(payload.get("cursor", 0) or 0)
        breadcrumb = str(payload.get("breadcrumb") or "Home")
        message = str(payload.get("message") or "Ready")

        draw.rectangle((0, 0, self.width, self.height), fill=(5, 5, 10))
        draw.rectangle((0, 0, self.width, 42), fill=(17, 11, 31))
        self._draw_marquee_box(image, (10, 8, 230, 27), breadcrumb, load_font(13, True), (245, 239, 255), (17, 11, 31), frame)
        draw.text((12, 28), message[:28], font=load_font(9), fill=(160, 143, 188))

        cover_item = current_playlist if isinstance(current_playlist, dict) else current_album
        if not isinstance(cover_item, dict) and isinstance(current, dict):
            cover_item = current
        has_detail = isinstance(cover_item, dict) and bool(
            cover_item.get("cover") or cover_item.get("cover_pixel") or cover_item.get("album_cover_pixel") or cover_item.get("cover_lcd_path")
        )

        rows_top = 52
        if has_detail:
            self._draw_cover(image, draw, cover_item, (12, 52, 98, 138), Image)
            title = str(cover_item.get("name") or cover_item.get("label") or cover_item.get("album") or "Selected")
            artist = str(cover_item.get("artist") or cover_item.get("queue_name") or "")
            count = cover_item.get("count") or ""
            if not count and isinstance(cover_item.get("track_ids"), list):
                count = len(cover_item.get("track_ids", []))
            self._draw_marquee_box(image, (108, 58, 228, 80), title, load_font(15, True), (245, 239, 255), (5, 5, 10), frame)
            if artist:
                self._draw_marquee_box(image, (108, 84, 228, 101), artist, load_font(11), (160, 143, 188), (5, 5, 10), frame // 2)
            if count:
                draw.text((108, 111), f"{count} tracks", font=load_font(10, True), fill=(93, 222, 255))
            rows_top = 150

        row_h = 34
        max_rows = max(1, (self.height - rows_top - 8) // row_h)
        start = max(0, min(cursor - 1, max(0, len(items) - max_rows)))
        selected_bg = (71, 35, 123)
        row_bg = (13, 14, 22)
        for row in range(max_rows):
            index = start + row
            y = rows_top + row * row_h
            if index >= len(items):
                break
            item = items[index]
            if not isinstance(item, dict):
                item = {"label": str(item)}
            label = str(item.get("label", ""))
            is_selected = index == cursor
            background = selected_bg if is_selected else row_bg
            outline = (190, 122, 255) if is_selected else (35, 31, 47)
            draw.rounded_rectangle((8, y, 232, y + 28), radius=5, fill=background, outline=outline, width=1)

            item_cover = self._cover_from_item(item, 22, Image)
            text_x = 18
            if item_cover is not None:
                image.paste(item_cover, (16, y + 3))
                text_x = 45
            elif str(item.get("type", "")) in {"album", "playlist"}:
                draw.rectangle((16, y + 6, 28, y + 18), outline=(226, 214, 255))
                text_x = 38
            fill = (255, 255, 255) if is_selected else (208, 196, 231)
            self._draw_marquee_box(image, (text_x, y + 6, 221, y + 24), label, load_font(12, is_selected), fill, background, frame)

    def render_boot(self) -> None:
        if not self.ensure() or not self.display:
            return

        from PIL import Image, ImageDraw

        image = Image.new("RGB", (self.width, self.height), (5, 5, 10))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, self.width, self.height), fill=(5, 5, 10))
        draw.rectangle((0, 0, self.width, 52), fill=(17, 11, 31))
        draw.text((16, 15), "JUKEBOX", font=load_font(22, True), fill=(245, 239, 255))
        draw.text((16, 76), "ILI9341 TFT", font=load_font(18, True), fill=(190, 122, 255))
        draw.text((16, 110), f"IP {local_ip()}", font=load_font(14), fill=(226, 214, 255))
        draw.text((16, 136), "PORT 8010", font=load_font(14), fill=(226, 214, 255))
        draw.rounded_rectangle((16, 210, 224, 270), radius=6, outline=(93, 222, 255), width=2)
        draw.text((34, 231), "booting cassette...", font=load_font(13), fill=(93, 222, 255))
        self.display.show(image)
        self.frames += 1


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)), 0)
    except ValueError:
        return default


def create_display() -> OledJukeboxDisplay | TftJukeboxDisplay:
    kind = str(os.environ.get("JUKEBOX_DISPLAY", "oled")).strip().lower()
    if kind in {"tft", "ili9341", "lcd240", "lcd"}:
        return TftJukeboxDisplay(
            enabled=os.environ.get("JUKEBOX_TFT", "1") != "0",
            bus=env_int("JUKEBOX_TFT_SPI_BUS", 0),
            device=env_int("JUKEBOX_TFT_SPI_DEVICE", 0),
            dc_pin=env_int("JUKEBOX_TFT_DC", 25),
            rst_pin=env_int("JUKEBOX_TFT_RST", 24),
            speed_hz=env_int("JUKEBOX_TFT_SPEED", 24_000_000),
        )
    return OledJukeboxDisplay(enabled=os.environ.get("JUKEBOX_OLED", "1") != "0")
