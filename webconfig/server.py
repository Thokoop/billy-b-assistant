"""Stable entry point for existing web service files. Keep this path."""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    from billy_runtime.entrypoints import run

    run(Path(__file__), 'web')
