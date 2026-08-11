"""Camera capture helper for Billy realtime vision function calls."""

from __future__ import annotations

import base64
import contextlib
import glob
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import (
    CAMERA_CAPTURE_HEIGHT,
    CAMERA_CAPTURE_TIMEOUT_SECONDS,
    CAMERA_CAPTURE_WIDTH,
    CAMERA_DEVICE_INDEX,
    CAMERA_HARDWARE,
    CAMERA_ROTATION,
    ENV_PATH,
    FFMPEG_BIN,
    LIBCAMERA_STILL_BIN,
    MOCKFISH,
)
from .logger import logger


@dataclass
class SceneDescriptionResult:
    ok: bool
    summary: str
    image_url: str = ""
    prompt: str = ""
    max_words: int = 80
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "summary": self.summary,
            "image_url": self.image_url,
            "prompt": self.prompt,
            "max_words": self.max_words,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def describe_scene(args: dict[str, Any]) -> dict[str, Any]:
    """Capture a fresh camera frame and return it as data URL for Realtime input."""
    # Re-read .env so camera changes from Web UI apply immediately without restart.
    load_dotenv(ENV_PATH, override=True)
    runtime = _get_runtime_camera_settings()

    prompt = (
        str(args.get("prompt") or "").strip()
        or "Describe exactly what you see in this image in 2 to 4 short sentences."
    )
    max_words = _coerce_int(args.get("max_words"), default=80, minimum=30, maximum=180)
    capture_timeout = _coerce_float(
        args.get("capture_timeout_seconds"),
        default=float(runtime["capture_timeout_seconds"]),
        minimum=2.0,
        maximum=15.0,
    )
    camera_hardware = (
        str(args.get("camera_hardware") or runtime["camera_hardware"]).strip().lower()
    )
    if camera_hardware not in {"none", "rpi_camera", "usb_webcam"}:
        camera_hardware = str(runtime["camera_hardware"])
    camera_device_index = _coerce_int(
        args.get("camera_device_index"),
        default=int(runtime["camera_device_index"]),
        minimum=0,
        maximum=20,
    )
    camera_rotation = _normalize_rotation_degrees(
        args.get("camera_rotation"),
        default=int(runtime["camera_rotation"]),
    )
    usb_scan_fallback = _coerce_bool(args.get("usb_scan_fallback"), default=True)
    fresh_frame = _coerce_bool(args.get("fresh_frame"), default=False)

    if MOCKFISH:
        return SceneDescriptionResult(
            ok=False,
            summary="Camera simulation active.",
            error=(
                "MOCKFISH is enabled, so no real camera frame is available. "
                "Disable MOCKFISH on Raspberry Pi hardware to use live scene descriptions."
            ),
            prompt=prompt,
            max_words=max_words,
        ).to_dict()

    if camera_hardware == "none":
        return SceneDescriptionResult(
            ok=False,
            summary="Camera is disabled.",
            error="Camera hardware is set to none. Select a camera in Hardware Settings.",
            prompt=prompt,
            max_words=max_words,
        ).to_dict()

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        image_path = tmp.name

    try:
        capture_error = _capture_image(
            image_path=image_path,
            timeout_seconds=capture_timeout,
            camera_hardware=camera_hardware,
            camera_device_index=camera_device_index,
            capture_width=int(runtime["capture_width"]),
            capture_height=int(runtime["capture_height"]),
            libcamera_still_bin=str(runtime["libcamera_still_bin"]),
            ffmpeg_bin=str(runtime["ffmpeg_bin"]),
            usb_scan_fallback=usb_scan_fallback,
            fresh_frame=fresh_frame,
        )
        if capture_error:
            return SceneDescriptionResult(
                ok=False,
                summary="I couldn't capture an image from the camera.",
                error=capture_error,
                prompt=prompt,
                max_words=max_words,
            ).to_dict()

        rotation_error = _apply_image_rotation(
            image_path=image_path,
            rotation_degrees=camera_rotation,
        )
        if rotation_error:
            return SceneDescriptionResult(
                ok=False,
                summary="I captured an image, but couldn't rotate it.",
                error=rotation_error,
                prompt=prompt,
                max_words=max_words,
            ).to_dict()

        image_url = _read_as_data_url(image_path)

        return SceneDescriptionResult(
            ok=True,
            summary="Scene captured.",
            image_url=image_url,
            prompt=prompt,
            max_words=max_words,
        ).to_dict()
    finally:
        with contextlib.suppress(OSError):
            os.remove(image_path)


def _capture_image(
    *,
    image_path: str,
    timeout_seconds: float,
    camera_hardware: str,
    camera_device_index: int,
    capture_width: int,
    capture_height: int,
    libcamera_still_bin: str,
    ffmpeg_bin: str,
    usb_scan_fallback: bool,
    fresh_frame: bool,
) -> str | None:
    if camera_hardware == "usb_webcam":
        return _capture_usb_webcam_image(
            image_path=image_path,
            timeout_seconds=timeout_seconds,
            camera_device_index=camera_device_index,
            capture_width=capture_width,
            capture_height=capture_height,
            ffmpeg_bin=ffmpeg_bin,
            usb_scan_fallback=usb_scan_fallback,
            fresh_frame=fresh_frame,
        )

    return _capture_rpi_camera_image(
        image_path=image_path,
        timeout_seconds=timeout_seconds,
        camera_device_index=camera_device_index,
        capture_width=capture_width,
        capture_height=capture_height,
        libcamera_still_bin=libcamera_still_bin,
    )


def _capture_rpi_camera_image(
    image_path: str,
    timeout_seconds: float,
    camera_device_index: int,
    capture_width: int,
    capture_height: int,
    libcamera_still_bin: str,
) -> str | None:
    attempt_errors: list[str] = []
    for camera_bin in _rpi_camera_binary_candidates(libcamera_still_bin):
        camera_cmd = [
            camera_bin,
            "--nopreview",
            "--immediate",
            "--timeout",
            "1",
            "--camera",
            str(camera_device_index),
            "--width",
            str(capture_width),
            "--height",
            str(capture_height),
            "--encoding",
            "jpg",
            "--quality",
            "90",
            "-o",
            image_path,
        ]
        try:
            completed = subprocess.run(
                camera_cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError:
            attempt_errors.append(f"'{camera_bin}' was not found")
            continue
        except subprocess.TimeoutExpired:
            attempt_errors.append(f"'{camera_bin}' timed out")
            continue
        except Exception as exc:
            attempt_errors.append(f"'{camera_bin}' failed: {exc}")
            continue

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            details = stderr or stdout or f"exit code {completed.returncode}"
            attempt_errors.append(f"'{camera_bin}': {details}")
            continue

        if not os.path.exists(image_path) or os.path.getsize(image_path) == 0:
            attempt_errors.append(f"'{camera_bin}' returned no image data")
            continue

        return None

    details = "; ".join(attempt_errors)
    return (
        "Raspberry Pi camera capture failed. "
        f"Tried the configured and compatible camera commands: {details}. "
        "Install rpicam-apps (or libcamera-apps on older Raspberry Pi OS releases) "
        "and verify camera access."
    )


def _rpi_camera_binary_candidates(configured_bin: str) -> list[str]:
    """Return the configured capture command plus old and new Raspberry Pi names."""
    candidates: list[str] = []
    for candidate in (configured_bin.strip(), "rpicam-still", "libcamera-still"):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def detect_rpi_camera_available(
    configured_bin: str, *, timeout_seconds: float = 3.0
) -> bool:
    """Detect a CSI camera using either current or legacy Raspberry Pi tools."""
    for camera_bin in _rpi_camera_binary_candidates(configured_bin):
        try:
            result = subprocess.run(
                [camera_bin, "--list-cameras"],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        except Exception:
            continue

        combined = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        if "no cameras available" in combined:
            continue
        # A detected camera entry starts with a numeric index, e.g. "0 : imx708".
        if "available cameras" in combined and any(
            line.strip().partition(":")[0].strip().isdigit()
            for line in combined.splitlines()
        ):
            return True
        # Compatibility with builds that omit the "Available cameras" header.
        if "/base/" in combined or "platform:" in combined:
            return True
    return False


def _capture_usb_webcam_image(
    image_path: str,
    timeout_seconds: float,
    camera_device_index: int,
    capture_width: int,
    capture_height: int,
    ffmpeg_bin: str,
    usb_scan_fallback: bool,
    fresh_frame: bool,
) -> str | None:
    configured_device = f"/dev/video{camera_device_index}"
    device_paths = _build_usb_device_candidates(
        configured_device=configured_device,
        include_fallback=usb_scan_fallback,
    )
    if not device_paths:
        return (
            "No V4L2 camera devices found. "
            "Plug in a webcam and check that /dev/video* exists."
        )

    attempt_errors: list[str] = []
    formats_to_try = [None, "mjpeg", "yuyv422"]
    per_attempt_timeout = max(1.0, min(float(timeout_seconds), 2.5))

    for device_path in device_paths:
        for input_format in formats_to_try:
            error = _try_usb_capture_command(
                image_path=image_path,
                device_path=device_path,
                input_format=input_format,
                timeout_seconds=per_attempt_timeout,
                capture_width=capture_width,
                capture_height=capture_height,
                ffmpeg_bin=ffmpeg_bin,
                fresh_frame=fresh_frame,
            )
            if error is None:
                if device_path != configured_device:
                    logger.warning(
                        f"USB camera capture succeeded on {device_path} (configured {configured_device}). "
                        "Consider setting CAMERA_DEVICE_INDEX accordingly."
                    )
                return None
            fmt_label = input_format or "auto"
            attempt_errors.append(f"{device_path} ({fmt_label}): {error}")

    available_devices = ", ".join(device_paths)
    concise_errors = "; ".join(attempt_errors[:3])
    return (
        "USB webcam capture failed on all candidate devices. "
        f"Tried: {available_devices}. "
        f"Sample errors: {concise_errors}. "
        "Set CAMERA_DEVICE_INDEX to the correct /dev/videoX node for your camera."
    )


def _build_usb_device_candidates(
    *, configured_device: str, include_fallback: bool
) -> list[str]:
    filtered = _list_usb_video_capture_nodes()
    if not filtered:
        return []

    if not include_fallback:
        return [configured_device] if configured_device in filtered else []

    ordered: list[str] = []
    if configured_device in filtered:
        ordered.append(configured_device)
    for path in filtered:
        if path not in ordered:
            ordered.append(path)
    return ordered


def _list_usb_video_capture_nodes() -> list[str]:
    candidates = sorted(glob.glob("/dev/video*"))
    results: list[str] = []
    for path in candidates:
        name = os.path.basename(path)
        suffix = name.removeprefix("video")
        if not suffix.isdigit():
            continue
        sys_video_dir = Path("/sys/class/video4linux") / name
        sys_name_path = sys_video_dir / "name"
        sys_device_path = sys_video_dir / "device"

        node_name = ""
        try:
            if sys_name_path.exists():
                node_name = sys_name_path.read_text(encoding="utf-8").strip().lower()
        except Exception:
            node_name = ""

        try:
            real_device_path = os.path.realpath(str(sys_device_path)).lower()
        except Exception:
            real_device_path = ""
        if "/usb" not in real_device_path:
            continue
        if "metadata" in node_name:
            continue
        # Prefer primary V4L2 nodes (index 0); skip common metadata/helper
        # sibling nodes (often index 1) that fail capture ioctls.
        index_path = sys_video_dir / "index"
        try:
            if (
                index_path.exists()
                and index_path.read_text(encoding="utf-8").strip() != "0"
            ):
                continue
        except Exception:
            pass
        results.append(path)
    return results


def _try_usb_capture_command(
    *,
    image_path: str,
    device_path: str,
    input_format: str | None,
    timeout_seconds: float,
    capture_width: int,
    capture_height: int,
    ffmpeg_bin: str,
    fresh_frame: bool,
) -> str | None:
    if fresh_frame:
        warmup_cmd = [
            ffmpeg_bin,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "video4linux2",
            "-framerate",
            "15",
        ]
        if input_format:
            warmup_cmd.extend(["-input_format", input_format])
        warmup_cmd.extend([
            "-i",
            device_path,
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ])
        with contextlib.suppress(Exception):
            subprocess.run(
                warmup_cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=max(0.8, min(float(timeout_seconds), 1.5)),
            )

    camera_cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "video4linux2",
        "-framerate",
        "15",
    ]
    if input_format:
        camera_cmd.extend(["-input_format", input_format])
    camera_cmd.extend([
        "-i",
        device_path,
        "-frames:v",
        "1",
        "-vf",
        f"scale={capture_width}:{capture_height}",
        "-q:v",
        "2",
        "-y",
        image_path,
    ])
    try:
        completed = subprocess.run(
            camera_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return f"'{ffmpeg_bin}' was not found. Install ffmpeg to use USB webcams."
    except subprocess.TimeoutExpired:
        return "capture timed out"
    except Exception as exc:
        return f"capture failed: {exc}"

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        return stderr or stdout or f"exit code {completed.returncode}"

    if not os.path.exists(image_path) or os.path.getsize(image_path) == 0:
        return "webcam returned no image data"

    return None


def _get_runtime_camera_settings() -> dict[str, Any]:
    camera_hardware = os.getenv("CAMERA_HARDWARE", CAMERA_HARDWARE).strip().lower()
    if camera_hardware not in {"none", "rpi_camera", "usb_webcam"}:
        camera_hardware = CAMERA_HARDWARE

    return {
        "camera_hardware": camera_hardware,
        "camera_device_index": _coerce_int(
            os.getenv("CAMERA_DEVICE_INDEX"),
            default=CAMERA_DEVICE_INDEX,
            minimum=0,
            maximum=20,
        ),
        "camera_rotation": _normalize_rotation_degrees(
            os.getenv("CAMERA_ROTATION"),
            default=CAMERA_ROTATION,
        ),
        "capture_width": _coerce_int(
            os.getenv("CAMERA_CAPTURE_WIDTH"),
            default=CAMERA_CAPTURE_WIDTH,
            minimum=320,
            maximum=3840,
        ),
        "capture_height": _coerce_int(
            os.getenv("CAMERA_CAPTURE_HEIGHT"),
            default=CAMERA_CAPTURE_HEIGHT,
            minimum=240,
            maximum=2160,
        ),
        "capture_timeout_seconds": _coerce_float(
            os.getenv("CAMERA_CAPTURE_TIMEOUT_SECONDS"),
            default=CAMERA_CAPTURE_TIMEOUT_SECONDS,
            minimum=2.0,
            maximum=20.0,
        ),
        "libcamera_still_bin": os.getenv(
            "LIBCAMERA_STILL_BIN", LIBCAMERA_STILL_BIN
        ).strip(),
        "ffmpeg_bin": os.getenv("FFMPEG_BIN", FFMPEG_BIN).strip(),
    }


def _read_as_data_url(image_path: str) -> str:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _apply_image_rotation(*, image_path: str, rotation_degrees: int) -> str | None:
    normalized = _normalize_rotation_degrees(rotation_degrees, default=0)
    if normalized == 0:
        return None

    try:
        from PIL import Image

        with Image.open(image_path) as image:
            rotated = image.rotate(-normalized, expand=True)
            rotated.save(image_path, format="JPEG", quality=90)
    except ImportError:
        return "Image rotation requires Pillow. Install dependencies and try again."
    except Exception as exc:
        return f"Image rotation failed: {exc}"

    return None


def _coerce_int(
    value: Any, *, default: int, minimum: int | None = None, maximum: int | None = None
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _coerce_float(
    value: Any,
    *,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_rotation_degrees(value: Any, *, default: int) -> int:
    parsed = _coerce_int(value, default=default)
    normalized = parsed % 360
    if normalized not in {0, 90, 180, 270}:
        return default if default in {0, 90, 180, 270} else 0
    return normalized
