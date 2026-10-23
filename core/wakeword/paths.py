"""Model-file lookup shared by local wake-word backends."""

from pathlib import Path


def _resolve_path(root_dir: str, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path(root_dir) / path
    return path


def resolve_keyword_path(root_dir: str, value: str) -> Path:
    if not value:
        return Path("")

    raw = Path(value)
    # Backward compatibility: existing absolute/relative paths continue to work.
    if raw.is_absolute() or raw.parent != Path("."):
        return _resolve_path(root_dir, value)

    # Filename-only mode: resolve from wakewords folder.
    candidate = Path(root_dir) / "wakewords" / raw.name
    if candidate.exists():
        return candidate
    return Path(root_dir) / "wakewords" / raw.name
