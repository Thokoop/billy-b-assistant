"""Ephemeral Live captions: bounded, isolated speakers and safe after restarts."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def captions(isolated, tmp_path, monkeypatch):
    module = isolated.load('providers.live_transcript')
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: clock.now))
    writer = module.LiveTranscript(tmp_path)
    return module, writer, clock


def test_interleaved_fragments_preserve_speakers_spacing_and_punctuation(captions):
    m, w, _ = captions
    w.append('user', 'Hello')
    w.append('billy', 'Hi')
    w.append('user', ', Billy!')
    w.append('billy', ' there.')
    w.publish()
    data = m.read_live_transcript(w.path.parent)
    assert [line['text'] for line in data['lines']] == ['Hello, Billy!', 'Hi there.']
    assert w.path.stat().st_mode & 0o777 == 0o600


def test_bounded_caption_windows_and_timestamp_gap(captions):
    m, w, clock = captions
    w.append('billy', 'word ' * 1000, 0, 1000)
    assert len(w.lines['billy']['text']) <= m.CAPTION_LIMITS['billy']
    clock.now += 4  # Transport delay alone must not break contiguous speech.
    w.append('billy', 'end.', 1100, 1500)
    assert w.lines['billy']['text'].endswith('word end.')
    w.append('billy', 'New sentence', 4100, 5000)
    assert w.lines['billy']['text'] == 'New sentence'
    clock.now += 3
    w.append('user', 'A new turn')
    assert w.lines['user']['text'] == 'A new turn'


def test_quiet_sessions_keep_captions_but_stalled_or_stopped_sessions_clear(captions):
    m, w, clock = captions
    w.append('user', 'Temporary')
    w.publish()
    clock.now += 4
    assert m.read_live_transcript(w.path.parent) is None  # Writer stalled.
    w.publish()
    assert m.read_live_transcript(w.path.parent)
    clock.now += 60
    w.publish()
    assert m.read_live_transcript(w.path.parent)['lines'][0]['text'] == 'Temporary'
    w.append('billy', 'Reply')
    w.publish()
    w.close()
    w.append('billy', 'Late fragment')
    w.publish()
    assert not w.path.exists() and w.lines == {}


def test_old_session_cleanup_cannot_remove_new_session(captions):
    m, old, _ = captions
    old.append('user', 'Old')
    old.publish()
    new = m.LiveTranscript(old.path.parent)
    new.append('user', 'New')
    new.publish()
    old.close()
    assert m.read_live_transcript(new.path.parent)['session'] == new.session_id


@pytest.mark.parametrize(
    'mutation',
    [
        {'process_start': 'other'},
        {'pid': -1},
        {'published': 0},
        {'lines': 'bad'},
        {'lines': [None]},
        {'lines': [{'speaker': 'user', 'text': 'future', 'updated': 1001}]},
        {'lines': [{'speaker': 'user', 'text': 'invalid', 'updated': -1}]},
        {'lines': [{'speaker': 'user', 'text': 'invalid', 'updated': float('nan')}]},
        {'lines': [{'speaker': 'user', 'text': 'x' * 1000, 'updated': 1000}]},
    ],
)
def test_stale_or_invalid_snapshot_is_rejected(captions, mutation):
    m, w, _ = captions
    w.append('user', 'Hi')
    w.publish()
    data = json.loads(w.path.read_text())
    data.update(mutation)
    w.path.write_text(json.dumps(data))
    assert m.read_live_transcript(w.path.parent) is None


def test_failed_caption_io_is_nonfatal_and_can_retry(captions, monkeypatch):
    m, w, _ = captions
    write = m.atomic_write
    monkeypatch.setattr(m, 'atomic_write', Mock(side_effect=OSError('read only')))
    w.append('user', 'Hello')
    w.publish()
    monkeypatch.setattr(m, 'atomic_write', write)
    w.publish()
    assert m.read_live_transcript(w.path.parent)
    w.close()


def test_empty_and_invalid_deltas_do_not_publish(captions):
    _, w, _ = captions
    for speaker, text in [('user', None), ('user', ''), ('other', 'No'), ('billy', {})]:
        w.append(speaker, text)
    w.publish()
    assert not w.path.exists()


def test_full_transcript_survives_caption_truncation_silence_and_close(captions):
    m, w, clock = captions
    question = 'A very long question with punctuation, ' * 100 + 'please?'
    answer = 'A complete answer. ' * 100 + 'Done.'
    w.append('user', question, 0, 1000)
    w.append('billy', answer, 1100, 2000)
    w.publish()
    assert (
        len(m.read_live_transcript(w.path.parent)['lines'][0]['text'])
        <= m.CAPTION_LIMITS['user']
    )
    clock.now += 10
    w.publish()
    assert len(m.read_live_transcript(w.path.parent)['lines']) == 2
    w.close()
    assert not w.path.exists()
    assert w.finish() == f'User: {question}\nBilly: {answer}'
    assert not w._segments and not w._last_segment
    assert w.finish() == ''


def test_full_transcript_groups_interleaved_fragments_and_subsequent_turns(captions):
    _, w, clock = captions
    w.append('user', 'Hello', 0, 500)
    w.append('billy', 'Hi', 600, 700)
    w.append('user', ', Billy!', 500, 600)
    w.append('billy', ' there.', 700, 1200)
    clock.now += 10
    w.append('user', 'What time is it?', 7000, 8000)
    w.append('billy', 'Time for tea.', 8100, 9000)
    assert (
        w.finish()
        == 'User: Hello, Billy!\nBilly: Hi there.\nUser: What time is it?\nBilly: Time for tea.'
    )


def test_caption_close_allows_final_transcript_but_finish_rejects_late_events(captions):
    _, w, _ = captions
    w.append('billy', 'Good')
    w.close()
    w.append('billy', 'bye.')
    w.publish()
    assert not w.path.exists() and not w.lines
    assert w.finish() == 'Billy: Goodbye.'
    w.append('user', 'too late')
    assert not w._segments and w.finish() == ''


def test_empty_transcripts_and_new_sessions_do_not_repeat_previous_text(captions):
    m, first, _ = captions
    assert first.finish() == ''
    second = m.LiveTranscript(first.path.parent)
    second.append('user', 'Fresh session')
    assert second.finish() == 'User: Fresh session'
    third = m.LiveTranscript(first.path.parent)
    third.append('user', '   ')
    assert third.finish() == ''


@pytest.mark.parametrize('timestamps', [True, False])
def test_short_replies_stay_in_conversation_order(captions, timestamps):
    _, w, clock = captions
    for index, (speaker, text) in enumerate([
        ('user', 'Hello.'),
        ('billy', 'Hi!'),
        ('user', 'Tea?'),
        ('billy', 'Yes, please.'),
    ]):
        clock.now += 0.75
        timing = (index * 750, index * 750 + 500) if timestamps else (None, None)
        w.append(speaker, text, *timing)
    assert w.finish() == 'User: Hello.\nBilly: Hi!\nUser: Tea?\nBilly: Yes, please.'


@pytest.mark.parametrize('timestamps', [True, False])
def test_captions_follow_new_turns_without_joining_previous_replies(
    captions, timestamps
):
    m, w, clock = captions
    turns = [
        ('user', 'Hello.'),
        ('billy', 'Hi!'),
        ('user', 'Tea?'),
        ('billy', 'Yes, please.'),
    ]
    for index, (speaker, text) in enumerate(turns):
        clock.now += 0.75
        timing = (index * 750, index * 750 + 500) if timestamps else (None, None)
        w.append(speaker, text, *timing)
        w.publish()
        lines = m.read_live_transcript(w.path.parent)['lines']
        assert [(line['speaker'], line['text']) for line in lines] == turns[
            max(0, index - 1) : index + 1
        ]
    assert w.finish() == 'User: Hello.\nBilly: Hi!\nUser: Tea?\nBilly: Yes, please.'


def test_late_fragments_do_not_reorder_captions_or_merge_the_next_turn(captions):
    m, w, clock = captions
    w.append('user', 'Hello', 0, 500)
    w.append('billy', 'Hi', 600, 700)
    clock.now += 1
    w.append('user', ', Billy!', 500, 600)
    w.publish()
    lines = m.read_live_transcript(w.path.parent)['lines']
    assert [(line['speaker'], line['text']) for line in lines] == [
        ('user', 'Hello, Billy!'),
        ('billy', 'Hi'),
    ]
    w.append('user', 'Tea?', 800, 1000)
    clock.now += 1
    w.append('billy', ' there.', 700, 800)
    w.publish()
    lines = m.read_live_transcript(w.path.parent)['lines']
    assert [(line['speaker'], line['text']) for line in lines] == [
        ('billy', 'Hi there.'),
        ('user', 'Tea?'),
    ]
    w.append('billy', 'Yes!', 1100, 1300)
    w.publish()
    lines = m.read_live_transcript(w.path.parent)['lines']
    assert [(line['speaker'], line['text']) for line in lines] == [
        ('user', 'Tea?'),
        ('billy', 'Yes!'),
    ]


def test_long_billy_caption_keeps_its_start_and_resets_on_next_reply(captions):
    m, w, clock = captions
    reply = 'This is a longer reply that needs more room in the subtitles. ' * 6
    w.append('billy', reply[:150], 0, 500)
    w.append('billy', reply[150:], 500, 1000)
    w.publish()
    assert m.read_live_transcript(w.path.parent)['lines'][0]['text'] == reply
    clock.now += 0.75
    w.append('user', 'Thanks!', 1100, 1500)
    w.append('billy', 'Any time.', 1600, 1800)
    w.publish()
    assert [
        line['text'] for line in m.read_live_transcript(w.path.parent)['lines']
    ] == [
        'Thanks!',
        'Any time.',
    ]


@pytest.mark.parametrize('speaker,limit', [('user', 160), ('billy', 512)])
def test_caption_limits_remain_bounded_per_speaker(captions, speaker, limit):
    m, w, _ = captions
    w.append(speaker, 'x' * 2000)
    w.publish()
    assert m.read_live_transcript(w.path.parent)['lines'][0]['text'] == 'x' * limit


def test_long_unicode_captions_fit_snapshot_limit(captions):
    m, w, _ = captions
    for speaker, limit in m.CAPTION_LIMITS.items():
        w.append(speaker, '🐟' * limit)
    w.publish()
    assert 4096 < w.path.stat().st_size <= m.MAX_SNAPSHOT_BYTES
    assert [
        (line['speaker'], line['text'])
        for line in m.read_live_transcript(w.path.parent)['lines']
    ] == [(speaker, '🐟' * limit) for speaker, limit in m.CAPTION_LIMITS.items()]
    w.path.write_text(' ' * (m.MAX_SNAPSHOT_BYTES + 1))
    assert m.read_live_transcript(w.path.parent) is None


def test_question_stays_during_long_reply_and_next_turn_only_replaces_its_speaker(
    captions,
):
    m, w, clock = captions
    question = 'How was your day?'
    w.append('user', question, 0, 500)
    w.append('billy', 'Let me tell you', 600, 1000)
    for index, fragment in enumerate([
        ' all about it.',
        ' It was busy.',
        ' Very busy.',
    ]):
        clock.now += 20
        w.append('billy', fragment, 1000 + index * 500, 1500 + index * 500)
        w.publish()
        # Each fresh read represents a new or reconnected web subscriber.
        lines = m.read_live_transcript(w.path.parent)['lines']
        assert [(line['speaker'], line['text']) for line in lines] == [
            ('user', question),
            ('billy', w.lines['billy']['text']),
        ]
    answer = w.lines['billy']['text']
    clock.now += 30
    w.publish()
    assert [
        line['text'] for line in m.read_live_transcript(w.path.parent)['lines']
    ] == [question, answer]
    w.append('user', 'And tomorrow?', 3000, 3500)
    w.publish()
    assert [
        (line['speaker'], line['text'])
        for line in m.read_live_transcript(w.path.parent)['lines']
    ] == [
        ('billy', answer),
        ('user', 'And tomorrow?'),
    ]
    w.append('billy', 'More of the same.', 3600, 4000)
    w.publish()
    assert [
        (line['speaker'], line['text'])
        for line in m.read_live_transcript(w.path.parent)['lines']
    ] == [
        ('user', 'And tomorrow?'),
        ('billy', 'More of the same.'),
    ]
    w.close()
    assert m.read_live_transcript(w.path.parent) is None


def test_full_history_contains_all_turns_and_untruncated_text_for_late_readers(
    captions,
):
    m, w, clock = captions
    turns = [
        ('user', 'First question'),
        ('billy', 'A long answer. ' * 500),
        ('user', 'Next question'),
        ('billy', 'Another full answer. ' * 500),
    ]
    for index, (speaker, text) in enumerate(turns):
        clock.now += 1
        w.append(speaker, text, index * 1000, index * 1000 + 500)
    w.publish()
    compact = m.read_live_transcript(w.path.parent)
    assert 'transcript' not in compact and len(compact['lines']) == 2
    full = m.read_live_transcript(w.path.parent, include_transcript=True)
    assert full['transcript'] == [
        {'speaker': speaker, 'text': text} for speaker, text in turns
    ]
    assert w.transcript_path.stat().st_mode & 0o777 == 0o600
    clock.now += 2
    w.publish()
    assert (
        m.read_live_transcript(w.path.parent, include_transcript=True)['transcript']
        == full['transcript']
    )
    w.close()
    assert not w.transcript_path.exists()
    assert w.finish() == '\n'.join(
        f"{'User' if speaker == 'user' else 'Billy'}: {text.strip()}"
        for speaker, text in turns
    )


def test_history_heartbeat_does_not_rewrite_text_and_late_fragments_update_their_turn(
    captions, monkeypatch
):
    m, w, clock = captions
    write = Mock(wraps=m.atomic_write)
    monkeypatch.setattr(m, 'atomic_write', write)
    w.append('user', 'Hello', 0, 500)
    w.append('billy', 'Hi', 600, 800)
    w.publish()
    write.reset_mock()
    clock.now += 2
    w.publish()
    assert [call.args[0] for call in write.call_args_list] == [w.path]
    w.append('user', ', Billy!', 500, 600)
    w.publish()
    assert m.read_live_transcript(w.path.parent, include_transcript=True)[
        'transcript'
    ] == [
        {'speaker': 'user', 'text': 'Hello, Billy!'},
        {'speaker': 'billy', 'text': 'Hi'},
    ]


@pytest.mark.parametrize(
    'mutation',
    [
        {'session': 'another-session'},
        {'revision': -1},
        {'turns': [None]},
        {'turns': [{'speaker': 'other', 'text': 'No'}]},
        {'turns': [{'speaker': 'user', 'text': 123}]},
    ],
)
def test_invalid_or_mismatched_history_never_replaces_current_session(
    captions, mutation
):
    m, w, _ = captions
    w.append('user', 'Current')
    w.publish()
    data = json.loads(w.transcript_path.read_text())
    data.update(mutation)
    w.transcript_path.write_text(json.dumps(data))
    full = m.read_live_transcript(w.path.parent, include_transcript=True)
    assert full['lines'][0]['text'] == 'Current'
    assert 'transcript' not in full and full['transcript_error']
    assert 'transcript_error' not in m.read_live_transcript(w.path.parent)


def test_history_is_not_served_after_a_stall_or_old_session_cleanup(captions):
    m, old, clock = captions
    old.append('user', 'Old')
    old.publish()
    clock.now += 4
    assert m.read_live_transcript(old.path.parent, include_transcript=True) is None
    new = m.LiveTranscript(old.path.parent)
    new.append('user', 'New')
    new.publish()
    old.close()
    assert m.read_live_transcript(new.path.parent, include_transcript=True)[
        'transcript'
    ] == [
        {'speaker': 'user', 'text': 'New'},
    ]
    new.close()
    assert not new.path.exists() and not new.transcript_path.exists()
