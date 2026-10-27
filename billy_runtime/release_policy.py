"""Version-tag eligibility shared by settings and update endpoints."""

import os
import re

from packaging.version import Version


CHANNELS = {'stable': 0, 'rc': 1, 'beta': 2}
# Accept existing RC1 tags and dotted prerelease numbers. Exclude branches,
# alpha/dev/post releases, local versions and incomplete version numbers.
TAG = re.compile(
    r'v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
    r'(?:-(?P<stage>(?i:rc|beta))(?:\.?(?:0|[1-9][0-9]*))?)?\Z'
)


def update_channel(values=None):
    values = os.environ if values is None else values
    channel = str(values.get('UPDATE_CHANNEL') or '').strip().lower()
    if channel:
        # A misspelled explicit preference must never opt into prereleases.
        return channel if channel in CHANNELS else 'stable'
    return (
        'rc'
        if str(values.get('SHOW_RC_VERSIONS', '')).strip().lower() == 'true'
        else 'stable'
    )


def release_version(tag):
    return Version(tag) if isinstance(tag, str) and TAG.fullmatch(tag) else None


def allows_release(tag, channel=None):
    match = TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        return False
    stage = (match['stage'] or 'stable').lower()
    selected = update_channel() if channel is None else channel
    return CHANNELS[stage] <= CHANNELS.get(selected, 0)


def newest_release(tags, channel=None):
    eligible = (tag for tag in tags if allows_release(tag, channel))
    return max(eligible, key=release_version, default=None)
