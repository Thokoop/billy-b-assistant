"""
Session module - provides components for voice conversation sessions.
"""

# Import BillySession from the implementation file
from ..session_manager import BillySession
from .audio_handler import AudioHandler
from .error_handler import ErrorHandler
from .function_handler import FunctionHandler
from .instruction_builder import InstructionContext, instruction_builder
from .mic_manager_wrapper import MicManagerWrapper
from .persona_handler import PersonaHandler
from .state_machine import SessionState
from .tool_manager import tool_manager
from .user_handler import UserHandler


__all__ = [
    "AudioHandler",
    "BillySession",
    "ErrorHandler",
    "FunctionHandler",
    "InstructionContext",
    "MicManagerWrapper",
    "PersonaHandler",
    "SessionState",
    "UserHandler",
    "instruction_builder",
    "tool_manager",
]


# Backward compatibility functions
def get_instructions_with_user_context() -> str:
    """
    DEPRECATED: Use instruction_builder.build() instead.

    Generate instructions with current user context and persona.
    """
    import os

    from dotenv import load_dotenv

    from ..config import ENV_PATH
    from ..persona_manager import persona_manager
    from ..profile_manager import user_manager

    load_dotenv(ENV_PATH, override=True)
    current_user_env = os.getenv("CURRENT_USER", "").strip().strip("'\"")
    current_user = user_manager.get_current_user()

    # Determine mode
    if current_user_env and current_user_env.lower() == "guest":
        mode = "guest"
    elif current_user:
        mode = "user"
    else:
        mode = "guest"

    # Build context
    context = InstructionContext(
        mode=mode,
        persona_name=persona_manager.current_persona,
        user_profile=current_user,
    )

    return instruction_builder.build(context)


def get_tools_for_current_mode():
    """
    DEPRECATED: Use tool_manager.get_tools() instead.

    Get tools list based on current mode (guest vs user mode).
    """
    import os

    from dotenv import load_dotenv

    from ..config import ENV_PATH
    from ..logger import logger

    load_dotenv(ENV_PATH, override=True)
    current_user_env = os.getenv("CURRENT_USER", "").strip().strip("'\"")

    logger.info(
        f"🔧 get_tools_for_current_mode: CURRENT_USER='{current_user_env}'", "🔧"
    )

    # Determine mode
    if current_user_env and current_user_env.lower() == "guest":
        mode = "guest"
    else:
        mode = "user"

    return tool_manager.get_tools(mode)
