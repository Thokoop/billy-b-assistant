"""Process-wide Home Assistant satellite service for the assistant worker.

The ESPHome API connection to Home Assistant outlives conversations: Home
Assistant stays subscribed while Billy is idle and may send announcements at
any time. The service therefore runs the satellite server and its mDNS
advertisement on a dedicated asyncio loop thread. A conversation
(:class:`core.ha_satellite.session.HaAssistSession`) runs on the session
thread's own loop and talks to the service through the thread-safe methods
below; pipeline events and TTS audio are routed to the attached session.
"""

import asyncio
import contextlib
import os
import re
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..logger import logger
from .messages import DeviceIdentity, VoiceAssistantAnnounceRequest, WakeWord
from .server import DEFAULT_PORT, SatelliteListener, SatelliteServer


PROVIDER_ID = 'home-assistant'
START_TIMEOUT = 5.0
CALL_TIMEOUT = 6.0
ANNOUNCE_TIMEOUT = 120.0
# How long a ringing device (the phone) may take to be answered. Together with
# ANNOUNCE_TIMEOUT this stays inside Home Assistant's own 5-minute limit.
RING_ANSWER_TIMEOUT = 150.0
KEY_POLL_SECONDS = 2.0

_service: 'SatelliteService | None' = None
_service_lock = threading.Lock()


def device_name(hostname: str | None = None) -> str:
    """The OS hostname as an ESPHome node name (lowercase, max 31 chars)."""
    name = (hostname or os.uname().nodename).split('.')[0].lower()
    name = re.sub(r'[^a-z0-9-]+', '-', name).strip('-')
    return (name or 'billy')[:31]


def device_mac() -> str:
    """The MAC of the main network interface; Home Assistant keys devices on it."""
    for interface in ('wlan0', 'eth0', 'end0'):
        with contextlib.suppress(OSError):
            mac = Path(f'/sys/class/net/{interface}/address').read_text().strip()
            if mac and mac != '00:00:00:00:00:00':
                return mac
    import uuid

    node = uuid.getnode()
    return ':'.join(f'{(node >> shift) & 0xFF:02x}' for shift in range(40, -8, -8))


def default_identity() -> DeviceIdentity:
    hostname = os.uname().nodename
    try:
        from billy_runtime.theme import display_name

        friendly = display_name(hostname=hostname)
    except Exception:
        friendly = hostname.split('.')[0].capitalize() or 'Billy'
    # Billy detects its own wake word (configured in Billy's settings) and
    # starts runs itself; it is advertised so Home Assistant can name it.
    return DeviceIdentity(
        name=device_name(hostname),
        friendly_name=friendly,
        mac_address=device_mac(),
        # Shown in Home Assistant as "<hostname> by ThingsFromThom".
        model=friendly,
        project_version=billy_version(),
        wake_words=local_wake_words(),
    )


def local_wake_words() -> tuple[WakeWord, ...]:
    """Billy's own wake word, when it listens for one.

    Home Assistant's voice setup wizard asks the user to say it and waits
    for a run that names it; without one it skips that step.
    """
    try:
        from .. import config

        if not config.WAKE_WORD_ENABLED:
            return ()
        from ..wakeword import wake_word_phrase

        phrase = wake_word_phrase(config)
    except Exception:
        return ()
    word_id = re.sub(r'[^a-z0-9]+', '_', phrase.lower()).strip('_') or 'wake_word'
    return (WakeWord(word_id, phrase),)


def billy_version() -> str:
    """The running release's version, as the Software settings show it."""
    try:
        from billy_runtime import updates
        from billy_runtime.paths import INSTALL_ROOT

        return updates.version_label(updates.selection()[0], INSTALL_ROOT) or ''
    except Exception:
        return ''


class _Router(SatelliteListener):
    def __init__(self, service: 'SatelliteService') -> None:
        self.service = service

    def connection_changed(self, connected: bool) -> None:
        state = 'connected' if connected else 'disconnected'
        logger.info(f'Home Assistant satellite {state}.', '🏠')

    def pipeline_event(self, event) -> None:
        session = self.service.session
        if session is not None:
            session.deliver_event(event)

    def tts_audio(self, data: bytes, end: bool) -> None:
        session = self.service.session
        if session is not None:
            session.deliver_tts_audio(data)

    def timer_event(self, event) -> None:
        logger.info(f'Home Assistant timer {event.name or event.timer_id}', '⏲️')

    async def announce(self, request: VoiceAssistantAnnounceRequest) -> bool:
        return await self.service.announce(request)


class SatelliteService:
    """Owns the satellite server, its loop thread and the attached session."""

    def __init__(
        self,
        identity: DeviceIdentity | None = None,
        *,
        host: str = '0.0.0.0',
        port: int = DEFAULT_PORT,
        advertise: bool = True,
        announcer: Callable[..., Awaitable[bool]] | None = None,
        ringer: Callable[[dict], bool] | None = None,
        advertiser_factory=None,
        key_store=None,
        key_poll_seconds: float = KEY_POLL_SECONDS,
    ) -> None:
        """``key_store=None`` serves plaintext only (tests and probes)."""
        self.identity = identity or default_identity()
        self.host = host
        self.port = port
        self.advertise = advertise
        self._announcer = announcer
        self._ringer = ringer
        self._advertiser_factory = advertiser_factory
        self.key_store = key_store
        self._key_poll_seconds = key_poll_seconds
        self._key_version = None
        self._key_task: asyncio.Task | None = None
        self.session = None
        self._session_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: SatelliteServer | None = None
        self._advertiser = None

    # ----- lifecycle (any thread) -------------------------------------------

    def start(self, timeout: float = START_TIMEOUT) -> bool:
        if self._thread is not None:
            return True
        started = threading.Event()
        failure: list[BaseException] = []

        def run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._start())
            except BaseException as exc:
                failure.append(exc)
                started.set()
                loop.close()
                return
            started.set()
            try:
                loop.run_forever()
            finally:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self._thread = threading.Thread(target=run, name='ha-satellite', daemon=True)
        self._thread.start()
        if not started.wait(timeout) or failure:
            reason = failure[0] if failure else 'timed out'
            logger.error(f'Home Assistant satellite could not start: {reason}')
            self.stop()
            return False
        return True

    def stop(self, timeout: float = 3.0) -> None:
        """Disconnect Home Assistant and end the loop thread; idempotent."""
        thread, loop = self._thread, self._loop
        self._thread = None
        if loop is not None and not loop.is_closed() and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._stop(), loop)
            with contextlib.suppress(Exception):
                future.result(timeout)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._loop = None

    async def _start(self) -> None:
        self._server = SatelliteServer(
            self.identity,
            _Router(self),
            host=self.host,
            port=self.port,
            key_store=self.key_store,
            on_key_changed=self._key_changed,
        )
        await self._server.start()
        if self.key_store is not None:
            self._key_version = self.key_store.version()
            self._key_task = asyncio.get_running_loop().create_task(self._watch_key())
        if self.advertise:
            factory = self._advertiser_factory
            if factory is None:
                from .discovery import AvahiAdvertiser as factory
            self._advertiser = factory(
                self.identity, self._server.port, encryption=self.encryption_state
            )
            await self._advertiser.start()

    @property
    def encryption_state(self) -> str | None:
        """None without a key store, else 'supported' or 'required' (mDNS)."""
        if self.key_store is None:
            return None
        return 'required' if self.key_store.get() is not None else 'supported'

    def _key_changed(self) -> None:
        """Home Assistant set a key (service loop): republish without waiting."""
        if self.key_store is not None:
            self._key_version = self.key_store.version()
        asyncio.get_running_loop().create_task(self._publish_encryption())

    async def _publish_encryption(self) -> None:
        advertiser = self._advertiser
        if advertiser is not None:
            await advertiser.update_encryption(self.encryption_state)

    async def _watch_key(self) -> None:
        """Apply a key changed elsewhere, such as a reset in the web UI."""
        while True:
            await asyncio.sleep(self._key_poll_seconds)
            version = self.key_store.version()
            if version == self._key_version:
                continue
            self._key_version = version
            state = 'set' if self.key_store.get() is not None else 'removed'
            logger.info(f'Home Assistant satellite: encryption key {state}.', '🔐')
            if self._server is not None:
                self._server.enforce_key()
            await self._publish_encryption()

    async def _stop(self) -> None:
        task, self._key_task = self._key_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        advertiser, self._advertiser = self._advertiser, None
        if advertiser is not None:
            await advertiser.stop()
        server = self._server
        if server is not None:
            await server.stop()

    @property
    def bound_port(self) -> int | None:
        return self._server.port if self._server is not None else None

    @property
    def voice_connected(self) -> bool:
        server = self._server
        return bool(server is not None and server.voice_connected)

    @property
    def peer_host(self) -> str | None:
        server = self._server
        return server.voice_peer_host if server is not None else None

    # ----- sessions -------------------------------------------------------------

    def attach(self, session) -> None:
        with self._session_lock:
            self.session = session

    def detach(self, session) -> None:
        with self._session_lock:
            if self.session is session:
                self.session = None

    async def _call(self, coroutine, timeout: float = CALL_TIMEOUT):
        """Run a server coroutine on the service loop from another loop."""
        loop = self._loop
        if loop is None or loop.is_closed() or self._server is None:
            coroutine.close()
            from .server import SatelliteUnavailable

            raise SatelliteUnavailable('Home Assistant satellite is not running')
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except (TimeoutError, asyncio.CancelledError):
            future.cancel()
            raise

    async def start_pipeline(
        self, *, conversation_id: str = '', wake_word_phrase: str = ''
    ):
        return await self._call(
            self._server.start_pipeline(
                conversation_id=conversation_id, wake_word_phrase=wake_word_phrase
            )
        )

    async def send_audio(self, pcm: bytes) -> bool:
        return await self._call(self._server.send_audio(pcm))

    async def stop_pipeline(self) -> None:
        with contextlib.suppress(Exception):
            await self._call(self._server.stop_pipeline(), timeout=1.5)

    # ----- announcements (service loop) -------------------------------------

    async def announce(self, request: VoiceAssistantAnnounceRequest) -> bool:
        announcer = self._announcer or play_announcement
        urls = [
            self.absolute_url(media)
            for media in (request.preannounce_media_id, request.media_id)
            if media
        ]
        ring = RingAnnouncement(urls, request.text, request.start_conversation)
        if await asyncio.to_thread(self._ring, ring):
            # The session started by lifting the handset plays it (and goes
            # on listening for start_conversation), so nothing follows here.
            return await asyncio.to_thread(ring.wait)
        try:
            success = await asyncio.wait_for(
                announcer(urls, request.text), ANNOUNCE_TIMEOUT
            )
        except TimeoutError:
            logger.warning('Home Assistant announcement timed out.')
            success = False
        if success and request.start_conversation:
            from .. import button

            await asyncio.to_thread(button.request_session_start, 'home-assistant')
        return success

    def _ring(self, ring: 'RingAnnouncement') -> bool:
        """Offer the announcement to a device that rings first (the phone).

        The device gets it like an MQTT "say" (``handle_say``) and starts a
        conversation with that opening once answered. Without such a device
        (the fish) this is False and Billy announces it at once.
        """
        ringer = self._ringer
        try:
            if ringer is None:
                from .. import devices

                ringer = devices.device_handles_say
            return bool(ringer(ring.opening()))
        except Exception as exc:
            logger.warning(f'Could not ring for the announcement: {exc}')
            return False

    def absolute_url(self, url: str) -> str:
        """Home Assistant may send paths relative to its own web server."""
        if not url.startswith('/'):
            return url
        from .. import config

        base = (getattr(config, 'HA_HOST', None) or '').rstrip('/')
        if not base:
            host = self.peer_host or 'homeassistant.local'
            base = f'http://{host}:8123'
        elif '://' not in base:
            base = f'http://{base}'
        return base + url


class RingAnnouncement:
    """A Home Assistant announcement waiting for a ringing device's answer.

    Thread-safe: the device and the conversation it starts run on other
    threads than the service loop that waits for the outcome. The first of
    :meth:`claim` (answered) and :meth:`expire` (nobody answered) wins.
    """

    def __init__(self, urls: list[str], text: str, start_conversation: bool):
        self.urls = list(urls)
        self.text = text
        self.start_conversation = bool(start_conversation)
        self.success = False
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._claimed = False
        self._expired = False

    def opening(self) -> dict:
        """The ``opening`` a device's ``handle_say`` receives."""
        return {
            'text': self.text or 'Home Assistant announcement',
            'kind': 'literal',
            'interactive': self.start_conversation,
            'announcement': self,
        }

    def claim(self) -> bool:
        """The conversation that plays it; False when too late or taken."""
        with self._lock:
            if self._claimed or self._expired:
                return False
            self._claimed = True
            return True

    def finish(self, success: bool) -> None:
        with self._lock:
            if self._done.is_set():
                return
            self.success = bool(success)
            self._done.set()

    def expire(self) -> bool:
        """Give up waiting for an answer; False once it is being played."""
        with self._lock:
            if self._claimed:
                return False
            self._expired = True
            self._done.set()
            return True

    def wait(
        self,
        answer_timeout: float | None = None,
        play_timeout: float | None = None,
    ) -> bool:
        answer_timeout = (
            RING_ANSWER_TIMEOUT if answer_timeout is None else answer_timeout
        )
        play_timeout = ANNOUNCE_TIMEOUT if play_timeout is None else play_timeout
        if not self._done.wait(answer_timeout):
            if self.expire():
                logger.info('Nobody answered the announcement call.', '📵')
                return False
            self._done.wait(play_timeout)
        if not self._done.is_set():
            logger.warning('Home Assistant announcement timed out.')
        return self.success


async def decode_to_playback(url: str, put, *, cancelled=lambda: False) -> int:
    """Decode a URL with ffmpeg into 24 kHz mono PCM chunks; returns bytes."""
    from .. import config

    ffmpeg = os.getenv('FFMPEG_BIN', 'ffmpeg').strip() or 'ffmpeg'
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        '-nostdin',
        '-loglevel',
        'error',
        '-i',
        url,
        '-f',
        's16le',
        '-ac',
        '1',
        '-ar',
        '24000',
        'pipe:1',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    chunk_bytes = int(24000 * config.CHUNK_MS / 1000) * 2
    total = 0
    try:
        while not cancelled():
            data = await process.stdout.read(chunk_bytes)
            if not data:
                break
            put(data)
            total += len(data)
        if cancelled():
            return 0
        _, stderr = await process.communicate()
        if process.returncode:
            detail = stderr.decode(errors='replace').strip()[-200:]
            logger.warning(f'Could not decode announcement: {detail}')
            return 0
        return total
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 2)


async def play_announcement(urls: list[str], text: str = '') -> bool:
    """Play announcement media while Billy is idle; False when busy or failed."""
    from .. import audio, button
    from ..bluetooth import mode as bluetooth_mode
    from ..movements import move_head
    from ..mqtt import mqtt_publish

    if bluetooth_mode.active() or button.is_active:
        logger.info('Skipping Home Assistant announcement: Billy is busy.', '🔕')
        return False
    if not button._session_start_lock.acquire(blocking=False):
        return False
    if text:
        logger.info(f'Home Assistant announcement: {text}', '📣')
    stop = threading.Event()
    try:
        await asyncio.to_thread(button._pause_wakeword_listener)
        audio.ensure_playback_worker_started(button.config.CHUNK_MS)
        audio.playback_done_event.clear()
        move_head('on')
        mqtt_publish('billy/state', 'speaking')
        played = 0
        for url in urls:
            played += await decode_to_playback(
                url,
                audio.playback_queue.put,
                cancelled=lambda: stop.is_set() or bluetooth_mode.active(),
            )
        await audio.playback_queue.wait(cancelled=bluetooth_mode.active)
        return played > 0
    except Exception as exc:
        logger.warning(f'Home Assistant announcement failed: {exc}')
        return False
    finally:
        stop.set()
        audio.playback_done_event.set()
        move_head('off')
        if not bluetooth_mode.active():
            mqtt_publish('billy/state', 'idle')
        button._session_start_lock.release()
        await asyncio.to_thread(button._resume_wakeword_listener)


# ----- module-level service ---------------------------------------------------


def get_service() -> SatelliteService | None:
    return _service


def start_service() -> SatelliteService | None:
    """Start the satellite when Home Assistant answers conversations."""
    global _service
    from .. import config

    if getattr(config, 'REALTIME_AI_PROVIDER', '') != PROVIDER_ID:
        return None
    with _service_lock:
        if _service is not None:
            return _service
        from .keystore import KeyStore

        service = SatelliteService(
            port=int(getattr(config, 'HA_SATELLITE_PORT', DEFAULT_PORT)),
            key_store=KeyStore(),
        )
        if not service.start():
            return None
        _service = service
        logger.info(
            f'Home Assistant can add Billy as "{service.identity.name}" '
            f'(ESPHome integration, port {service.bound_port}).',
            '🏠',
        )
        return service


def stop_service() -> None:
    global _service
    with _service_lock:
        service, _service = _service, None
    if service is not None:
        service.stop()
