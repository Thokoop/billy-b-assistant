"""Bounded, cancellable tool execution without blocking the Realtime receiver."""

import asyncio
from collections import deque
from contextvars import ContextVar


_tool_context = ContextVar('billy_tool_call', default=None)


class ToolCalls:
    def __init__(self, session, *, timeout=45.0, capacity=8):
        self.session = session
        self.timeout = timeout
        self.capacity = capacity
        self._epoch = 0
        self._tasks = {}
        self._seen = deque(maxlen=128)
        self._lock = asyncio.Lock()
        self._input_wait = None

    def guard(self):
        """Reject even cancellation-resistant handlers before they send anything."""
        context = _tool_context.get()
        if context is not None:
            owner, epoch = context
            if (
                owner is not self
                or epoch != self._epoch
                or not self.session.session_active.is_set()
            ):
                raise asyncio.CancelledError

    def in_call(self):
        context = _tool_context.get()
        return context is not None and context[0] is self

    def pending(self, response_id):
        return any(r == response_id and not t.done() for r, t in self._tasks.values())

    def start(self, name, arguments, call_id, response_id):
        if not self.session.session_active.is_set() or call_id in self._seen:
            return
        if len(self._tasks) >= self.capacity:
            raise RuntimeError('Too many pending tool calls')
        self._seen.append(call_id)
        task = asyncio.create_task(
            self._run(name, arguments, call_id, self._epoch),
            name=f'billy-tool-{name}',
        )
        self._tasks[call_id] = (response_id, task)
        task.add_done_callback(
            lambda done, epoch=self._epoch: self._finished(
                call_id, response_id, done, epoch
            )
        )

    def _finished(self, call_id, response_id, task, epoch):
        self._tasks.pop(call_id, None)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.session.loop.call_exception_handler({
                    'message': 'Tool task cleanup failed',
                    'exception': error,
                })
            if epoch == self._epoch and not self.pending(response_id):
                self.session._on_tool_calls_finished(response_id)

    def cancel_input_wait(self):
        if self._input_wait is not None:
            self._input_wait.cancel()
            self._input_wait = None

    def release_input_after_playback(self, response_id):
        self.cancel_input_wait()
        self._input_wait = asyncio.create_task(self._release_input(response_id))

    async def _release_input(self, response_id):
        try:
            completed = await self.session.audio_handler.wait_for_playback_complete()
            if (
                completed
                and self.session.session_active.is_set()
                and response_id == self.session._current_response_id
                and self.pending(response_id)
            ):
                self.session.audio_handler.signal_playback_done()
                self.session.state.assistant_speaking = False
                self.session.state.allow_mic_input = True
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.session.stop_session(reason='tool_leadin_playback_failed')

    def cancel(self):
        self._epoch += 1
        self.cancel_input_wait()
        current = asyncio.current_task()
        for _, task in tuple(self._tasks.values()):
            # A failed send can end its own session from inside a tool task.
            if task is not current:
                task.cancel()

    async def _run(self, name, arguments, call_id, epoch):
        token = _tool_context.set((self, epoch))
        try:
            try:
                async with asyncio.timeout(self.timeout):
                    async with self._lock:
                        self.guard()
                        await self.session.function_handler.handle(
                            name, arguments, call_id
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.guard()
                # Failed actions must also complete their original call. Never
                # claim a timed-out external operation was rolled back.
                try:
                    await self.session._report_tool_failure(call_id, name, exc)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self.session.stop_session(reason='tool_result_send_failed')
        finally:
            _tool_context.reset(token)
