"""Exercise delayed motor work against fake GPIO, including stop races."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


class Scheduler:
    def __init__(self):
        self.actions = {}

    def schedule(self, pin, delay, action):
        self.actions[pin] = action

    def cancel(self, pin):
        self.actions.pop(pin, None)


@pytest.fixture
def motors(isolated):
    m = isolated.load('movements')
    m._stop_scheduler = Scheduler()
    m.pending = []
    m.gpio_calls = []
    m.lgpio.tx_pwm = lambda h, pin, freq, duty: m.gpio_calls.append((pin, duty))
    m.lgpio.gpio_write = Mock()
    m.time = SimpleNamespace(time=lambda: 10.0, sleep=lambda _: None)

    def thread(target, **kwargs):
        return SimpleNamespace(start=lambda: m.pending.append(target))

    m.threading = SimpleNamespace(Thread=thread)
    m.Thread = thread
    return m


def test_stopping_before_head_worker_starts_leaves_motor_off(motors):
    m = motors
    m.move_head('on')
    m.stop_all_motors()
    m.gpio_calls.clear()
    m.pending.pop()()
    assert not m.gpio_calls and not m.head_out


@pytest.mark.parametrize('stop', ['all', 'head', 'watchdog'])
def test_stop_during_head_push_prevents_delayed_hold(motors, stop):
    m = motors
    action = {
        'all': m.stop_all_motors,
        'head': lambda: m.move_head('off'),
        'watchdog': lambda: m._stop_channel(m.HEAD),
    }[stop]
    m.time.sleep = lambda _: action()
    m.move_head('on')
    m.pending.pop()()
    assert (m.HEAD, 80) in m.gpio_calls
    assert (m.HEAD, 100) not in m.gpio_calls
    assert not m.head_out


def test_normal_head_timing_and_new_motion_after_stop_are_preserved(motors):
    m = motors
    for _ in range(3):
        m.move_head('on')
        m.pending.pop()()
        assert m.gpio_calls[-1] == (m.HEAD, 100)
        assert m.head_out
        m.stop_all_motors()
        assert not m.head_out


def test_old_deadline_cannot_brake_replacement_motion(motors):
    m = motors
    m.run_motor_async(m.MOUTH, duration=0.1)
    old_stop = m._stop_scheduler.actions[m.MOUTH]
    m.run_motor_async(m.MOUTH, speed_percent=70, duration=0.5)
    m.gpio_calls.clear()
    old_stop()
    assert m.gpio_calls == []
    m._stop_scheduler.actions[m.MOUTH]()
    assert (m.MOUTH, 0) in m.gpio_calls


def test_tail_takeover_invalidates_head_hold_on_shared_bridge(motors):
    m = motors
    m.time.sleep = lambda _: m.move_tail()
    m.move_head('on')
    m.pending.pop()()
    assert (m.HEAD, 100) not in m.gpio_calls
    assert (m.HEAD, 0) in m.gpio_calls and (m.TAIL, 80) in m.gpio_calls
    assert not m.head_out


def test_pending_tail_and_interlude_cannot_restart_after_global_stop(motors):
    m = motors
    m.move_tail_async()
    m.interlude()
    m.stop_all_motors()
    m.gpio_calls.clear()
    for work in m.pending:
        work()
    assert not m.gpio_calls
    assert not m._head_tail_lock.locked()


def test_stop_mid_interlude_prevents_remaining_moves(motors):
    m = motors
    m.time.sleep = lambda _: m.stop_all_motors()
    m.interlude()
    m.pending.pop()()
    assert all(duty == 0 for _, duty in m.gpio_calls)
    assert not m._head_tail_lock.locked()


def test_old_interlude_retraction_cannot_cancel_a_new_head_motion(motors):
    m = motors
    m.random = SimpleNamespace(
        uniform=lambda *args: 0, randint=lambda *args: 0, random=lambda: 0
    )
    m.interlude()
    m.pending.pop(0)()
    old_retract = m._stop_scheduler.actions[m.HEAD]
    m.move_head('off')
    m.move_head('on')
    m.gpio_calls.clear()
    old_retract()
    assert not m.gpio_calls and m.head_out


def test_stop_is_serialized_with_gpio_write(motors):
    m = motors
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def pwm(h, pin, freq, duty):
        if duty == 80:
            entered.set()
            assert release.wait(1)
        m.gpio_calls.append((pin, duty))

    m.lgpio.tx_pwm = pwm
    writer = threading.Thread(
        target=m.run_motor_async, args=(m.MOUTH, None, 80), daemon=True
    )
    stopper = threading.Thread(
        target=lambda: (m.stop_all_motors(), stopped.set()), daemon=True
    )
    writer.start()
    try:
        assert entered.wait(1)
        stopper.start()
        assert not stopped.wait(0.02)
    finally:
        release.set()
        writer.join(1)
        if stopper.ident is not None:
            stopper.join(1)
    assert stopped.is_set()
    assert [duty for pin, duty in m.gpio_calls if pin == m.MOUTH][-1] == 0
