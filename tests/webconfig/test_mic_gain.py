"""Mic gain without a USB microphone is unavailable, not a server error."""

from types import SimpleNamespace
from unittest.mock import Mock

from conftest import definitions
from flask import Blueprint, Flask, jsonify, request


def client(card_index, controls=''):
    subprocess = SimpleNamespace(
        check_output=Mock(return_value=controls), check_call=Mock()
    )
    logger = Mock()
    bp = Blueprint('audio', __name__)
    namespace = {
        'bp': bp,
        'jsonify': jsonify,
        'request': request,
        'subprocess': subprocess,
        'logger': logger,
        're': __import__('re'),
        'core_config': SimpleNamespace(MIC_GAIN='max'),
        'get_usb_capture_card_index': lambda: card_index,
        'save_configured_mic_gain': Mock(),
    }
    definitions(
        'application/webconfig/app/routes/audio.py',
        [
            'get_mic_gain_numid',
            'parse_amixer_control_range',
            'parse_amixer_control_value',
            'mic_gain',
        ],
        namespace,
    )
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app.test_client(), subprocess, logger


def test_without_a_usb_microphone_gain_is_unavailable():
    # A device with only a loopback/Bluetooth microphone (no USB capture card).
    http, subprocess, logger = client(None)
    response = http.get('/mic-gain')
    assert response.status_code == 200
    assert response.get_json() == {
        'available': False,
        'reason': 'no_usb_microphone',
    }
    # No "amixer -c None" call and no warning in the log.
    subprocess.check_output.assert_not_called()
    logger.warning.assert_not_called()
    response = http.post('/mic-gain', json={'value': 4})
    assert response.status_code == 409
    assert response.get_json()['available'] is False
    subprocess.check_call.assert_not_called()


def test_a_card_without_a_gain_control_is_unavailable():
    http, _, _ = client(2, controls="numid=3,iface=MIXER,name='Speaker Volume'\n")
    assert http.get('/mic-gain').get_json() == {
        'available': False,
        'reason': 'no_gain_control',
    }


def test_usb_microphone_gain_is_reported():
    controls = "numid=7,iface=MIXER,name='Mic Capture Volume'\n"
    http, subprocess, _ = client(2, controls=controls)
    subprocess.check_output.side_effect = [
        controls,
        "  ; type=INTEGER,access=rw---R--,values=1,min=0,max=16,step=0\n  : values=12\n",
    ]
    data = http.get('/mic-gain').get_json()
    assert data['available'] is True
    assert (data['gain'], data['min'], data['max']) == (12, 0, 16)
