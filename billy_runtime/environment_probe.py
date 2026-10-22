"""Hardware-free verification run by the candidate environment's own Python."""

import importlib
import os
import subprocess
import sys
import tempfile


def dependency_errors(output, python_version):
    # Upstream 0.6.0 unconditionally declares TFLite on Linux. Billy uses ONNX
    # on 3.13, where TFLite has no wheel. Do not ignore ANY other pip check error.
    allowed = (
        'openwakeword 0.6.0 requires tflite-runtime, which is not installed.'
        if python_version == (3, 13)
        else None
    )
    return [
        line
        for line in output.splitlines()
        if line and line != allowed and line != 'No broken requirements found.'
    ]


def verify():
    check = subprocess.run(
        [sys.executable, '-m', 'pip', '--isolated', 'check'],
        capture_output=True,
        text=True,
        timeout=60,
    )
    errors = dependency_errors(check.stdout, sys.version_info[:2])
    if check.returncode not in {0, 1} or errors or check.stderr.strip():
        raise RuntimeError(
            'Dependency validation failed: ' + check.stdout + check.stderr
        )
    if check.returncode and not check.stdout.strip():
        raise RuntimeError('Dependency validation returned no diagnostic')
    for module in (
        'numpy',
        'scipy.signal',
        'sounddevice',
        'websockets',
        'openai',
        'aiohttp',
        'flask',
        'flask_sock',
        'simple_websocket',
        'dotenv',
        'dbus_next',
        'onnxruntime',
        'openwakeword',
        'webrtcvad',
        'lgpio',
    ):
        importlib.import_module(module)
    # No audio device is opened. Verify the same 48 kHz/10 ms processor used
    # by calls, and the 24 kHz format used by Realtime.
    from aec_audio_processing import AudioProcessor

    for rate in (24000, 48000):
        processor = AudioProcessor(
            enable_aec=True, enable_ns=True, enable_agc=True, enable_vad=False
        )
        processor.set_stream_format(rate, 1, rate, 1)
        processor.set_reverse_stream_format(rate, 1)
        processor.process_stream(b'\0' * (rate // 100) * 2)
    print('Dependency imports and AEC format checks passed')


if __name__ == '__main__':
    # Also safe when invoked directly, outside the preparation command.
    with tempfile.TemporaryDirectory(prefix='billy-env-probe-') as directory:
        os.chdir(directory)
        verify()
