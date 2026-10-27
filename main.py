"""Stable entry point for existing Billy service files. Keep this path."""

from pathlib import Path


if __name__ == '__main__':
    from billy_runtime.entrypoints import run

    run(Path(__file__), 'core')
