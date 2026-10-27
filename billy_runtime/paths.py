"""Stable writable installation data, separate from selected release code."""

import os
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = Path(os.environ.get('BILLY_INSTALL_ROOT', CODE_ROOT)).resolve()
