"""
Instruction builder for generating AI prompts with user/persona context.
"""

from dataclasses import dataclass
from typing import Optional

from ..config import (
    INSTRUCTIONS,
    _filter_vision_instruction_line,
    get_tool_instructions,
)
from ..mood import mood_manager
from ..persona import PersonaProfile
from ..persona_manager import persona_manager


@dataclass
class InstructionContext:
    """Context for instruction generation."""

    mode: str  # "guest" or "user"
    persona_name: str
    user_profile: Optional[object] = None


class InstructionBuilder:
    """Builds AI instructions based on current context."""

    def __init__(self):
        self._cache = {}

    def build(self, context: InstructionContext) -> str:
        """Build instructions for given context."""
        if context.mode == "guest":
            return self._build_guest_instructions(context)
        return self._build_user_instructions(context)

    def _build_guest_instructions(self, context: InstructionContext) -> str:
        """Build instructions for guest mode."""
        persona_data = persona_manager.load_persona(context.persona_name)
        persona_instructions = persona_manager.get_persona_instructions(
            context.persona_name
        )

        if persona_data and persona_instructions:
            sections = [
                f"# Role & Objective\n{persona_instructions}",
                f"# Tools\n{get_tool_instructions().strip()}",
                self._build_personality_section(persona_data),
                self._build_backstory_section(persona_data),
                mood_manager.get_prompt_section(),
            ]
            return "\n---\n".join(filter(None, sections))

        # Fallback to default with guest mode modifications
        fallback_instructions = INSTRUCTIONS.replace(
            "USER RECOGNITION: ALWAYS call `identify_user` at conversation start. Greet users by name when known.",
            "GUEST MODE: You are in guest mode. Only call `identify_user` if someone explicitly introduces themselves with clear name patterns like 'I am [Name]', 'My name is [Name]', 'Hey billy it is [Name]', or 'This is [Name]'. Do NOT call `identify_user` for greetings like 'Hello', 'Hi', or casual conversation. Otherwise treat everyone as a guest visitor.",
        ).replace(
            "USER SYSTEM:\n- IDENTIFICATION: When you recognize a user's voice/name, call `identify_user` with name and confidence (high/medium/low). Respond with personalized greeting after.\n- MEMORY: Call `store_memory` when users share personal info. Categories: preference/fact/event/relationship/interest. Importance: high/medium/low.\n- PERSONA: Use `manage_profile` with action=\"switch_persona\" for different personalities.",
            "USER SYSTEM: Limited in guest mode - only `identify_user` available. After identification, ALWAYS call `store_memory` when users share personal info. Be proactive - don't wait for them to ask.\n\nMEMORY STORAGE TRIGGERS:\nCall `store_memory` for ANY of these patterns:\n- \"I like/love/enjoy/hate/dislike [something]\"\n- \"I have/own/possess [something]\"\n- \"I work as/at [something]\"\n- \"I live in/at [somewhere]\"\n- \"I am [something]\"\n- \"My favorite [something] is [something]\"\n- \"I prefer [something]\"\n- \"I'm interested in [something]\"\n- \"I'm from [somewhere]\"\n- \"I do [activity/hobby]\"\n\nCategories: preference/fact/event/relationship/interest\nImportance: high/medium/low (use \"high\" for explicitly important info)",
        )
        fallback_instructions += f"\n---\n{mood_manager.get_prompt_section()}"
        return _filter_vision_instruction_line(fallback_instructions)

    def _build_user_instructions(self, context: InstructionContext) -> str:
        """Build instructions for user mode."""
        user_profile = context.user_profile
        preferred_persona = user_profile.data['USER_INFO'].get(
            'preferred_persona', 'default'
        )
        persona_data = persona_manager.load_persona(preferred_persona)
        persona_instructions = persona_manager.get_persona_instructions(
            preferred_persona
        )

        if persona_data and persona_instructions:
            sections = [
                f"# Role & Objective\n{persona_instructions}",
                f"# Tools\n{get_tool_instructions().strip()}",
                self._build_personality_section(persona_data),
                self._build_backstory_section(persona_data),
                self._build_user_context_section(user_profile),
                mood_manager.get_prompt_section(),
            ]
            return "\n---\n".join(filter(None, sections))

        # Fallback
        user_context = user_profile.get_context_string() if user_profile else ""
        fallback = INSTRUCTIONS + (
            f"\n---\n# Current User Context\n{user_context}" if user_context else ""
        )
        fallback += f"\n---\n{mood_manager.get_prompt_section()}"
        return _filter_vision_instruction_line(fallback)

    def _build_personality_section(self, persona_data: dict) -> str:
        """Build personality traits section."""
        if not persona_data.get('personality'):
            return ""

        personality = PersonaProfile()
        for trait, value in persona_data['personality'].items():
            if hasattr(personality, trait):
                setattr(personality, trait, int(value))

        return f"# Personality & Tone\n{personality.generate_prompt()}"

    def _build_backstory_section(self, persona_data: dict) -> str:
        """Build backstory section."""
        backstory = persona_data.get('backstory', {})
        if not backstory:
            return ""

        backstory_lines = [f"- {k}: {v}" for k, v in backstory.items()]
        return (
            f"# Context (backstory)\nUse your backstory to inspire jokes, metaphors, or occasional references in conversation, staying consistent with your personality.\n"
            + "\n".join(backstory_lines)
        )

    def _build_user_context_section(self, user_profile) -> str:
        """Build user context section."""
        if not user_profile:
            return ""
        context = user_profile.get_context_string()
        return f"# Current User Context\n{context}" if context else ""

    def clear_cache(self):
        """Clear instruction cache."""
        self._cache.clear()


# Singleton instance
instruction_builder = InstructionBuilder()
