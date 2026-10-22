"""Serialize profile changes across core, mood persistence and web requests."""

import copy
import functools
from pathlib import Path

from .configuration import ROOT
from .storage import transaction


def profile_lock(root=ROOT):
    return transaction(Path(root) / 'profiles' / '.transactions')


def profile_edit(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with profile_lock(self.profile_path.parent.parent):
            old = copy.deepcopy(getattr(self, 'data', None))
            if self.profile_path.exists():
                self.data = self._load_profile()
            try:
                return method(self, *args, **kwargs)
            except Exception:
                self.data = old
                raise

    return wrapped
