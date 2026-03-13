"""
Persona management system for Billy Bass Assistant.
Handles loading and switching between different personality configurations.
"""

import configparser
from pathlib import Path
from typing import Any

from .logger import logger
from .realtime_ai_provider import voice_provider_registry


class PersonaManager:
    """Manages different Billy personas and personality configurations."""

    def __init__(self):
        self.personas_dir = Path("personas")
        self.personas_dir.mkdir(exist_ok=True)
        self.persona_presets_dir = Path("persona_presets")
        self.current_persona = "default"  # Default persona
        self._persona_cache: dict[str, dict[str, Any]] = {}

    def get_available_personas(self) -> list[dict]:
        """Get list of available persona files with their metadata."""
        personas = []

        # Add default persona.ini if it exists
        default_persona_file = Path("persona.ini")
        if default_persona_file.exists():
            try:
                config = configparser.ConfigParser()
                config.read(default_persona_file)
                personas.append({
                    "name": "default",
                    "description": "Default",
                })
            except Exception as e:
                logger.warning(f"Failed to load default persona: {e}")

        # Add custom personas from personas directory (both old format and new folder format)
        if self.personas_dir.exists():
            # Check for old format: personas/*.ini
            for file_path in self.personas_dir.glob("*.ini"):
                persona_name = file_path.stem
                persona_data = self.load_persona(persona_name)
                if persona_data:
                    personas.append({
                        "name": persona_name,
                        "description": persona_data.get("meta", {}).get(
                            "description", persona_name
                        ),
                    })

            # Check for new format: personas/*/persona.ini
            for folder_path in self.personas_dir.iterdir():
                if folder_path.is_dir():
                    persona_file = folder_path / "persona.ini"
                    if persona_file.exists():
                        persona_name = folder_path.name
                        persona_data = self.load_persona(persona_name)
                        if persona_data:
                            personas.append({
                                "name": persona_name,
                                "description": persona_data.get("meta", {}).get(
                                    "description", persona_name
                                ),
                            })

        return (
            sorted(personas, key=lambda x: x["name"])
            if personas
            else [{"name": "default", "description": "Default"}]
        )

    def load_persona(self, persona_name: str) -> dict[str, Any] | None:
        """Load a persona configuration from file."""
        if persona_name in self._persona_cache:
            return self._persona_cache[persona_name]

        # Handle default persona
        if persona_name == "default":
            persona_file = Path("persona.ini")
        else:
            # Check new folder structure first: personas/persona_name/persona.ini
            persona_file = self.personas_dir / persona_name / "persona.ini"
            if not persona_file.exists():
                # Fall back to old structure: personas/persona_name.ini
                persona_file = self.personas_dir / f"{persona_name}.ini"

        if not persona_file.exists():
            logger.warning(f"Persona file not found: {persona_file}")
            return None

        try:
            config = configparser.ConfigParser()
            config.read(persona_file)

            persona_data = {
                "name": persona_name,
                "personality": dict(config.items("PERSONALITY"))
                if config.has_section("PERSONALITY")
                else {},
                "backstory": dict(config.items("BACKSTORY"))
                if config.has_section("BACKSTORY")
                else {},
                "meta": dict(config.items("META"))
                if config.has_section("META")
                else {},
            }

            # Migrate and convert personality values to integers
            from .persona import migrate_traits

            persona_data["personality"] = migrate_traits(persona_data["personality"])

            self._persona_cache[persona_name] = persona_data
            logger.info(f"Loaded persona: {persona_name}", "🎭")
            return persona_data

        except Exception as e:
            logger.error(f"Failed to load persona {persona_name}: {e}")
            return None

    def get_persona_instructions(self, persona_name: str) -> str:
        """Get formatted instructions for a specific persona."""
        persona_data = self.load_persona(persona_name)
        if not persona_data:
            return ""

        # Get persona-specific instructions from META section
        persona_specific_instructions = persona_data['meta'].get('instructions', '')

        # If this persona has specific instructions, use them instead of the default format
        if persona_specific_instructions and persona_name != 'default':
            return persona_specific_instructions

        # For default persona or personas without specific instructions, use compact format
        instructions = f"[PERSONA: {persona_data['meta'].get('description', persona_name)} | MOOD: {persona_data['meta'].get('mood', 'neutral')} | ENERGY: {persona_data['meta'].get('energy', 'medium')}]\n"

        # Format backstory as key-value pairs
        if persona_data['backstory']:
            backstory_parts = []
            for key, value in persona_data['backstory'].items():
                backstory_parts.append(f"{key}: {value}")
            instructions += f"Backstory: {'; '.join(backstory_parts)}\n"

        # Compact personality traits
        if persona_data["personality"]:
            traits = [
                f"{trait}:{value}"
                for trait, value in persona_data["personality"].items()
            ]
            instructions += f"Traits: {', '.join(traits)}\n"

        return instructions

    def switch_persona(self, persona_name: str) -> bool:
        """Switch to a different persona."""
        available_personas = [p["name"] for p in self.get_available_personas()]
        if persona_name not in available_personas:
            logger.warning(f"Persona not available: {persona_name}")
            return False

        # Force reload from disk in case persona.ini was edited during runtime.
        self.clear_persona_cache(persona_name)
        self.current_persona = persona_name
        logger.info(f"Switched to persona: {persona_name}", "🎭")
        return True

    def get_current_persona_data(self) -> dict[str, Any] | None:
        """Get data for the current persona."""
        return self.load_persona(self.current_persona)

    def get_current_persona_instructions(self) -> str:
        """Get instructions for the current persona."""
        return self.get_persona_instructions(self.current_persona)

    def get_persona_voice(self, persona_name: str) -> str:
        """Get the voice setting for a specific persona."""
        persona_data = self.load_persona(persona_name)
        try:
            provider = voice_provider_registry.get_provider()
            default_voice = provider.default_voice
            supported_voices = set(provider.get_supported_voices())
        except Exception:
            default_voice = "ballad"
            supported_voices = {"ballad"}

        if not persona_data:
            return default_voice

        raw_voice = str(persona_data.get("meta", {}).get("voice", "")).strip().lower()
        if not raw_voice:
            return default_voice

        if raw_voice not in supported_voices:
            logger.warning(
                f"Persona '{persona_name}' has unsupported voice '{raw_voice}'. "
                f"Using provider default '{default_voice}'."
            )
            return default_voice

        return raw_voice

    def get_current_persona_voice(self) -> str:
        """Get the voice setting for the current persona."""
        return self.get_persona_voice(self.current_persona)

    def clear_persona_cache(self, persona_name: str = None) -> None:
        """Clear the cache for a specific persona or all personas."""
        if persona_name:
            self._persona_cache.pop(persona_name, None)
            logger.info(f"Cleared cache for persona: {persona_name}", "🎭")
        else:
            self._persona_cache.clear()
            logger.info("Cleared all persona cache", "🎭")

    def get_persona_presets(self) -> list[dict]:
        """Get list of available persona preset templates."""
        presets = []

        if not self.persona_presets_dir.exists():
            return presets

        # Check for presets in persona_presets/*/persona.ini
        for folder_path in self.persona_presets_dir.iterdir():
            if folder_path.is_dir():
                preset_file = folder_path / "persona.ini"
                if preset_file.exists():
                    preset_name = folder_path.name
                    try:
                        config = configparser.ConfigParser()
                        config.read(preset_file)

                        # Get name and description from META section
                        name = config.get("META", "name", fallback=preset_name.title())
                        description = config.get("META", "description", fallback="")

                        presets.append({
                            "id": preset_name,
                            "name": name,
                            "description": description,
                        })
                    except Exception as e:
                        logger.warning(f"Failed to load preset {preset_name}: {e}")

        return sorted(presets, key=lambda x: x["name"])

    def create_persona_from_preset(
        self, preset_id: str, new_persona_name: str, display_name: str = None
    ) -> bool:
        """Create a new persona from a persona preset template."""
        try:
            # Load the preset
            preset_file = self.persona_presets_dir / preset_id / "persona.ini"
            if not preset_file.exists():
                logger.error(f"Preset not found: {preset_id}")
                return False

            # Create the new persona directory
            new_persona_dir = self.personas_dir / new_persona_name
            new_persona_dir.mkdir(parents=True, exist_ok=True)

            # Copy the preset file to the new persona
            import shutil

            shutil.copy(preset_file, new_persona_dir / "persona.ini")

            # Update the name in the persona file if display_name is provided
            if display_name:
                new_persona_file = new_persona_dir / "persona.ini"
                config = configparser.ConfigParser()
                config.read(new_persona_file)

                if config.has_section('META'):
                    config.set('META', 'name', display_name)

                    with open(new_persona_file, 'w') as f:
                        config.write(f)

            # Clear cache for the new persona
            self.clear_persona_cache(new_persona_name)

            logger.info(
                f"Created persona '{new_persona_name}' from preset '{preset_id}'", "🎭"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to create persona from preset: {e}")
            return False


# Global persona manager instance
persona_manager = PersonaManager()
