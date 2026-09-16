"""Compare complete command payloads with frozen results from both source repos."""
import hashlib
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from custom_components.purethink.protocol import generate_command

DEVICE = 'DIV01-ABCDEF'
OPERATIONS = [
    {}, {'mode': 'on'}, {'mode': 'off'},
    *({'mode': mode} for mode in ['Manual', 'Auto', 'Sleep 1', 'Sleep 2', 'Sleep 3']),
    *({'fan_speed': speed} for speed in range(6)),
    *({'fan_mode': mode} for mode in ['환기 꺼짐', '배기', '흡기', '흡/배기']),
    *({'pressure_mode': mode} for mode in ['정압', '양압', '음압']),
    {'filter_reset': 'prefilter'}, {'filter_reset': 'hepafilter'},
]
KEYS = ['power', 'fan_speed', 'ai_mode', 'sleep_mode', 'pressure_mode', 'fan_in', 'fan_out']


def states():
    for values in itertools.product(range(2), range(6), range(2), range(4), range(3), range(2), range(2)):
        yield dict(zip(KEYS, values))


def digest(generator, operation, upstream=False):
    translated = dict(operation)
    if upstream:
        for key in ['mode', 'fan_mode']:
            if key in translated:
                translated[key] = {'Manual': 'Normal', 'Auto': 'AI Mode',
                    '환기 꺼짐': '흡기Off-배기Off', '배기': '흡기Off-배기On',
                    '흡기': '흡기On-배기Off', '흡/배기': '흡기On-배기On'}.get(translated[key], translated[key])
    result = hashlib.sha256()
    for state in states():
        hass = SimpleNamespace(data={'purethink': {'_devices': {DEVICE: 'entry'},
            'entry': {'state': state, 'command_topic': f'/things/{DEVICE}/shadow'}}})
        contents = json.loads(generator(DEVICE, hass, **translated))['contents']
        packet = bytes.fromhex(contents)
        assert len(packet) == 23
        assert packet[:4] == bytes.fromhex('A8A81722')
        assert int.from_bytes(packet[-2:], 'big') == 393 + sum(packet[4:18])
        result.update(packet)
    return result.hexdigest()


@pytest.mark.parametrize('index,operation', list(enumerate(OPERATIONS)))
def test_command_equivalence(index, operation):
    expected = json.loads((Path(__file__).parent / 'fixtures' / 'protocol_hashes.json').read_text())
    actual = digest(generate_command, operation)
    assert actual == expected['local']['digests'][index]
    assert actual == expected['upstream']['digests'][index]
