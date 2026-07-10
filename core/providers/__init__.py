import os

from ..config import ENV_PATH
from ..logger import logger
from ..wakeword_provider import wakeword_provider_registry


logger.verbose("Importing core.providers")
# Register realtime AI providers
from ..config import (
    OPENAI_API_KEY,
    OPENAI_MODEL,
    REALTIME_AI_PROVIDER,
    XAI_API_KEY,
    XAI_MODEL,
)
from ..realtime_ai_provider import voice_provider_registry
from .openai_provider import OpenAIProvider
from .openwakeword_wakeword_provider import OpenWakeWordBackend
from .porcupine_wakeword_provider import PorcupineWakeWordBackend
from .xai_provider import XAIProvider


logger.verbose(f"OPENAI_API_KEY set: {bool(OPENAI_API_KEY)}")
logger.verbose(f"XAI_API_KEY set: {bool(XAI_API_KEY)}")
logger.verbose(f"REALTIME_AI_PROVIDER: {REALTIME_AI_PROVIDER}")

if OPENAI_API_KEY:
    openai_provider = OpenAIProvider(api_key=OPENAI_API_KEY, model=OPENAI_MODEL)
    voice_provider_registry.register_provider(openai_provider)

if XAI_API_KEY:
    xai_provider = XAIProvider(api_key=XAI_API_KEY, model=XAI_MODEL)
    voice_provider_registry.register_provider(xai_provider)

available_providers = set(voice_provider_registry.get_available_providers())

if not available_providers:
    # No API keys are set - provide helpful error message with diagnostics
    env_exists = os.path.exists(ENV_PATH) if ENV_PATH else False
    env_path_info = (
        f"Expected .env path: {ENV_PATH}" if ENV_PATH else "ENV_PATH not set"
    )
    env_exists_info = (
        f"File exists: {env_exists}" if ENV_PATH else "Cannot check - ENV_PATH not set"
    )
    openai_from_env = os.getenv("OPENAI_API_KEY", "")
    xai_from_env = os.getenv("XAI_API_KEY", "")

    error_msg = (
        "At least one provider API key must be set!\n"
        f"  {env_path_info}\n"
        f"  {env_exists_info}\n"
        f"  OPENAI_API_KEY from os.getenv: {'set' if openai_from_env else 'not set'}\n"
        f"  XAI_API_KEY from os.getenv: {'set' if xai_from_env else 'not set'}\n"
        f"  Please check your .env file and ensure it contains OPENAI_API_KEY or XAI_API_KEY"
    )
    # Never hard-fail on missing API keys; log a warning so services still start.
    logger.warning(error_msg)
else:
    requested_provider = REALTIME_AI_PROVIDER or "openai"
    if requested_provider in available_providers:
        voice_provider_registry.set_default_provider(requested_provider)
    elif "openai" in available_providers:
        logger.warning(
            f"Configured realtime provider '{requested_provider}' is unavailable. Falling back to OpenAI.",
            "⚠️",
        )
        voice_provider_registry.set_default_provider("openai")
    else:
        fallback_provider = next(iter(available_providers))
        logger.warning(
            f"Configured realtime provider '{requested_provider}' is unavailable. Falling back to '{fallback_provider}'.",
            "⚠️",
        )
        voice_provider_registry.set_default_provider(fallback_provider)

# Register wake-word providers
wakeword_provider_registry.register_provider("porcupine", PorcupineWakeWordBackend)
wakeword_provider_registry.register_provider("openwakeword", OpenWakeWordBackend)
