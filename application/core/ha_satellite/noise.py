"""Noise_NNpsk0_25519_ChaChaPoly_SHA256, as the ESPHome native API uses it.

Implements the Noise Protocol Framework (revision 34) for exactly the one
handshake pattern ESPHome uses::

    NNpsk0:
      -> psk, e
      <- e, ee

with the prologue ``NoiseAPIInit\\0\\0``. The pre-shared key is the device's
32-byte API encryption key. Primitives come from ``cryptography`` (X25519 and
ChaCha20-Poly1305); hashing uses the standard library.

Billy is normally the responder. The initiator side exists for tests and for
the local probes.
"""

import base64
import binascii
import hashlib
import hmac

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


PROTOCOL_NAME = b'Noise_NNpsk0_25519_ChaChaPoly_SHA256'
PROLOGUE = b'NoiseAPIInit\x00\x00'
KEY_SIZE = 32
DH_SIZE = 32
TAG_SIZE = 16
ZERO_PSK = bytes(KEY_SIZE)
MAX_NONCE = 2**64 - 1


class NoiseError(Exception):
    """A handshake or transport message failed authentication."""


def decode_psk(text: str | bytes) -> bytes:
    """A base64 API encryption key as its raw 32 bytes (ValueError if not)."""
    try:
        raw = binascii.a2b_base64(text if isinstance(text, bytes) else text.encode())
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise ValueError('The encryption key is not valid base64') from exc
    if len(raw) != KEY_SIZE:
        raise ValueError('The encryption key must be 32 bytes (44 base64 chars)')
    return raw


def encode_psk(raw: bytes) -> str:
    return base64.b64encode(raw).decode('ascii')


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hkdf(chaining_key: bytes, material: bytes, outputs: int) -> list[bytes]:
    temp = _hmac(chaining_key, material)
    out, previous = [], b''
    for index in range(1, outputs + 1):
        previous = _hmac(temp, previous + bytes((index,)))
        out.append(previous)
    return out


def _nonce(counter: int) -> bytes:
    return b'\x00\x00\x00\x00' + counter.to_bytes(8, 'little')


class CipherState:
    """One direction of an established session (or a handshake cipher)."""

    def __init__(self, key: bytes | None = None) -> None:
        self._aead = ChaCha20Poly1305(key) if key is not None else None
        self._counter = 0

    @property
    def has_key(self) -> bool:
        return self._aead is not None

    def _next_nonce(self) -> bytes:
        if self._counter >= MAX_NONCE:
            raise NoiseError('nonce exhausted')
        nonce = _nonce(self._counter)
        self._counter += 1
        return nonce

    def encrypt(self, plaintext: bytes, ad: bytes = b'') -> bytes:
        if self._aead is None:
            return plaintext
        return self._aead.encrypt(self._next_nonce(), plaintext, ad)

    def decrypt(self, ciphertext: bytes, ad: bytes = b'') -> bytes:
        if self._aead is None:
            return ciphertext
        nonce = _nonce(self._counter)
        try:
            plaintext = self._aead.decrypt(nonce, ciphertext, ad)
        except InvalidTag:
            # The nonce is only consumed by a message that authenticates.
            raise NoiseError('authentication failed') from None
        self._counter += 1
        return plaintext


class _SymmetricState:
    def __init__(self) -> None:
        if len(PROTOCOL_NAME) <= hashlib.sha256().digest_size:
            self.h = PROTOCOL_NAME.ljust(32, b'\x00')
        else:
            self.h = hashlib.sha256(PROTOCOL_NAME).digest()
        self.ck = self.h
        self.cipher = CipherState()

    def mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def mix_key(self, material: bytes) -> None:
        self.ck, key = _hkdf(self.ck, material, 2)
        self.cipher = CipherState(key)

    def mix_key_and_hash(self, material: bytes) -> None:
        self.ck, temp_h, key = _hkdf(self.ck, material, 3)
        self.mix_hash(temp_h)
        self.cipher = CipherState(key)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.cipher.encrypt(plaintext, self.h)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.cipher.decrypt(ciphertext, self.h)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple[CipherState, CipherState]:
        first, second = _hkdf(self.ck, b'', 2)
        return CipherState(first), CipherState(second)


def _public_bytes(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _dh(private: X25519PrivateKey, public: bytes) -> bytes:
    try:
        return private.exchange(X25519PublicKey.from_public_bytes(public))
    except ValueError as exc:  # low-order point: an all-zero shared secret
        raise NoiseError('invalid ephemeral key') from exc


class NoiseHandshake:
    """One NNpsk0 handshake; ``split()`` yields (send, receive) ciphers."""

    def __init__(
        self,
        psk: bytes,
        *,
        initiator: bool = False,
        ephemeral: X25519PrivateKey | None = None,
    ) -> None:
        if len(psk) != KEY_SIZE:
            raise ValueError('psk must be 32 bytes')
        self.initiator = initiator
        self._psk = psk
        self._ephemeral = ephemeral or X25519PrivateKey.generate()
        self._remote_ephemeral: bytes | None = None
        self._state = _SymmetricState()
        self._state.mix_hash(PROLOGUE)
        self._step = 0

    def _write_e(self) -> bytes:
        public = _public_bytes(self._ephemeral)
        self._state.mix_hash(public)
        self._state.mix_key(public)  # psk handshakes mix e into the key too
        return public

    def _read_e(self, message: bytes) -> bytes:
        if len(message) < DH_SIZE:
            raise NoiseError('handshake message too short')
        self._remote_ephemeral = bytes(message[:DH_SIZE])
        self._state.mix_hash(self._remote_ephemeral)
        self._state.mix_key(self._remote_ephemeral)
        return message[DH_SIZE:]

    # -> psk, e
    def write_message_1(self, payload: bytes = b'') -> bytes:
        assert self.initiator and self._step == 0
        self._state.mix_key_and_hash(self._psk)
        out = self._write_e() + self._state.encrypt_and_hash(payload)
        self._step = 1
        return out

    def read_message_1(self, message: bytes) -> bytes:
        assert not self.initiator and self._step == 0
        self._state.mix_key_and_hash(self._psk)
        rest = self._read_e(message)
        payload = self._state.decrypt_and_hash(rest)
        self._step = 1
        return payload

    # <- e, ee
    def write_message_2(self, payload: bytes = b'') -> bytes:
        assert not self.initiator and self._step == 1
        out = self._write_e()
        self._state.mix_key(_dh(self._ephemeral, self._remote_ephemeral))
        out += self._state.encrypt_and_hash(payload)
        self._step = 2
        return out

    def read_message_2(self, message: bytes) -> bytes:
        assert self.initiator and self._step == 1
        rest = self._read_e(message)
        self._state.mix_key(_dh(self._ephemeral, self._remote_ephemeral))
        payload = self._state.decrypt_and_hash(rest)
        self._step = 2
        return payload

    @property
    def handshake_hash(self) -> bytes:
        return self._state.h

    def split(self) -> tuple[CipherState, CipherState]:
        """(send, receive) transport ciphers for this side."""
        if self._step != 2:
            raise NoiseError('handshake not complete')
        initiator_to_responder, responder_to_initiator = self._state.split()
        if self.initiator:
            return initiator_to_responder, responder_to_initiator
        return responder_to_initiator, initiator_to_responder
