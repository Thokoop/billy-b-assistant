"""Sample-rate bridges between Billy's audio path and Home Assistant.

Home Assistant's ESPHome satellite protocol carries 16 kHz mono 16-bit PCM in
both directions. Billy captures at the microphone's own rate and plays 24 kHz
mono chunks (the playback worker upsamples those to the 48 kHz output).
"""

from math import gcd

import numpy as np
from scipy.signal import resample_poly


ASSIST_RATE = 16000


def mic_to_assist_pcm(indata, mic_rate: int) -> bytes:
    """One capture block (frames x channels int16) as 16 kHz mono PCM bytes."""
    samples = np.asarray(indata)
    if samples.ndim == 2:
        # The first channel is the microphone on Billy's stereo USB adapters.
        samples = samples[:, 0]
    samples = samples.astype(np.float32)
    rate = int(mic_rate or ASSIST_RATE)
    if rate != ASSIST_RATE and samples.size:
        divisor = gcd(rate, ASSIST_RATE)
        samples = resample_poly(samples, ASSIST_RATE // divisor, rate // divisor)
    return np.clip(np.rint(samples), -32768, 32767).astype('<i2').tobytes()


class StreamResampler:
    """Continuous linear resampling of streamed int16 PCM chunks.

    Chunk boundaries do not produce clicks: the last input sample of a chunk
    is carried into the next one, and output positions are counted on the
    whole stream with integer arithmetic so they never drift.
    """

    def __init__(self, in_rate: int, out_rate: int) -> None:
        divisor = gcd(in_rate, out_rate)
        self._in = in_rate // divisor
        self._out = out_rate // divisor
        self._consumed = 0  # input samples seen before the current chunk
        self._next_output = 0  # index of the next output sample
        self._previous: float | None = None
        self._pending = b''

    def process(self, pcm: bytes) -> bytes:
        data = self._pending + pcm
        usable = len(data) - len(data) % 2
        self._pending = data[usable:]
        if not usable:
            return b''
        chunk = np.frombuffer(data[:usable], dtype='<i2').astype(np.float64)
        last_index = self._consumed + len(chunk) - 1
        last_output = (last_index * self._out) // self._in
        outputs = np.arange(self._next_output, last_output + 1, dtype=np.int64)
        positions = outputs * self._in / self._out
        if self._previous is None:
            base, buffer = self._consumed, chunk
        else:
            base = self._consumed - 1
            buffer = np.concatenate(([self._previous], chunk))
        result = np.interp(positions - base, np.arange(len(buffer)), buffer)
        self._previous = float(chunk[-1])
        self._consumed += len(chunk)
        self._next_output = last_output + 1
        return np.clip(np.rint(result), -32768, 32767).astype('<i2').tobytes()
