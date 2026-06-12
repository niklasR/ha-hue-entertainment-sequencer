"""DTLS stream manager and effect engine for Hue Entertainment.

Transport: pyOpenSSL DTLS (primary) → openssl binary fallback.
Effects run as asyncio tasks; a background thread owns the DTLS socket.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import queue
import random
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import urllib.request
from typing import Callable

from .const import (
    DEFAULT_BRIGHTNESS,
    DEFAULT_CYCLE_SPEED,
    DEFAULT_FLASH_COUNT,
    DEFAULT_PULSE_RATE_HZ,
    DEFAULT_STROBE_HZ,
    EFFECT_CANDLE,
    EFFECT_COLOR_CYCLE,
    EFFECT_CONFETTI,
    EFFECT_FLASH,
    EFFECT_POLICE,
    EFFECT_PULSE,
    EFFECT_SEQUENCE,
    EFFECT_STATIC,
    EFFECT_STROBE,
    EFFECT_THEATER,
    PARAM_BRIGHTNESS,
    PARAM_COLOR,
    PARAM_COLOR_SPEED,
    PARAM_FLASH_COUNT,
    PARAM_PULSE_RATE,
    PARAM_SEQUENCE,
    PARAM_STROBE_HZ,
)

_LOGGER = logging.getLogger(__name__)

_OPENSSL_FALLBACK_PATHS = [
    "/usr/bin/openssl",
    "/usr/local/bin/openssl",
    "/bin/openssl",
]


# ---------------------------------------------------------------------------
# Frame builder
# ---------------------------------------------------------------------------

def _build_frame(lights: list[tuple[int, int, int, int]], seq: int = 0) -> bytes:
    header = (
        b"HueStream"
        + bytes([0x01, 0x00, seq & 0xFF, 0x00, 0x00, 0x00, 0x00])
    )
    payload = bytearray()
    for light_id, r, g, b in lights:
        payload += bytes([0x00]) + struct.pack(">H", light_id) + struct.pack(">HHH", r, g, b)
    return header + bytes(payload)


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[float, float, float]:
    if s == 0:
        return v, v, v
    i = int(h * 6)
    f = h * 6 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    match i % 6:
        case 0: return v, t, p
        case 1: return q, v, p
        case 2: return p, v, t
        case 3: return p, q, v
        case 4: return t, p, v
        case _: return v, p, q


def _rgb255_to_16bit(r: int, g: int, b: int, brightness: int = 255) -> tuple[int, int, int]:
    scale = brightness / 255
    return (
        int(r / 255 * scale * 0xFFFF),
        int(g / 255 * scale * 0xFFFF),
        int(b / 255 * scale * 0xFFFF),
    )


# ---------------------------------------------------------------------------
# DTLS backends
# ---------------------------------------------------------------------------

class _PyOpenSSLBackend:
    """DTLS via pyOpenSSL — no binary needed, uses libssl already in HA."""

    def __init__(self, bridge_ip: str, username: str, clientkey: str) -> None:
        self._bridge_ip = bridge_ip
        self._username = username
        self._clientkey = clientkey
        self._conn = None
        self._sock = None

    def connect(self) -> None:
        from OpenSSL.SSL import Context, Connection  # type: ignore[import]

        # DTLSv1_2_METHOD = 9 per OpenSSL constants
        DTLS_METHOD = getattr(
            __import__("OpenSSL.SSL", fromlist=["DTLSv1_2_METHOD"]),
            "DTLSv1_2_METHOD",
            9,
        )
        psk = bytes.fromhex(self._clientkey)
        psk_id = self._username.encode("utf-8")

        ctx = Context(DTLS_METHOD)
        ctx.set_cipher_list(b"PSK-AES128-GCM-SHA256")
        ctx.set_psk_client_callback(lambda conn, hint: (psk_id, psk))

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.connect((self._bridge_ip, 2100))
        self._sock.settimeout(5.0)

        self._conn = Connection(ctx, self._sock)
        self._conn.set_connect_state()
        self._conn.do_handshake()
        self._sock.settimeout(None)
        _LOGGER.debug("pyOpenSSL DTLS handshake complete")

    def write(self, data: bytes) -> None:
        if self._conn:
            self._conn.write(data)

    def close(self) -> None:
        try:
            if self._conn:
                self._conn.shutdown()
        except Exception:
            pass
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass


class _OpenSSLBinaryBackend:
    """DTLS via openssl(1) subprocess — fallback if pyOpenSSL DTLS unavailable."""

    def __init__(self, bridge_ip: str, username: str, clientkey: str) -> None:
        self._bridge_ip = bridge_ip
        self._username = username
        self._clientkey = clientkey
        self._proc = None

    def _find_binary(self) -> str | None:
        found = shutil.which("openssl")
        if found:
            return found
        for p in _OPENSSL_FALLBACK_PATHS:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        try:
            r = subprocess.run(
                ["find", "/usr", "/bin", "-name", "openssl", "-type", "f", "-maxdepth", "6"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.strip().splitlines():
                line = line.strip()
                if line and os.access(line, os.X_OK):
                    return line
        except Exception:
            pass
        return None

    def connect(self) -> None:
        binary = self._find_binary()
        if not binary:
            raise RuntimeError(
                "Neither pyOpenSSL DTLS nor openssl binary available. "
                "Add to compose.yaml: entrypoint: [\"/bin/sh\",\"-c\","
                "\"apk add --no-cache openssl 2>/dev/null; exec /init\"]"
            )
        self._proc = subprocess.Popen(
            [binary, "s_client",
             "-connect", f"{self._bridge_ip}:2100",
             "-dtls1_2",
             "-psk_identity", self._username,
             "-psk", self._clientkey,
             "-cipher", "PSK-AES128-GCM-SHA256",
             "-quiet"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        import time; time.sleep(0.35)
        _LOGGER.debug("openssl binary DTLS session open (pid %d)", self._proc.pid)

    def write(self, data: bytes) -> None:
        if self._proc and self._proc.stdin and not self._proc.stdin.closed:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()

    def close(self) -> None:
        if self._proc:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
            except Exception:
                pass


def _make_backend(bridge_ip: str, username: str, clientkey: str):
    """Try pyOpenSSL DTLS; fall back to binary subprocess."""
    try:
        backend = _PyOpenSSLBackend(bridge_ip, username, clientkey)
        backend.connect()
        return backend
    except Exception as exc:
        _LOGGER.debug("pyOpenSSL DTLS unavailable (%s), trying binary", exc)

    backend = _OpenSSLBinaryBackend(bridge_ip, username, clientkey)
    backend.connect()
    return backend


# ---------------------------------------------------------------------------
# Stream manager
# ---------------------------------------------------------------------------

class EntertainmentGroupStream:
    """One DTLS stream per entertainment group.

    DTLS runs in a background thread; effects run as asyncio tasks.
    Frames are passed via a small queue.
    """

    def __init__(
        self,
        bridge_ip: str,
        username: str,
        clientkey: str,
        group_id: str,
        light_ids: list[int],
        on_state_change: Callable[[], None] | None = None,
    ) -> None:
        self._bridge_ip = bridge_ip
        self._username = username
        self._clientkey = clientkey
        self._group_id = group_id
        self._lights = light_ids
        self._on_state_change = on_state_change

        self._backend = None
        self._stream_thread: threading.Thread | None = None
        self._frame_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=8)
        self._stop_event = threading.Event()
        self._ready_event: asyncio.Event | None = None
        self._thread_error: Exception | None = None

        self._effect_task: asyncio.Task | None = None
        self._current_effect: str | None = None
        self._seq = 0

        # Live parameters — effects read these every frame so changes take effect immediately
        self._params: dict = {
            PARAM_STROBE_HZ:   DEFAULT_STROBE_HZ,
            PARAM_COLOR_SPEED: DEFAULT_CYCLE_SPEED,
            PARAM_PULSE_RATE:  DEFAULT_PULSE_RATE_HZ,
            PARAM_BRIGHTNESS:  DEFAULT_BRIGHTNESS,
            PARAM_COLOR:       (0xFFFF, 0xFFFF, 0xFFFF),
            PARAM_FLASH_COUNT: DEFAULT_FLASH_COUNT,
            PARAM_SEQUENCE:    None,
        }

        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def is_streaming(self) -> bool:
        return self._stream_thread is not None and self._stream_thread.is_alive()

    @property
    def current_effect(self) -> str | None:
        return self._current_effect

    def update_param(self, name: str, value) -> None:
        """Update a live parameter; running effects pick it up immediately."""
        self._params[name] = value

    async def async_start_effect(
        self,
        effect: str,
        *,
        color: tuple[int, int, int] | None = None,
        hz: float | None = None,
        flash_count: int | None = None,
        pulse_rate: float | None = None,
        cycle_speed: float | None = None,
    ) -> None:
        # Merge any explicitly passed values into params
        if color is not None:
            self._params[PARAM_COLOR] = color
        if hz is not None:
            self._params[PARAM_STROBE_HZ] = hz
        if flash_count is not None:
            self._params[PARAM_FLASH_COUNT] = flash_count
        if pulse_rate is not None:
            self._params[PARAM_PULSE_RATE] = pulse_rate
        if cycle_speed is not None:
            self._params[PARAM_COLOR_SPEED] = cycle_speed

        if not self.is_streaming:
            await self._connect()

        await self._cancel_effect()
        self._current_effect = effect
        self._effect_task = asyncio.ensure_future(self._effect_coro(effect))
        self._effect_task.add_done_callback(self._on_effect_done)
        self._notify()

    async def async_stop(self) -> None:
        await self._cancel_effect()
        self._current_effect = None
        self._disconnect()
        self._notify()

    # ------------------------------------------------------------------
    # Connection (thread-based)
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        await self._api("PUT", f"/groups/{self._group_id}", {"stream": {"active": True}})

        loop = asyncio.get_running_loop()
        self._ready_event = asyncio.Event()
        self._thread_error = None
        self._stop_event.clear()
        # Drain any stale frames
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except Exception:
                break

        self._stream_thread = threading.Thread(
            target=self._thread_main, args=(loop,), daemon=True, name="hue_dtls"
        )
        self._stream_thread.start()

        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=6.0)
        except asyncio.TimeoutError:
            self._stop_event.set()
            raise RuntimeError("DTLS connection timed out")

        if self._thread_error:
            raise self._thread_error

    def _thread_main(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            backend = _make_backend(self._bridge_ip, self._username, self._clientkey)
            self._backend = backend
            loop.call_soon_threadsafe(self._ready_event.set)

            while not self._stop_event.is_set():
                try:
                    frame = self._frame_queue.get(timeout=8.0)
                    if frame is None:
                        break
                    backend.write(frame)
                except queue.Empty:
                    # Send a keepalive — bridge closes after 10s silence
                    if self._lights:
                        frame = _build_frame([(lid, 0, 0, 0) for lid in self._lights], seq=self._seq)
                        self._seq = (self._seq + 1) & 0xFF
                        try:
                            backend.write(frame)
                        except Exception:
                            break

        except Exception as exc:
            _LOGGER.error("DTLS stream error: %s", exc)
            self._thread_error = exc
            loop.call_soon_threadsafe(self._ready_event.set)

        finally:
            if self._backend:
                self._backend.close()
                self._backend = None
            try:
                loop.call_soon_threadsafe(
                    asyncio.ensure_future,
                    self._api("PUT", f"/groups/{self._group_id}", {"stream": {"active": False}}),
                )
            except Exception:
                pass

    def _disconnect(self) -> None:
        self._stop_event.set()
        try:
            self._frame_queue.put_nowait(None)  # wake the thread
        except Exception:
            pass
        self._stream_thread = None

    # ------------------------------------------------------------------
    # Frame sending
    # ------------------------------------------------------------------

    def _enqueue_frame(self, lights: list[tuple[int, int, int, int]]) -> None:
        frame = _build_frame(lights, seq=self._seq)
        self._seq = (self._seq + 1) & 0xFF
        # Drop oldest if full to keep latency low
        if self._frame_queue.full():
            try:
                self._frame_queue.get_nowait()
            except Exception:
                pass
        try:
            self._frame_queue.put_nowait(frame)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Effects — all read self._params each frame for live updates
    # ------------------------------------------------------------------

    _EFFECT_DISPATCH = None  # set after class definition

    def _effect_coro(self, effect: str):
        fn = self._EFFECT_DISPATCH.get(effect, EntertainmentGroupStream._fx_static)
        return fn(self)

    async def _fx_strobe(self):
        phase = 0
        loop = asyncio.get_running_loop()
        while True:
            hz = max(0.5, self._params[PARAM_STROBE_HZ])
            color = self._params[PARAM_COLOR]
            t0 = loop.time()
            v = color if phase == 0 else (0, 0, 0)
            self._enqueue_frame([(lid, *v) for lid in self._lights])
            phase ^= 1
            await asyncio.sleep(max(0, 1 / hz - (loop.time() - t0)))

    async def _fx_flash(self):
        color = self._params[PARAM_COLOR]
        count = self._params[PARAM_FLASH_COUNT]
        for _ in range(count):
            self._enqueue_frame([(lid, *color) for lid in self._lights])
            await asyncio.sleep(0.15)
            self._enqueue_frame([(lid, 0, 0, 0) for lid in self._lights])
            await asyncio.sleep(0.15)

    async def _fx_pulse(self):
        interval = 1 / 30
        t = 0.0
        while True:
            color = self._params[PARAM_COLOR]
            rate  = self._params[PARAM_PULSE_RATE]
            b = 1.0 if rate == 0 else (math.sin(2 * math.pi * rate * t) + 1) / 2
            self._enqueue_frame(
                [(lid, int(color[0] * b), int(color[1] * b), int(color[2] * b))
                 for lid in self._lights]
            )
            t += interval
            await asyncio.sleep(interval)

    async def _fx_color_cycle(self):
        interval = 1 / 25
        t = 0.0
        n = max(1, len(self._lights))
        while True:
            speed = self._params[PARAM_COLOR_SPEED]
            bri   = self._params[PARAM_BRIGHTNESS]
            frame = []
            for i, lid in enumerate(self._lights):
                hue = (t * speed + i / n) % 1.0
                r, g, b = _hsv_to_rgb(hue, 1.0, 1.0)
                frame.append((lid, int(r * 0xFFFF * bri), int(g * 0xFFFF * bri), int(b * 0xFFFF * bri)))
            self._enqueue_frame(frame)
            t += interval
            await asyncio.sleep(interval)

    async def _fx_theater(self):
        """2-axis: color rotation speed + brightness pulse rate.

        color_speed = 0  →  static colour from PARAM_COLOR (set via rgb_color on turn_on)
        pulse_rate   = 0  →  steady brightness, no pulsing
        """
        interval = 1 / 25
        t = 0.0
        n = max(1, len(self._lights))
        while True:
            color_speed = self._params[PARAM_COLOR_SPEED]
            pulse_rate  = self._params[PARAM_PULSE_RATE]
            bri_base    = self._params[PARAM_BRIGHTNESS]

            bri = bri_base if pulse_rate == 0 else \
                  bri_base * (math.sin(2 * math.pi * pulse_rate * t) + 1) / 2

            frame = []
            for i, lid in enumerate(self._lights):
                if color_speed > 0:
                    hue = (t * color_speed + i / n) % 1.0
                    r, g, b = _hsv_to_rgb(hue, 1.0, 1.0)
                else:
                    c = self._params[PARAM_COLOR]
                    r, g, b = c[0] / 0xFFFF, c[1] / 0xFFFF, c[2] / 0xFFFF
                frame.append((lid, int(r * 0xFFFF * bri), int(g * 0xFFFF * bri), int(b * 0xFFFF * bri)))
            self._enqueue_frame(frame)
            t += interval
            await asyncio.sleep(interval)

    async def _fx_candle(self):
        while True:
            bri_base = self._params[PARAM_BRIGHTNESS]
            frame = []
            for lid in self._lights:
                bri = bri_base * random.uniform(0.3, 1.0)
                frame.append((
                    lid,
                    int(0xFFFF * bri),
                    int(0xFFFF * bri * random.uniform(0.25, 0.45)),
                    int(0xFFFF * bri * random.uniform(0.0, 0.08)),
                ))
            self._enqueue_frame(frame)
            await asyncio.sleep(random.uniform(0.04, 0.11))

    async def _fx_police(self):
        phase = 0
        while True:
            frame = [(lid, 0xFFFF, 0, 0) for lid in self._lights] if phase == 0 \
                else [(lid, 0, 0, 0xFFFF) for lid in self._lights]
            self._enqueue_frame(frame)
            phase ^= 1
            await asyncio.sleep(0.125)

    async def _fx_confetti(self):
        while True:
            self._enqueue_frame([
                (lid, random.randint(0, 0xFFFF), random.randint(0, 0xFFFF), random.randint(0, 0xFFFF))
                for lid in self._lights
            ])
            await asyncio.sleep(1 / 25)

    def _state_at(self, keyframes: list, t: float) -> dict:
        """Interpolate a single {r,g,b,brightness} state at time t. Keyframes must be pre-sorted by t."""
        kfs = keyframes
        if t <= kfs[0]["t"]:
            state = kfs[0]
        elif t >= kfs[-1]["t"]:
            state = kfs[-1]
        else:
            state = kfs[-1]
            for i in range(len(kfs) - 1):
                if kfs[i]["t"] <= t <= kfs[i + 1]["t"]:
                    a = (t - kfs[i]["t"]) / (kfs[i + 1]["t"] - kfs[i]["t"])
                    k0, k1 = kfs[i], kfs[i + 1]
                    state = {
                        "r":          int(k0["r"]          + (k1["r"]          - k0["r"])          * a),
                        "g":          int(k0["g"]          + (k1["g"]          - k0["g"])          * a),
                        "b":          int(k0["b"]          + (k1["b"]          - k0["b"])          * a),
                        "brightness": k0["brightness"] + (k1["brightness"] - k0["brightness"]) * a,
                    }
                    break
        return state

    def _state_to_16bit(self, state: dict) -> tuple[int, int, int]:
        bri = state.get("brightness", 1.0)
        return (
            int(state["r"] / 255 * bri * 0xFFFF),
            int(state["g"] / 255 * bri * 0xFFFF),
            int(state["b"] / 255 * bri * 0xFFFF),
        )

    def _interpolate_keyframes(self, keyframes: list, t: float) -> list[tuple[int,int,int,int]]:
        """Linear interpolation between global keyframes → list of (lid, r16, g16, b16)."""
        if not keyframes:
            return [(lid, 0, 0, 0) for lid in self._lights]
        state = self._state_at(sorted(keyframes, key=lambda k: k["t"]), t)
        r16, g16, b16 = self._state_to_16bit(state)
        return [(lid, r16, g16, b16) for lid in self._lights]

    async def _fx_sequence(self):
        """Play a keyframe sequence with linear interpolation at 25 fps.

        Re-reads PARAM_SEQUENCE every frame so live edits take effect without
        restarting (timeline editor calls update_sequence while playing).

        Supports two payload shapes:
          • {keyframes, duration, loop}             — same colour for all lights
          • {lights: {"0": {keyframes}, ...}, ...}  — independent per-light tracks
        """
        seq = self._params.get(PARAM_SEQUENCE)
        if not seq:
            await self._fx_static()
            return

        interval = 1 / 25
        t = 0.0

        sorted_tracks: dict[int, list] = {}
        prev_seq = None

        while True:
            seq = self._params.get(PARAM_SEQUENCE)
            if not seq:
                break

            if seq is not prev_seq:
                prev_seq = seq
                per_light = seq.get("lights")
                if per_light:
                    sorted_tracks = {
                        i: sorted(
                            (per_light.get(str(i)) or per_light.get("0", {})).get("keyframes", []),
                            key=lambda k: k["t"],
                        )
                        for i in range(len(self._lights))
                    }

            duration = float(seq.get("duration", 1.0))
            loop     = seq.get("loop", True)
            t_mod    = (t % duration) if loop else min(t, duration)

            per_light = seq.get("lights")
            if per_light:
                frame = []
                for i, lid in enumerate(self._lights):
                    kfs = sorted_tracks.get(i, [])
                    if kfs:
                        state = self._state_at(kfs, t_mod)
                        r16, g16, b16 = self._state_to_16bit(state)
                    else:
                        r16, g16, b16 = 0, 0, 0
                    frame.append((lid, r16, g16, b16))
            else:
                kfs = seq.get("keyframes", [])
                if not kfs:
                    break
                frame = self._interpolate_keyframes(kfs, t_mod)

            self._enqueue_frame(frame)
            t += interval

            if not loop and t >= duration:
                break

            await asyncio.sleep(interval)

    async def _fx_static(self):
        while True:
            color = self._params[PARAM_COLOR]
            self._enqueue_frame([(lid, *color) for lid in self._lights])
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _cancel_effect(self) -> None:
        if self._effect_task and not self._effect_task.done():
            self._effect_task.cancel()
            try:
                await self._effect_task
            except (asyncio.CancelledError, Exception):
                pass
        self._effect_task = None

    def _on_effect_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is None:
            asyncio.ensure_future(self.async_stop())
        self._notify()

    def _notify(self) -> None:
        if self._on_state_change:
            self._on_state_change()

    async def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"https://{self._bridge_ip}/api/{self._username}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: json.loads(
                urllib.request.urlopen(req, context=self._ssl_ctx, timeout=5).read()
            ),
        )


EntertainmentGroupStream._EFFECT_DISPATCH = {
    EFFECT_STROBE:      EntertainmentGroupStream._fx_strobe,
    EFFECT_FLASH:       EntertainmentGroupStream._fx_flash,
    EFFECT_PULSE:       EntertainmentGroupStream._fx_pulse,
    EFFECT_COLOR_CYCLE: EntertainmentGroupStream._fx_color_cycle,
    EFFECT_THEATER:     EntertainmentGroupStream._fx_theater,
    EFFECT_CANDLE:      EntertainmentGroupStream._fx_candle,
    EFFECT_POLICE:      EntertainmentGroupStream._fx_police,
    EFFECT_CONFETTI:    EntertainmentGroupStream._fx_confetti,
    EFFECT_SEQUENCE:    EntertainmentGroupStream._fx_sequence,
}
