"""Local wake-word detection and backends, registered explicitly at startup."""

import os
import re


def wake_word_phrase(settings=None) -> str:
    """The configured wake word as words: "hey-billy.onnx" becomes "Hey Billy".

    Home Assistant gets it with a run Billy's own wake word started; its voice
    setup wizard waits for exactly that to finish its wake-word step.
    """
    if settings is None:
        from .. import config as settings
    if settings.WAKE_WORD_BACKEND == "openwakeword":
        path = settings.WAKE_WORD_OPENWAKEWORD_MODEL_PATH
    else:
        path = settings.WAKE_WORD_PORCUPINE_KEYWORD_PATH
    stem = os.path.basename(str(path or "")).rsplit(".", 1)[0]
    # Picovoice names add "_en_<platform>_v3_0_0"; openWakeWord adds "_v0.1".
    stem = re.split(r"_[a-z]{2}_", stem, maxsplit=1)[0]
    stem = re.sub(r"[_-]v\d[\w.]*$", "", stem)
    words = re.sub(r"[\s_-]+", " ", stem).strip()
    return words.title() if words else "Wake word"
