"""
Camera capture and vision description for Billy's look_around feature.

On Mac/dev: uses OpenCV to grab a frame from the built-in or USB camera.
On Pi: tries picamera2 first, falls back to OpenCV for USB webcam.
"""

import base64
import platform
import sys

from .logger import logger


def capture_image_base64(device_index: int = 0) -> str | None:
    """Capture a single frame and return it as a base64-encoded JPEG string."""
    on_pi = platform.system() == "Linux"

    if on_pi:
        image_b64 = _capture_picamera2()
        if image_b64:
            return image_b64
        logger.info("picamera2 not available, falling back to OpenCV", "📷")

    return _capture_opencv(device_index)


def _capture_picamera2() -> str | None:
    try:
        import io
        import time

        from picamera2 import Picamera2

        picam = Picamera2()
        config = picam.create_still_configuration()
        picam.configure(config)
        picam.start()
        time.sleep(0.5)  # warmup
        buf = io.BytesIO()
        picam.capture_file(buf, format="jpeg")
        picam.stop()
        picam.close()
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.warning(f"picamera2 capture failed: {e}", "📷")
        return None


def _capture_opencv(device_index: int) -> str | None:
    try:
        import time

        import cv2

        cap = cv2.VideoCapture(device_index)
        if not cap.isOpened():
            logger.warning(f"Could not open camera device {device_index}", "📷")
            return None
        # Discard a few frames to let the camera warm up (exposure/white-balance)
        for _ in range(5):
            cap.read()
        ret, frame = cap.read()
        cap.release()
        if not ret:
            logger.warning("Camera read failed", "📷")
            return None
        _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return base64.b64encode(buffer.tobytes()).decode("utf-8")
    except ImportError:
        logger.warning("opencv-python not installed — run: pip install opencv-python", "📷")
        return None
    except Exception as e:
        logger.warning(f"OpenCV capture failed: {e}", "📷")
        return None


async def describe_image(image_b64: str, api_key: str) -> str:
    """Send a base64 image to GPT-4o-mini vision and return a scene description."""
    import aiohttp

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Describe what you see in this image briefly and naturally, "
                            "as if you just looked around the room. Focus on people, "
                            "objects, and the overall scene. Be concise (2-4 sentences)."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}",
                            "detail": "low",
                        },
                    },
                ],
            }
        ],
        "max_tokens": 200,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.warning(f"Vision API error {resp.status}: {text}", "📷")
                return "I couldn't make out what was in front of me."
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()
