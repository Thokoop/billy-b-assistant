"""Minimal protobuf wire codec and plaintext ESPHome API framing.

Only the protobuf features the ESPHome voice messages use are supported:
unsigned varints (uint32, bool, enum), length-delimited fields (string,
bytes, nested messages) and fixed32 floats. Writing the codec by hand keeps
the satellite free of a protobuf runtime dependency in the ARM64 locks.
"""

import asyncio
import struct
from collections.abc import Iterable


WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LENGTH = 2
WIRE_FIXED32 = 5

PLAINTEXT_PREAMBLE = 0x00
NOISE_PREAMBLE = 0x01

# Voice frames are small (about 1 KiB of PCM); anything far beyond that is a
# broken or hostile peer and must not make Billy allocate unbounded memory.
MAX_FRAME_SIZE = 256 * 1024

_MAX_VARINT_BYTES = 10


class ProtocolError(Exception):
    """The peer sent bytes that are not a valid plaintext ESPHome API frame."""


Field = tuple[int, object]
Fields = dict[int, list[object]]


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError('negative varints are not supported')
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_varint(data: bytes, pos: int = 0) -> tuple[int, int]:
    result = 0
    for index in range(_MAX_VARINT_BYTES):
        if pos >= len(data):
            raise ProtocolError('truncated varint')
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return result, pos
    raise ProtocolError('varint is too long')


def _key(number: int, wire_type: int) -> bytes:
    return encode_varint((number << 3) | wire_type)


def encode_message(fields: Iterable[Field]) -> bytes:
    """Encode ``(field_number, value)`` pairs, omitting proto3 defaults.

    ``bool``/``int`` become varints, ``str``/``bytes`` length-delimited fields
    and ``float`` a fixed32. Nested messages are passed as encoded ``bytes``.
    A field may appear more than once to encode a repeated field.
    """
    out = bytearray()
    for number, value in fields:
        if value is None or isinstance(value, bool | int | float) and value == 0:
            continue
        if isinstance(value, bool | int):
            out += _key(number, WIRE_VARINT) + encode_varint(int(value))
        elif isinstance(value, float):
            out += _key(number, WIRE_FIXED32) + struct.pack('<f', value)
        elif isinstance(value, str | bytes | bytearray):
            raw = value.encode('utf-8') if isinstance(value, str) else bytes(value)
            if not raw:
                continue
            out += _key(number, WIRE_LENGTH) + encode_varint(len(raw)) + raw
        else:
            raise TypeError(f'unsupported field type for {number}: {type(value)}')
    return bytes(out)


def decode_message(data: bytes) -> Fields:
    """Decode into ``{field_number: [raw values]}``; unknown fields are kept."""
    fields: Fields = {}
    pos = 0
    while pos < len(data):
        key, pos = decode_varint(data, pos)
        number, wire_type = key >> 3, key & 0x07
        if number == 0:
            raise ProtocolError('field number 0 is invalid')
        if wire_type == WIRE_VARINT:
            value, pos = decode_varint(data, pos)
        elif wire_type == WIRE_LENGTH:
            length, pos = decode_varint(data, pos)
            if pos + length > len(data):
                raise ProtocolError('truncated length-delimited field')
            value = bytes(data[pos : pos + length])
            pos += length
        elif wire_type in (WIRE_FIXED32, WIRE_FIXED64):
            size = 4 if wire_type == WIRE_FIXED32 else 8
            if pos + size > len(data):
                raise ProtocolError('truncated fixed-width field')
            value = bytes(data[pos : pos + size])
            pos += size
        else:
            raise ProtocolError(f'unsupported wire type {wire_type}')
        fields.setdefault(number, []).append(value)
    return fields


def get_int(fields: Fields, number: int, default: int = 0) -> int:
    values = fields.get(number)
    return values[-1] if values and isinstance(values[-1], int) else default


def get_bool(fields: Fields, number: int) -> bool:
    return bool(get_int(fields, number))


def get_bytes(fields: Fields, number: int) -> bytes:
    values = fields.get(number)
    return values[-1] if values and isinstance(values[-1], bytes) else b''


def get_str(fields: Fields, number: int) -> str:
    return get_bytes(fields, number).decode('utf-8', errors='replace')


def get_float(fields: Fields, number: int, default: float = 0.0) -> float:
    raw = get_bytes(fields, number)
    return struct.unpack('<f', raw)[0] if len(raw) == 4 else default


def get_repeated_bytes(fields: Fields, number: int) -> list[bytes]:
    return [value for value in fields.get(number, []) if isinstance(value, bytes)]


def get_repeated_str(fields: Fields, number: int) -> list[str]:
    return [
        value.decode('utf-8', errors='replace')
        for value in get_repeated_bytes(fields, number)
    ]


def encode_frame(message_type: int, payload: bytes) -> bytes:
    return (
        bytes((PLAINTEXT_PREAMBLE,))
        + encode_varint(len(payload))
        + encode_varint(message_type)
        + payload
    )


async def _read_varint(reader: asyncio.StreamReader) -> int:
    result = 0
    for index in range(_MAX_VARINT_BYTES):
        byte = (await reader.readexactly(1))[0]
        result |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return result
    raise ProtocolError('varint is too long')


async def read_frame(
    reader: asyncio.StreamReader,
    max_size: int = MAX_FRAME_SIZE,
    *,
    preamble: int | None = None,
) -> tuple[int, bytes]:
    """Read one plaintext frame; ``IncompleteReadError`` means the peer left.

    Pass ``preamble`` when its byte was already read to choose the transport.
    """
    if preamble is None:
        preamble = (await reader.readexactly(1))[0]
    if preamble == NOISE_PREAMBLE:
        raise ProtocolError('encrypted (Noise) connections are not enabled')
    if preamble != PLAINTEXT_PREAMBLE:
        raise ProtocolError(f'unexpected frame preamble 0x{preamble:02x}')
    size = await _read_varint(reader)
    if size > max_size:
        raise ProtocolError(f'frame of {size} bytes exceeds {max_size}')
    message_type = await _read_varint(reader)
    payload = await reader.readexactly(size) if size else b''
    return message_type, payload


def encode_noise_frame(payload: bytes) -> bytes:
    """A Noise transport frame: 0x01, big-endian 16-bit length, payload."""
    if len(payload) > 0xFFFF:
        raise ValueError('Noise frame too large')
    return bytes((NOISE_PREAMBLE,)) + len(payload).to_bytes(2, 'big') + payload


async def read_noise_frame(
    reader: asyncio.StreamReader, *, preamble: int | None = None
) -> bytes:
    if preamble is None:
        preamble = (await reader.readexactly(1))[0]
    if preamble != NOISE_PREAMBLE:
        raise ProtocolError('expected an encrypted (Noise) frame')
    size = int.from_bytes(await reader.readexactly(2), 'big')
    return await reader.readexactly(size) if size else b''
