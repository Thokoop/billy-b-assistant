"""Small, ephemeral GPT-Live captions shared by the core and web workers.

Captions and the full session transcript live in the runtime directory (RAM on
the Pi) while the session is active. Only fullscreen readers request the full
text. The session still logs its complete transcript once at shutdown.
"""

import json
import os
import threading
import time
import uuid
from contextlib import suppress

from billy_runtime.paths import INSTALL_ROOT
from billy_runtime.storage import atomic_write, runtime_directory
from billy_runtime.worker import _process_identity


# Billy's longer replies can grow beyond two subtitle lines. Keep both
# windows bounded independently of the complete session transcript.
CAPTION_LIMITS = {'user': 160, 'billy': 512}
MAX_SNAPSHOT_BYTES = 16 * 1024  # Includes JSON-escaped Unicode for both windows.
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024


class LiveTranscript:
    def __init__(self, directory=None):
        self.path = (
            directory or runtime_directory(INSTALL_ROOT)
        ) / 'live-captions.json'
        self.transcript_path = self.path.with_name('live-transcript.json')
        self.revision = 0
        self.transcript_revision = -1
        self.enabled = directory is not None or os.getenv('BILLY_WORKER_MODE') == 'core'
        self.session_id = uuid.uuid4().hex
        self.lines = {}
        self.lock = threading.Lock()
        self.closed = False
        self.dirty = False
        self.published = 0.0
        self._segments = []
        self._last_segment = {}
        self._finished = False

    def append(self, speaker, delta, start_ms=None, end_ms=None):
        if speaker not in ('user', 'billy') or not isinstance(delta, str) or not delta:
            return
        now = time.time()
        with self.lock:
            if self._finished:
                return
            # Preserve every fragment independently of the short caption window.
            # A speaker's late fragments can arrive between the other's words.
            segment = self._last_segment.get(speaker)
            previous = segment or {}
            new_segment = (
                segment is None
                or self._has_gap(previous, now, start_ms)
                or self._new_reply(segment, now, start_ms)
            )
            if new_segment:
                segment = {'speaker': speaker, 'parts': []}
                self._segments.append(segment)
                self._last_segment[speaker] = segment
            segment['parts'].append(delta)
            segment.update(updated=now, end_ms=end_ms)
            self.revision += 1
            if self.closed:
                return  # Finish draining transcript events without reviving captions.
            # A new reply replaces this speaker's previous caption and moves
            # below the other speaker. Late fragments keep their existing place.
            if new_segment:
                self.lines.pop(speaker, None)
            previous = self.lines.get(speaker, {})
            text = previous.get('text', '') + delta
            limit = CAPTION_LIMITS[speaker]
            if len(text) > limit:
                text = text[-limit:]
                if ' ' in text:
                    text = text.split(' ', 1)[1]
            self.lines[speaker] = {'text': text, 'updated': now, 'end_ms': end_ms}
            self.dirty = True

    def _new_reply(self, segment, now, start_ms):
        latest = self._segments[-1]
        if latest is segment:
            return False
        # Distinguish a quick reply from a delayed fragment of overlapping
        # speech; short exchanges must not collapse into one line per speaker.
        if isinstance(start_ms, (int, float)) and isinstance(
            latest.get('end_ms'), (int, float)
        ):
            return start_ms >= latest['end_ms']
        return now - segment['updated'] > 0.5

    @staticmethod
    def _has_gap(previous, now, start_ms):
        if isinstance(start_ms, (int, float)) and isinstance(
            previous.get('end_ms'), (int, float)
        ):
            return start_ms - previous['end_ms'] > 2500
        return now - previous.get('updated', 0) > 2.5

    def recent(self, max_chars=2000):
        """The latest part of the conversation, without consuming it."""
        with self.lock:
            segments = [
                (segment['speaker'], ''.join(segment['parts']).strip())
                for segment in self._segments
            ]
        labels = {'user': 'User', 'billy': 'Billy'}
        lines = [f'{labels[speaker]}: {text}' for speaker, text in segments if text]
        text = '\n'.join(lines)
        if len(text) <= max_chars:
            return text
        # Keep whole lines from the end.
        kept = []
        size = 0
        for line in reversed(lines):
            size += len(line) + 1
            if size > max_chars:
                break
            kept.append(line)
        return '\n'.join(reversed(kept))

    def finish(self):
        """Consume the full transcript exactly once, even on concurrent closes."""
        with self.lock:
            if self._finished:
                return ''
            self._finished = True
            segments, self._segments = self._segments, []
            self._last_segment.clear()
        labels = {'user': 'User', 'billy': 'Billy'}
        return '\n'.join(
            f"{labels[segment['speaker']]}: {text}"
            for segment in segments
            if (text := ''.join(segment['parts']).strip())
        )

    def publish(self):
        """Called by a background task, at most five times per second."""
        if not self.enabled:
            return
        with self.lock:
            if self.closed or self._finished:
                return
            now = time.time()
            # Keep the last turn per speaker until their next one replaces it.
            # The heartbeat still lets readers reject stalled sessions.
            if not self.dirty and now - self.published < 1:
                return
            try:
                if not self.lines:
                    self._remove_owned()
                else:
                    pid = os.getpid()
                    snapshot = {
                        'session': self.session_id,
                        'published': now,
                        'pid': pid,
                        'process_start': _process_identity(pid),
                        'revision': self.revision,
                        'lines': [
                            {'speaker': k, 'text': v['text'], 'updated': v['updated']}
                            for k, v in self.lines.items()
                        ],
                    }
                    self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    # Write history only when text changes. The small caption
                    # snapshot remains the heartbeat for both views.
                    if self.transcript_revision != self.revision:
                        history = {
                            'session': self.session_id,
                            'revision': self.revision,
                            'turns': [
                                {
                                    'speaker': segment['speaker'],
                                    'text': ''.join(segment['parts']),
                                }
                                for segment in self._segments
                            ],
                        }
                        atomic_write(self.transcript_path, json.dumps(history))
                        self.transcript_revision = self.revision
                    atomic_write(self.path, json.dumps(snapshot))
                self.dirty = False
                self.published = now
            except (OSError, ValueError, AttributeError, IndexError):
                pass  # Captions must never stop a conversation or flood its log.

    def _remove_owned(self):
        for path in (self.path, self.transcript_path):
            if (
                path.exists()
                and json.loads(path.read_text()).get('session') == self.session_id
            ):
                path.unlink(missing_ok=True)

    def close(self):
        with self.lock:
            self.closed = True
            self.lines.clear()
            if self.enabled:
                with suppress(OSError, ValueError, AttributeError):
                    self._remove_owned()


def read_live_transcript(directory=None, *, include_transcript=False):
    """Reject stale snapshots after a crash, restart, or reused process ID."""
    path = (directory or runtime_directory(INSTALL_ROOT)) / 'live-captions.json'
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return None
        data = json.loads(path.read_text())
        now = time.time()
        if not 0 <= now - data['published'] < 3:
            return None
        pid = data['pid']
        if (
            not isinstance(pid, int)
            or pid <= 0
            or data['process_start'] != _process_identity(pid)
        ):
            return None
        lines = data['lines']
        if not isinstance(lines, list) or len(lines) > 2:
            return None
        lines = [
            line
            for line in lines
            if line['speaker'] in ('user', 'billy')
            and isinstance(line['text'], str)
            and len(line['text']) <= CAPTION_LIMITS[line['speaker']]
            and 0 <= line['updated'] <= data['published']
        ]
        if not lines:
            return None
        snapshot = {
            'session': data['session'],
            'published': data['published'],
            'revision': data.get('revision'),
            'lines': lines,
        }
        if include_transcript:
            try:
                history_path = path.with_name('live-transcript.json')
                if history_path.stat().st_size > MAX_TRANSCRIPT_BYTES:
                    raise ValueError('Transcript exceeds the web view size limit')
                history = json.loads(history_path.read_text())
                turns = history['turns']
                if (
                    history['session'] != data['session']
                    or history['revision'] != snapshot['revision']
                    or not isinstance(turns, list)
                    or not all(
                        isinstance(turn, dict)
                        and turn.get('speaker') in ('user', 'billy')
                        and isinstance(turn.get('text'), str)
                        for turn in turns
                    )
                ):
                    raise ValueError('Transcript does not match the current session')
                snapshot['transcript'] = turns
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                # A reader can race a publication; the socket retries without
                # clearing valid compact captions or showing a partial history.
                snapshot['transcript_error'] = (
                    'Session transcript is temporarily unavailable.'
                )
        return snapshot
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return None
