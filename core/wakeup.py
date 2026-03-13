import asyncio
import os
import re
import wave

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
    def __init__(self, *, voice: str | None = None, persona_name: str = "default"):
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

    async def generate(self, prompt: str, index: int) -> str:
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

        instructions = (
            "IMPORTANT: Always respond by speaking the exact user text out loud. Do not add, change or rephrase anything!\n\n"
            + persona_instructions
        )

        audio_bytes = await provider.generate_audio_clip(
            prompt=prompt,
            voice=self.voice,
            instructions=instructions,
        )

        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(audio_bytes)

        print(f"✅ Saved wakeup clip: {path}")
        return path


def generate_wake_clip_async(prompt, index, persona_name="default"):
    async def _run():
        gen = WakeupClipGenerator(persona_name=persona_name)
        return await gen.generate(prompt, index)

    return asyncio.run(_run())
