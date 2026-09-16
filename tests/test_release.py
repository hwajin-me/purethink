"""Release artifact validation: installable layout and isolated manifest version."""
import json
from pathlib import Path
from zipfile import ZipFile
import pytest
from scripts.build_release import build


def test_release_archive(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'custom_components/purethink'
    original = (source / 'manifest.json').read_bytes()
    output = build(source, tmp_path / 'dist/purethink.zip', '2026.9.15.42')
    with ZipFile(output) as archive:
        names = archive.namelist()
        assert {'__init__.py', 'bridge.py', 'sensor.py', 'config_flow.py', 'translations/ko.json'} <= set(names)
        assert json.loads(archive.read('manifest.json'))['version'] == '2026.9.15.42'
        assert not any('__pycache__' in n or n.startswith('custom_components/') for n in names)
        for name in names:
            if name != 'manifest.json':
                assert archive.read(name) == (source / name).read_bytes()
    assert (source / 'manifest.json').read_bytes() == original
    with pytest.raises(ValueError):
        build(source, output, '../bad')
    (tmp_path / 'manifest.json').write_text('{"domain":"other"}')
    with pytest.raises(ValueError):
        build(tmp_path, output, '2026.9.15.42')


def test_release_workflow_and_hacs_version_contract():
    import yaml
    from awesomeversion import AwesomeVersion
    root=Path(__file__).resolve().parents[1]
    workflow=yaml.load((root/'.github/workflows/release.yml').read_text(),Loader=yaml.BaseLoader)
    assert workflow['on']['push']['branches']==['main']
    assert workflow['on']['pull_request']['branches']==['main']
    assert workflow['jobs']['release']['needs']=='test'
    assert workflow['jobs']['release']['permissions']['contents']=='write'
    test_steps=workflow['jobs']['test']['steps']
    assert any('--cov-fail-under=100' in step.get('run','') for step in test_steps)
    assert any(step.get('env',{}).get('PURETHINK_LIVE_MQTT')=='1' for step in test_steps)
    hacs=json.loads((root/'hacs.json').read_text())
    assert hacs['zip_release'] and hacs['filename']=='purethink.zip'
    assert AwesomeVersion('v2026.9.15.42')==AwesomeVersion('2026.9.15.42')
    assert AwesomeVersion('2026.9.15.43')>AwesomeVersion('2026.9.15.42')>AwesomeVersion('1.8.2')
