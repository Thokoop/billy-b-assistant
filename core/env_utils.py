"""Compatibility entrypoint for transactional configuration updates."""

from billy_runtime.configuration import patch_env


def set_env_key(env_path, key: str, value) -> None:
    patch_env(env_path, {key: value})
