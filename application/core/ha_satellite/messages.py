"""ESPHome API message IDs and the voice-satellite subset Billy speaks.

IDs, field numbers and enums follow esphome/components/api/api.proto. Only
messages a voice satellite needs are modeled; other requests are ignored.
"Server" in that file is the device (Billy) and "client" is Home Assistant.
"""

from dataclasses import dataclass, field
from enum import IntEnum, IntFlag

from .wire import (
    Fields,
    decode_message,
    encode_message,
    get_bool,
    get_bytes,
    get_int,
    get_repeated_bytes,
    get_repeated_str,
    get_str,
)


# API 1.10 is the first version with voice_assistant_feature_flags. Clients
# read capabilities from DeviceInfoResponse below 1.15, so Billy does not have
# to implement every message change made since then.
API_VERSION_MAJOR = 1
API_VERSION_MINOR = 10


class MessageType(IntEnum):
    HELLO_REQUEST = 1
    HELLO_RESPONSE = 2
    # Removed in ESPHome 2026.1; answered for older Home Assistant clients.
    AUTHENTICATION_REQUEST = 3
    AUTHENTICATION_RESPONSE = 4
    DISCONNECT_REQUEST = 5
    DISCONNECT_RESPONSE = 6
    PING_REQUEST = 7
    PING_RESPONSE = 8
    DEVICE_INFO_REQUEST = 9
    DEVICE_INFO_RESPONSE = 10
    LIST_ENTITIES_REQUEST = 11
    LIST_ENTITIES_DONE_RESPONSE = 19
    SUBSCRIBE_STATES_REQUEST = 20
    SUBSCRIBE_LOGS_REQUEST = 28
    SUBSCRIBE_HOMEASSISTANT_SERVICES_REQUEST = 34
    SUBSCRIBE_HOME_ASSISTANT_STATES_REQUEST = 38
    SUBSCRIBE_VOICE_ASSISTANT_REQUEST = 89
    VOICE_ASSISTANT_REQUEST = 90
    VOICE_ASSISTANT_RESPONSE = 91
    VOICE_ASSISTANT_EVENT_RESPONSE = 92
    VOICE_ASSISTANT_AUDIO = 106
    VOICE_ASSISTANT_TIMER_EVENT_RESPONSE = 115
    VOICE_ASSISTANT_ANNOUNCE_REQUEST = 119
    VOICE_ASSISTANT_ANNOUNCE_FINISHED = 120
    VOICE_ASSISTANT_CONFIGURATION_REQUEST = 121
    VOICE_ASSISTANT_CONFIGURATION_RESPONSE = 122
    VOICE_ASSISTANT_SET_CONFIGURATION = 123
    DEVICE_CAPABILITIES_REQUEST = 149
    NOISE_ENCRYPTION_SET_KEY_REQUEST = 124
    NOISE_ENCRYPTION_SET_KEY_RESPONSE = 125
    DEVICE_CAPABILITIES_RESPONSE = 150


class VoiceAssistantFeature(IntFlag):
    """DeviceInfoResponse.voice_assistant_feature_flags (aioesphomeapi)."""

    VOICE_ASSISTANT = 1
    SPEAKER = 2
    API_AUDIO = 4
    TIMERS = 8
    ANNOUNCE = 16
    START_CONVERSATION = 32


class VoiceAssistantSubscribeFlag(IntFlag):
    NONE = 0
    # api.proto defines API audio as 1, but aioesphomeapi (Home Assistant's
    # client) sends 1 << 2. Accept either, as ESPHome firmware does.
    API_AUDIO = 1
    API_AUDIO_AIOESPHOMEAPI = 1 << 2
    WAKE_WORD_AUDIO = 1 << 3


class VoiceAssistantRequestFlag(IntFlag):
    NONE = 0
    USE_VAD = 1
    USE_WAKE_WORD = 2


class VoiceAssistantEvent(IntEnum):
    ERROR = 0
    RUN_START = 1
    RUN_END = 2
    STT_START = 3
    STT_END = 4
    INTENT_START = 5
    INTENT_END = 6
    TTS_START = 7
    TTS_END = 8
    WAKE_WORD_START = 9
    WAKE_WORD_END = 10
    STT_VAD_START = 11
    STT_VAD_END = 12
    WAKE_WORD_REJECTED = 13
    TTS_STREAM_START = 98
    TTS_STREAM_END = 99
    INTENT_PROGRESS = 100


class VoiceAssistantTimerEvent(IntEnum):
    STARTED = 0
    UPDATED = 1
    CANCELLED = 2
    FINISHED = 3


@dataclass(frozen=True)
class WakeWord:
    id: str
    wake_word: str
    trained_languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeviceIdentity:
    """What Home Assistant shows for the satellite device."""

    name: str
    friendly_name: str
    mac_address: str
    model: str = 'Billy'
    manufacturer: str = 'ThingsFromThom'
    project_version: str = ''
    feature_flags: int = (
        VoiceAssistantFeature.VOICE_ASSISTANT
        | VoiceAssistantFeature.SPEAKER
        | VoiceAssistantFeature.API_AUDIO
        | VoiceAssistantFeature.ANNOUNCE
        | VoiceAssistantFeature.START_CONVERSATION
        | VoiceAssistantFeature.TIMERS
    )
    wake_words: tuple[WakeWord, ...] = ()
    max_active_wake_words: int = 1

    @property
    def project_name(self) -> str:
        """ESPHome's ``manufacturer.model`` project name.

        Home Assistant's ESPHome integration shows these two parts as the
        device's model and manufacturer, ahead of the separate fields.
        """
        manufacturer = self.manufacturer.replace('.', '-') or 'ThingsFromThom'
        model = self.model.replace('.', '-') or 'Billy'
        return f'{manufacturer}.{model}'


# ----- Home Assistant -> Billy ------------------------------------------------


@dataclass(frozen=True)
class HelloRequest:
    client_info: str
    api_version_major: int
    api_version_minor: int

    @classmethod
    def parse(cls, fields: Fields) -> 'HelloRequest':
        return cls(get_str(fields, 1), get_int(fields, 2), get_int(fields, 3))


@dataclass(frozen=True)
class SubscribeVoiceAssistantRequest:
    subscribe: bool
    flags: int

    @classmethod
    def parse(cls, fields: Fields) -> 'SubscribeVoiceAssistantRequest':
        return cls(get_bool(fields, 1), get_int(fields, 2))


@dataclass(frozen=True)
class VoiceAssistantResponse:
    port: int
    error: bool

    @classmethod
    def parse(cls, fields: Fields) -> 'VoiceAssistantResponse':
        return cls(get_int(fields, 1), get_bool(fields, 2))


@dataclass(frozen=True)
class VoiceAssistantEventResponse:
    event_type: int
    data: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, fields: Fields) -> 'VoiceAssistantEventResponse':
        data = {}
        for raw in get_repeated_bytes(fields, 2):
            item = decode_message(raw)
            data[get_str(item, 1)] = get_str(item, 2)
        return cls(get_int(fields, 1), data)


@dataclass(frozen=True)
class VoiceAssistantAudio:
    data: bytes
    end: bool

    @classmethod
    def parse(cls, fields: Fields) -> 'VoiceAssistantAudio':
        return cls(get_bytes(fields, 1), get_bool(fields, 2))


@dataclass(frozen=True)
class VoiceAssistantTimerEventResponse:
    event_type: int
    timer_id: str
    name: str
    total_seconds: int
    seconds_left: int
    is_active: bool

    @classmethod
    def parse(cls, fields: Fields) -> 'VoiceAssistantTimerEventResponse':
        return cls(
            get_int(fields, 1),
            get_str(fields, 2),
            get_str(fields, 3),
            get_int(fields, 4),
            get_int(fields, 5),
            get_bool(fields, 6),
        )


@dataclass(frozen=True)
class VoiceAssistantAnnounceRequest:
    media_id: str
    text: str
    preannounce_media_id: str
    start_conversation: bool

    @classmethod
    def parse(cls, fields: Fields) -> 'VoiceAssistantAnnounceRequest':
        return cls(
            get_str(fields, 1),
            get_str(fields, 2),
            get_str(fields, 3),
            get_bool(fields, 4),
        )


def parse_set_configuration(fields: Fields) -> list[str]:
    return get_repeated_str(fields, 1)


# ----- Billy -> Home Assistant ------------------------------------------------


def hello_response(identity: DeviceIdentity, server_info: str) -> bytes:
    return encode_message([
        (1, API_VERSION_MAJOR),
        (2, API_VERSION_MINOR),
        (3, server_info[:32]),
        (4, identity.name[:31]),
    ])


def device_info_response(
    identity: DeviceIdentity,
    server_info: str,
    *,
    encryption_supported: bool = False,
    encryption_provisionable: bool = False,
) -> bytes:
    """``encryption_supported``: Billy accepts a key from Home Assistant;
    ``encryption_provisionable``: no key yet, so the all-zero key is accepted
    for the short Noise session in which Home Assistant sends one."""
    return encode_message([
        (2, identity.name[:31]),
        (3, identity.mac_address),
        (4, server_info[:32]),
        (6, identity.model[:127]),
        (8, identity.project_name[:127]),
        (9, identity.project_version[:127]),
        (12, identity.manufacturer[:20]),
        (13, identity.friendly_name[:120]),
        (17, int(identity.feature_flags)),
        (19, encryption_supported),
        (26, encryption_supported and encryption_provisionable),
    ])


def set_key_response(success: bool) -> bytes:
    return encode_message([(1, success)])


def device_capabilities_response(identity: DeviceIdentity) -> bytes:
    voice = encode_message([(1, int(identity.feature_flags))])
    return encode_message([(2, voice)])


def voice_assistant_request(
    *,
    start: bool,
    conversation_id: str = '',
    flags: int = VoiceAssistantRequestFlag.USE_VAD,
    wake_word_phrase: str = '',
) -> bytes:
    return encode_message([
        (1, start),
        (2, conversation_id),
        (3, int(flags)),
        (5, wake_word_phrase),
    ])


def voice_assistant_audio(data: bytes, *, end: bool = False) -> bytes:
    return encode_message([(1, data), (2, end)])


def announce_finished(success: bool) -> bytes:
    return encode_message([(1, success)])


def configuration_response(
    identity: DeviceIdentity, active_wake_words: list[str]
) -> bytes:
    fields = []
    for word in identity.wake_words:
        fields.append((
            1,
            encode_message(
                [(1, word.id), (2, word.wake_word)]
                + [(3, language) for language in word.trained_languages]
            ),
        ))
    fields += [(2, word_id) for word_id in active_wake_words]
    fields.append((3, identity.max_active_wake_words))
    return encode_message(fields)
