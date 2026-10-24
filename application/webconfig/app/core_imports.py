from core import config as core_config
from core.providers.realtime import voice_provider_registry


# Export core_config for use in server.py
__all__ = ['core_config', 'voice_provider_registry']
