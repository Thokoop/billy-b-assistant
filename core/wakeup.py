import asyncio
import os
import re
import wave
from typing import Optional

from .config import CUSTOM_INSTRUCTIONS
from .realtime_ai_provider import voice_provider_registry


WAKEUP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../sounds/wake-up/custom")
)
os.makedirs(WAKEUP_DIR, exist_ok=True)


def get_persona_wakeup_dir(persona_name: str) -> str:
    """Get the wake-up directory for a specific persona."""
    persona_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../personas", persona_name)
    )
    wakeup_dir = os.path.join(persona_dir, "wakeup")
    os.makedirs(wakeup_dir, exist_ok=True)
    return wakeup_dir


def slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", text).strip("_").lower()


def get_wakeup_path(phrase: str) -> str:
    return os.path.join(WAKEUP_DIR, f"{slugify(phrase)}.wav")


class WakeupClipGenerator:
    def __init__(self, *, voice: Optional[str] = None, persona_name: str = "default"):
        self.persona_name = persona_name

        # Get voice from persona if not specified
        if voice:
            self.voice = voice
        else:
            try:
                from .persona_manager import persona_manager

                self.voice = persona_manager.get_persona_voice(persona_name)
            except Exception:
                self.voice = "ballad"  # Default voice

    def _mood_delivery_hint(self, moods) -> str:
        normalized = [
            str(mood).strip().lower() for mood in (moods or []) if str(mood).strip()
        ]
        if not normalized:
            return ""

        style_hints = {
            "neutral": "even, natural, and brief",
            "calm": "relaxed, steady, and gentle",
            "cheerful": "brighter and warmer, without becoming sing-song",
            "warm": "friendly, affectionate, and supportive",
            "curious": "alert, questioning, and slightly lifted",
            "focused": "clear, direct, and ready",
            "playful": "light, cheeky, and amused",
            "mischievous": "sly, teasing, and amused",
            "excited": "higher energy and eager, without rushing the words",
            "surprised": "briefly caught off guard, with a quick lift in energy",
            "sleepy": "lower energy, softer, and a little slower",
            "bored": "flat, low-effort, and unimpressed",
            "sad": "subdued, softer, and a little heavy",
            "anxious": "tense, uncertain, and slightly quicker",
            "flustered": "slightly hurried and thrown off",
            "annoyed": "dry, clipped, and mildly irritated",
            "grumpy": "rougher, muttered, and reluctantly engaged",
            "dramatic": "more theatrical and expressive, but still short",
        }
        hints = [style_hints[mood] for mood in normalized if mood in style_hints]
        if not hints:
            return ""
        return (
            "Mood tags for this activation cue: "
            + ", ".join(normalized)
            + ". Delivery should feel "
            + "; ".join(hints[:3])
            + "."
        )

    async def generate(self, prompt: str, index: int, moods=None) -> str:
        # Use appropriate directory based on persona
        if self.persona_name == "default":
            # For default persona, use the custom directory
            path = os.path.join(WAKEUP_DIR, f"{index}.wav")
        else:
            # For other personas, use persona-specific directory
            persona_wakeup_dir = get_persona_wakeup_dir(self.persona_name)
            path = os.path.join(persona_wakeup_dir, f"{index}.wav")

        provider = voice_provider_registry.get_provider()

        print(f"🔊 Generating wakeup clip for: {prompt} → {index}")

        # Get current persona instructions
        try:
            from .persona_manager import persona_manager

            persona_instructions = persona_manager.get_persona_instructions(
                self.persona_name
            )
        except Exception:
            persona_instructions = CUSTOM_INSTRUCTIONS

        mood_hint = self._mood_delivery_hint(moods)
        instructions = (
            "Keep the persona's voice, accent, pace, and attitude, but do not change "
            "the wake-up line's words. This is a short recorded activation sound, "
            "not a conversation."
            + (f" {mood_hint}" if mood_hint else "")
            + "\n\n"
            + persona_instructions
        )

        audio_bytes = await provider.generate_audio_clip(
            prompt=prompt,
            voice=self.voice,
            instructions=instructions,
            strict_literal=True,
        )

        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(audio_bytes)

        print(f"✅ Saved wakeup clip: {path}")
        return path


def generate_wake_clip_async(prompt, index, persona_name="default", moods=None):
    async def _run():
        gen = WakeupClipGenerator(persona_name=persona_name)
        return await gen.generate(prompt, index, moods=moods)

    return asyncio.run(_run())
