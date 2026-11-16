"""ESPHome API server that lets Home Assistant use Billy as an Assist satellite.

Home Assistant connects to Billy (TCP 6053 by default), subscribes to voice
assistant requests and runs the Assist pipeline. Billy starts a run after its
own wake word, streams microphone PCM (16 kHz, 16-bit mono) until Home
Assistant's VAD ends speech, and receives pipeline events. The TTS result
arrives as a URL in the ``TTS_END`` event or as streamed audio frames.

Everything here runs on one asyncio loop and is hardware-free: microphone,
playback and motors stay with the caller through :class:`SatelliteListener`.
Callbacks run on the loop and must not block.

Encryption: with a :class:`~core.ha_satellite.keystore.KeyStore`, Billy speaks
the ESPHome Noise transport. Without a stored key it accepts plaintext and the
all-zero key, and only lets the zero-key session set a key (Home Assistant's
automatic provisioning). Once a key is stored, plaintext and the zero key are
refused and every connection must use that key.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable

from ..logger import logger
from .messages import (
    DeviceIdentity,
    HelloRequest,
    MessageType,
    SubscribeVoiceAssistantRequest,
    VoiceAssistantAnnounceRequest,
    VoiceAssistantAudio,
    VoiceAssistantEvent,
    VoiceAssistantEventResponse,
    VoiceAssistantResponse,
    VoiceAssistantSubscribeFlag,
    VoiceAssistantTimerEventResponse,
    announce_finished,
    configuration_response,
    device_capabilities_response,
    device_info_response,
    hello_response,
    parse_set_configuration,
    set_key_response,
    voice_assistant_audio,
    voice_assistant_request,
)
from .noise import ZERO_PSK, NoiseError, NoiseHandshake, decode_psk
from .wire import (
    NOISE_PREAMBLE,
    PLAINTEXT_PREAMBLE,
    ProtocolError,
    decode_message,
    encode_frame,
    encode_noise_frame,
    get_bytes,
    read_frame,
    read_noise_frame,
)


DEFAULT_PORT = 6053
MAX_CONNECTIONS = 4
HANDSHAKE_TIMEOUT = 10.0
# Plaintext sessions that were open when Home Assistant set a key are closed
# after this delay, once Home Assistant has stored the key it will reconnect with.
PLAINTEXT_GRACE_SECONDS = 3.0
HANDSHAKE_MAC_FAILURE = b'Handshake MAC failure'

# What the zero-key provisioning session may do: identify Billy and set a key.
_PROVISIONING_MESSAGES = {
    MessageType.PING_REQUEST,
    MessageType.PING_RESPONSE,
    MessageType.DISCONNECT_REQUEST,
    MessageType.DISCONNECT_RESPONSE,
    MessageType.DEVICE_INFO_REQUEST,
    MessageType.NOISE_ENCRYPTION_SET_KEY_REQUEST,
}

_MIC_CLOSING_EVENTS = {
    VoiceAssistantEvent.STT_VAD_END,
    VoiceAssistantEvent.STT_END,
    VoiceAssistantEvent.RUN_END,
    VoiceAssistantEvent.ERROR,
}
_RUN_ENDING_EVENTS = {VoiceAssistantEvent.RUN_END, VoiceAssistantEvent.ERROR}
# After Billy stops a run Home Assistant still has open, that run's ERROR and
# RUN_END can arrive after the next start request (button pressed twice in a
# row). They belong to the old run until the new run's RUN_START; Home
# Assistant ends the old run before it starts the new one.
STALE_END_SECONDS = 3.0
RUN_ENDED_EARLY = 'Home Assistant ended the run before it started'


class SatelliteUnavailable(Exception):
    """Home Assistant is not connected or refused to start a pipeline run."""


class RunEndedEarly(SatelliteUnavailable):
    """Home Assistant ended a run straight away, without starting it.

    Its voice setup wizard does this while it waits for a wake word; it is
    not a failure of Billy or the pipeline.
    """


class SatelliteListener:
    """Receives satellite activity. All methods run on the event loop."""

    def connection_changed(self, connected: bool) -> None:
        """Home Assistant gained or lost its voice subscription."""

    def pipeline_event(self, event: VoiceAssistantEventResponse) -> None:
        """An Assist pipeline event (STT text, TTS URL, errors, ...)."""

    def tts_audio(self, data: bytes, end: bool) -> None:
        """Streamed TTS audio sent over the API connection."""

    def timer_event(self, event: VoiceAssistantTimerEventResponse) -> None:
        """A timer started, updated, cancelled or finished in Home Assistant."""

    def active_wake_words_changed(self, wake_word_ids: list[str]) -> None:
        """Home Assistant selected different wake words for this satellite."""

    async def announce(self, request: VoiceAssistantAnnounceRequest) -> bool:
        """Play an announcement; return whether playback succeeded."""
        return False


class PipelineRun:
    """One Assist pipeline run started by Billy."""

    def __init__(self) -> None:
        self.accepted: asyncio.Future[VoiceAssistantResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self.mic_open = False
        self.finished = asyncio.Event()
        self.error: str | None = None
        self.started = False  # Home Assistant sent this run's RUN_START

    def _finish(self, error: str | None = None) -> None:
        self.mic_open = False
        if error and self.error is None:
            self.error = error
        if not self.accepted.done():
            kind = RunEndedEarly if error == RUN_ENDED_EARLY else SatelliteUnavailable
            self.accepted.set_exception(kind(error or 'run ended'))
            # Nobody may be awaiting it any more; avoid "never retrieved" noise.
            self.accepted.exception()
        self.finished.set()


class _PlainCodec:
    encrypted = False

    def encode(self, message_type: int, payload: bytes) -> bytes:
        return encode_frame(message_type, payload)

    async def read(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        return await read_frame(reader)


class _NoiseCodec:
    """ESPHome's encrypted frame: AEAD(type16, length16, payload)."""

    encrypted = True

    def __init__(self, send, receive, key: bytes) -> None:
        self._send = send
        self._receive = receive
        self.key = key

    def encode(self, message_type: int, payload: bytes) -> bytes:
        header = message_type.to_bytes(2, 'big') + len(payload).to_bytes(2, 'big')
        return encode_noise_frame(self._send.encrypt(header + payload))

    async def read(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        frame = await read_noise_frame(reader)
        try:
            message = self._receive.decrypt(frame)
        except NoiseError as exc:
            raise ProtocolError(f'encrypted frame rejected: {exc}') from None
        if len(message) < 4:
            raise ProtocolError('encrypted frame too short')
        message_type = int.from_bytes(message[:2], 'big')
        size = int.from_bytes(message[2:4], 'big')
        if size != len(message) - 4:
            raise ProtocolError('encrypted frame length mismatch')
        return message_type, bytes(message[4:])


class _Connection:
    def __init__(
        self,
        server: 'SatelliteServer',
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info('peername')
        self.client_info = ''
        self.closed = False
        self.codec: _PlainCodec | _NoiseCodec = _PlainCodec()
        # Noise session with the all-zero key: may only set a key.
        self.provisioning = False
        self._tasks: set[asyncio.Task] = set()

    @property
    def encrypted(self) -> bool:
        return self.codec.encrypted

    async def send(self, message_type: MessageType, payload: bytes = b'') -> bool:
        if self.closed:
            return False
        try:
            self.writer.write(self.codec.encode(message_type, payload))
            await asyncio.wait_for(self.writer.drain(), self.server.write_timeout)
        except (TimeoutError, ConnectionError, RuntimeError) as exc:
            logger.warning(f'HA satellite: send to {self.peer} failed: {exc!r}')
            self.close()
            return False
        return True

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            self.writer.close()

    async def _write_raw(self, data: bytes) -> None:
        self.writer.write(data)
        await asyncio.wait_for(self.writer.drain(), self.server.write_timeout)

    async def _open(self) -> tuple[int, bytes] | None:
        """Choose the transport from the first byte; returns the first message."""
        preamble = (await self.reader.readexactly(1))[0]
        keys = self.server.key_store
        key = keys.get() if keys is not None else None
        if preamble == PLAINTEXT_PREAMBLE:
            if key is not None:
                # A Noise frame tells aioesphomeapi that encryption is required.
                logger.warning(
                    f'HA satellite: refused unencrypted connection from {self.peer}; '
                    'Home Assistant needs the encryption key'
                )
                await self._write_raw(encode_noise_frame(b'\x01Encryption required'))
                return None
            return await read_frame(self.reader, preamble=preamble)
        if preamble != NOISE_PREAMBLE or keys is None:
            raise ProtocolError(f'unsupported frame preamble 0x{preamble:02x}')
        return await asyncio.wait_for(self._handshake(key), HANDSHAKE_TIMEOUT)

    async def _handshake(self, key: bytes | None) -> tuple[int, bytes] | None:
        await read_noise_frame(self.reader, preamble=NOISE_PREAMBLE)  # client hello
        identity = self.server.identity
        mac = ''.join(
            c for c in identity.mac_address.lower() if c in '0123456789abcdef'
        )
        await self._write_raw(
            encode_noise_frame(
                b'\x01' + identity.name.encode() + b'\x00' + mac.encode() + b'\x00'
            )
        )
        frame = await read_noise_frame(self.reader)
        if not frame or frame[0] != 0:
            raise ProtocolError('malformed Noise handshake')
        psk = key if key is not None else ZERO_PSK
        handshake = NoiseHandshake(psk)
        try:
            handshake.read_message_1(frame[1:])
            reply = handshake.write_message_2()
        except NoiseError:
            # Wrong key: aioesphomeapi reports this text as "invalid key".
            await self._write_raw(encode_noise_frame(b'\x01' + HANDSHAKE_MAC_FAILURE))
            logger.warning(
                f'HA satellite: {self.peer} used the wrong encryption key'
                if key is not None
                else f'HA satellite: Noise handshake from {self.peer} failed'
            )
            return None
        await self._write_raw(encode_noise_frame(b'\x00' + reply))
        send, receive = handshake.split()
        self.codec = _NoiseCodec(send, receive, psk)
        self.provisioning = key is None
        return await self.codec.read(self.reader)

    async def run(self) -> None:
        try:
            first = await self._open()
            if first is None:
                return
            message_type, payload = first
            if message_type != MessageType.HELLO_REQUEST:
                raise ProtocolError(f'expected HelloRequest, got {message_type}')
            hello = HelloRequest.parse(decode_message(payload))
            self.client_info = hello.client_info
            logger.info(
                f'HA satellite: {hello.client_info or "client"} connected '
                f'from {self.peer} (API {hello.api_version_major}.'
                f'{hello.api_version_minor})'
            )
            await self.send(
                MessageType.HELLO_RESPONSE,
                hello_response(self.server.identity, self.server.server_info),
            )
            while not self.closed:
                message_type, payload = await self.codec.read(self.reader)
                await self.server._handle(self, message_type, payload)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass
        except ProtocolError as exc:
            logger.warning(f'HA satellite: closing {self.peer}: {exc}')
        finally:
            self.close()
            self.server._connection_closed(self)


class SatelliteServer:
    """ESPHome API endpoint for one Billy voice satellite."""

    def __init__(
        self,
        identity: DeviceIdentity,
        listener: SatelliteListener,
        *,
        host: str = '0.0.0.0',
        port: int = DEFAULT_PORT,
        server_info: str = 'Billy',
        response_timeout: float = 5.0,
        write_timeout: float = 2.0,
        key_store=None,
        on_key_changed: Callable[[], None] | None = None,
    ) -> None:
        self.identity = identity
        self.listener = listener
        self.host = host
        self.port = port
        self.server_info = server_info
        self.response_timeout = response_timeout
        self.write_timeout = write_timeout
        self.key_store = key_store
        self.on_key_changed = on_key_changed
        self.active_wake_words = [word.id for word in identity.wake_words[:1]]
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[_Connection] = set()
        self._connection_tasks: set[asyncio.Task] = set()
        self._voice: _Connection | None = None
        self._run: PipelineRun | None = None
        # Home Assistant's side: a run it started and has not ended yet, and
        # until when the end of a run Billy stopped may still arrive.
        self._ha_run_open = False
        self._stale_end_until = 0.0

    # ----- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._accept, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info(f'HA satellite: listening on {self.host}:{self.port}')

    async def stop(self, timeout: float = 2.0) -> None:
        """Close the listener and every connection; safe to call repeatedly."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        self._finish_run('satellite stopped')
        for connection in list(self._connections):
            if not connection.closed:
                with contextlib.suppress(Exception):
                    connection.writer.write(
                        connection.codec.encode(MessageType.DISCONNECT_REQUEST, b'')
                    )
            connection.close()
        tasks = list(self._connection_tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
        if server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), timeout)

    @property
    def voice_connected(self) -> bool:
        return self._voice is not None and not self._voice.closed

    @property
    def voice_peer_host(self) -> str | None:
        """Address of the subscribed Home Assistant, for relative media URLs."""
        connection = self._voice
        peer = connection.peer if connection is not None else None
        return peer[0] if isinstance(peer, tuple) and peer else None

    @property
    def encryption_required(self) -> bool:
        return self.key_store is not None and self.key_store.get() is not None

    def enforce_key(self) -> int:
        """Close sessions the stored key no longer allows; returns how many.

        Used when the key changes outside a session (reset in the web UI, or
        set by Home Assistant): plaintext and zero-key sessions end once a key
        exists, and sessions under another key end when it changes or goes.
        """
        key = self.key_store.get() if self.key_store is not None else None
        closed = 0
        for connection in list(self._connections):
            codec = connection.codec
            if key is None:
                stale = connection.encrypted and not connection.provisioning
            elif connection.encrypted:
                stale = codec.key != key
            else:
                stale = True
            if stale and not connection.closed:
                connection.close()
                closed += 1
        return closed

    # ----- pipeline control -------------------------------------------------

    async def start_pipeline(
        self, *, wake_word_phrase: str = '', conversation_id: str = ''
    ) -> PipelineRun:
        """Ask Home Assistant to start an Assist run and wait until it accepts.

        Any previous run is ended first. Raises :class:`SatelliteUnavailable`
        when Home Assistant is absent, refuses, or does not answer in time.
        """
        connection = self._voice
        if connection is None or connection.closed:
            raise SatelliteUnavailable('Home Assistant is not connected')
        if self._run is not None:
            await self.stop_pipeline()
        run = PipelineRun()
        self._run = run
        sent = await connection.send(
            MessageType.VOICE_ASSISTANT_REQUEST,
            voice_assistant_request(
                start=True,
                conversation_id=conversation_id,
                wake_word_phrase=wake_word_phrase,
            ),
        )
        if not sent:
            self._finish_run('could not reach Home Assistant', run)
            raise SatelliteUnavailable('could not reach Home Assistant')
        try:
            response = await asyncio.wait_for(
                asyncio.shield(run.accepted), self.response_timeout
            )
        except TimeoutError:
            self._finish_run('Home Assistant did not answer', run)
            raise SatelliteUnavailable('Home Assistant did not answer') from None
        if response.error or response.port:
            # A non-zero port asks for UDP audio, which Billy did not offer.
            reason = 'pipeline refused' if response.error else 'UDP audio requested'
            self._finish_run(reason, run)
            raise SatelliteUnavailable(reason)
        return run

    async def send_audio(self, pcm: bytes) -> bool:
        """Send one microphone chunk; False once Home Assistant stops listening."""
        run, connection = self._run, self._voice
        if run is None or not run.mic_open or connection is None or not pcm:
            return False
        return await connection.send(
            MessageType.VOICE_ASSISTANT_AUDIO, voice_assistant_audio(pcm)
        )

    async def stop_pipeline(self) -> None:
        """End Billy's current run (button, cancellation, shutdown)."""
        run = self._run
        if run is None:
            return
        self._finish_run('stopped by Billy', run)
        if self._ha_run_open:
            self._stale_end_until = time.monotonic() + STALE_END_SECONDS
        connection = self._voice
        if connection is not None:
            await connection.send(
                MessageType.VOICE_ASSISTANT_REQUEST,
                voice_assistant_request(start=False),
            )

    def _finish_run(self, error: str | None, run: PipelineRun | None = None) -> None:
        run = run or self._run
        if run is None:
            return
        run._finish(error)
        if self._run is run:
            self._run = None

    # ----- connection handling ----------------------------------------------

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._server is None or len(self._connections) >= MAX_CONNECTIONS:
            writer.close()
            return
        connection = _Connection(self, reader, writer)
        self._connections.add(connection)
        task = asyncio.current_task()
        if task is not None:
            self._connection_tasks.add(task)
            task.add_done_callback(self._connection_tasks.discard)
        await connection.run()

    def _connection_closed(self, connection: _Connection) -> None:
        self._connections.discard(connection)
        if self._voice is connection:
            self._voice = None
            self._ha_run_open, self._stale_end_until = False, 0.0
            self._finish_run('Home Assistant disconnected')
            self._notify(self.listener.connection_changed, False)

    def _notify(self, callback: Callable, *args) -> None:
        try:
            callback(*args)
        except Exception:
            logger.exception('HA satellite: listener callback failed')

    async def _handle(
        self, connection: _Connection, message_type: int, payload: bytes
    ) -> None:
        fields = decode_message(payload)
        if connection.provisioning and message_type not in _PROVISIONING_MESSAGES:
            logger.debug(
                f'HA satellite: ignoring message {message_type} on a key setup session'
            )
            return
        if message_type == MessageType.PING_REQUEST:
            await connection.send(MessageType.PING_RESPONSE)
        elif message_type == MessageType.DISCONNECT_REQUEST:
            await connection.send(MessageType.DISCONNECT_RESPONSE)
            connection.close()
        elif message_type == MessageType.DISCONNECT_RESPONSE:
            connection.close()
        elif message_type == MessageType.AUTHENTICATION_REQUEST:
            await connection.send(MessageType.AUTHENTICATION_RESPONSE)
        elif message_type == MessageType.DEVICE_INFO_REQUEST:
            await connection.send(
                MessageType.DEVICE_INFO_RESPONSE,
                device_info_response(
                    self.identity,
                    self.server_info,
                    encryption_supported=self.key_store is not None,
                    encryption_provisionable=not self.encryption_required,
                ),
            )
        elif message_type == MessageType.NOISE_ENCRYPTION_SET_KEY_REQUEST:
            success = self._set_key(connection, get_bytes(fields, 1))
            await connection.send(
                MessageType.NOISE_ENCRYPTION_SET_KEY_RESPONSE, set_key_response(success)
            )
        elif message_type == MessageType.DEVICE_CAPABILITIES_REQUEST:
            await connection.send(
                MessageType.DEVICE_CAPABILITIES_RESPONSE,
                device_capabilities_response(self.identity),
            )
        elif message_type == MessageType.LIST_ENTITIES_REQUEST:
            await connection.send(MessageType.LIST_ENTITIES_DONE_RESPONSE)
        elif message_type == MessageType.SUBSCRIBE_VOICE_ASSISTANT_REQUEST:
            self._subscribe(connection, SubscribeVoiceAssistantRequest.parse(fields))
        elif message_type == MessageType.VOICE_ASSISTANT_RESPONSE:
            run = self._run
            if run is not None and connection is self._voice:
                response = VoiceAssistantResponse.parse(fields)
                if not run.accepted.done():
                    run.mic_open = not response.error and not response.port
                    run.accepted.set_result(response)
        elif message_type == MessageType.VOICE_ASSISTANT_EVENT_RESPONSE:
            if connection is self._voice:
                self._pipeline_event(VoiceAssistantEventResponse.parse(fields))
        elif message_type == MessageType.VOICE_ASSISTANT_AUDIO:
            if connection is self._voice:
                audio = VoiceAssistantAudio.parse(fields)
                self._notify(self.listener.tts_audio, audio.data, audio.end)
        elif message_type == MessageType.VOICE_ASSISTANT_TIMER_EVENT_RESPONSE:
            self._notify(
                self.listener.timer_event,
                VoiceAssistantTimerEventResponse.parse(fields),
            )
        elif message_type == MessageType.VOICE_ASSISTANT_ANNOUNCE_REQUEST:
            connection.spawn(
                self._announce(connection, VoiceAssistantAnnounceRequest.parse(fields))
            )
        elif message_type == MessageType.VOICE_ASSISTANT_CONFIGURATION_REQUEST:
            await connection.send(
                MessageType.VOICE_ASSISTANT_CONFIGURATION_RESPONSE,
                configuration_response(self.identity, self.active_wake_words),
            )
        elif message_type == MessageType.VOICE_ASSISTANT_SET_CONFIGURATION:
            known = {word.id for word in self.identity.wake_words}
            requested = parse_set_configuration(fields)
            selected = [word for word in requested if word in known]
            self.active_wake_words = selected[: self.identity.max_active_wake_words]
            self._notify(
                self.listener.active_wake_words_changed, list(self.active_wake_words)
            )
        else:
            # States, logs, services and the like: nothing to report yet.
            logger.debug(f'HA satellite: ignoring message type {message_type}')

    def _set_key(self, connection: _Connection, key_text: bytes) -> bool:
        """Store a key sent by Home Assistant; never over an unencrypted session."""
        if self.key_store is None or not connection.encrypted:
            logger.warning('HA satellite: refused an encryption key sent in plaintext')
            return False
        try:
            key = decode_psk(key_text)
            if key == ZERO_PSK:
                raise ValueError('the all-zero key is not a key')
            self.key_store.set(key)
        except (ValueError, OSError) as exc:
            logger.warning(f'HA satellite: could not store the encryption key: {exc}')
            return False
        logger.info('HA satellite: Home Assistant set the encryption key.', '🔐')
        # Close older unencrypted sessions once Home Assistant has the key.
        asyncio.get_running_loop().call_later(PLAINTEXT_GRACE_SECONDS, self.enforce_key)
        if self.on_key_changed is not None:
            self._notify(self.on_key_changed)
        return True

    def _subscribe(
        self, connection: _Connection, request: SubscribeVoiceAssistantRequest
    ) -> None:
        if not request.subscribe:
            if self._voice is connection:
                self._voice = None
                self._finish_run('Home Assistant unsubscribed')
                self._notify(self.listener.connection_changed, False)
            return
        api_audio = (
            VoiceAssistantSubscribeFlag.API_AUDIO
            | VoiceAssistantSubscribeFlag.API_AUDIO_AIOESPHOMEAPI
        )
        if not request.flags & api_audio:
            logger.warning(
                'HA satellite: Home Assistant did not offer API audio; '
                'UDP audio is not supported'
            )
            return
        previous, self._voice = self._voice, connection
        self._ha_run_open, self._stale_end_until = False, 0.0
        if previous is not None and previous is not connection:
            self._finish_run('Home Assistant reconnected')
            previous.close()
        self._notify(self.listener.connection_changed, True)

    def _pipeline_event(self, event: VoiceAssistantEventResponse) -> None:
        kind = event.event_type
        run = self._run
        if kind == VoiceAssistantEvent.RUN_START:
            self._ha_run_open = True
            self._stale_end_until = 0.0
            if run is not None:
                run.started = True
        elif kind in _RUN_ENDING_EVENTS and self._is_stale_end(run):
            if kind == VoiceAssistantEvent.RUN_END:
                self._ha_run_open = False
                self._stale_end_until = 0.0
            logger.verbose(f'HA satellite: ignored the end of an earlier run ({kind})')
            return
        if kind == VoiceAssistantEvent.RUN_END:
            self._ha_run_open = False
        if run is not None:
            if kind in _MIC_CLOSING_EVENTS:
                run.mic_open = False
            if kind in _RUN_ENDING_EVENTS:
                error = None
                if kind == VoiceAssistantEvent.ERROR:
                    error = event.data.get('message') or event.data.get('code')
                    error = error or 'pipeline error'
                elif not run.started and not run.accepted.done():
                    error = RUN_ENDED_EARLY
                self._finish_run(error, run)
        self._notify(self.listener.pipeline_event, event)

    def _is_stale_end(self, run: PipelineRun | None) -> bool:
        """Whether an ERROR or RUN_END belongs to a run Billy already stopped."""
        if run is not None and run.started:
            return False
        return time.monotonic() < self._stale_end_until

    async def _announce(
        self, connection: _Connection, request: VoiceAssistantAnnounceRequest
    ) -> None:
        try:
            success = bool(await self.listener.announce(request))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('HA satellite: announcement failed')
            success = False
        await connection.send(
            MessageType.VOICE_ASSISTANT_ANNOUNCE_FINISHED, announce_finished(success)
        )
