import subprocess

import sounddevice as sd

from . import audio as audio
from .logger import logger


def diagnose_audio_issues():
    """Diagnose common audio issues that might cause mic failures."""
    logger.info("Running audio diagnostics...", "🔍")

    try:
        # Check for processes using audio devices
        result = subprocess.run(
            ["lsof", "/dev/snd/*"], capture_output=True, text=True, timeout=5
        )
        if result.stdout:
            logger.warning("Processes using audio devices:")
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    logger.warning(f"  {line}")
        else:
            logger.info("No processes found using audio devices", "✅")
    except Exception as e:
        logger.warning(f"Could not check audio device usage: {e}")

    try:
        # Check ALSA status
        result = subprocess.run(
            ["cat", "/proc/asound/cards"], capture_output=True, text=True, timeout=3
        )
        if result.stdout:
            logger.info("ALSA sound cards:")
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    logger.info(f"  {line}")
    except Exception as e:
        logger.warning(f"Could not check ALSA cards: {e}")


class MicManager:
    def __init__(self):
        self.stream = None

    def start(self, callback):
        self.stop()
        try:
            self.stream = sd.InputStream(
                samplerate=audio.MIC_RATE,
                device=audio.MIC_DEVICE_INDEX,
                channels=audio.MIC_CHANNELS,
                dtype='int16',
                blocksize=audio.CHUNK_SIZE,
                # Some USB devices report only 40 ms even for 'high' latency.
                # Allow scheduling gaps without changing the 40 ms DSP blocks.
                # AEC below uses the actual latency negotiated by the driver.
                latency=0.2,
                callback=callback,
            )
            self.stream.start()
            audio.set_aec_capture_latency(getattr(self.stream, "latency", 0.0))
        except Exception as exc:
            # Selection and rate validation happen before startup. Falling back
            # to an unrelated default device can capture the wrong microphone.
            self.stop()
            logger.error(
                f"Could not open selected microphone {audio.MIC_DEVICE_INDEX}: {exc}"
            )
            raise

    def stop(self):
        stream, self.stream = self.stream, None
        if stream is None:
            return
        try:
            try:
                stream.abort()
            except Exception:
                stream.stop()
        except Exception as exc:
            logger.warning(f"Error stopping mic stream: {exc}")
        finally:
            try:
                stream.close()
            except Exception as exc:
                logger.warning(f"Error closing mic stream: {exc}")
