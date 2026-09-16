"""Build an installable HACS archive without modifying the checked-out source."""
import argparse
import json
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED


def build(source, output, version):
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version):
        raise ValueError('Expected YYYY.M.D.run_number version')
    source, output = Path(source), Path(output)
    manifest = json.loads((source / 'manifest.json').read_text())
    if manifest['domain'] != 'purethink':
        raise ValueError('Unexpected integration domain')
    manifest['version'] = version
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob('*')):
            if path.is_file() and path.suffix in {'.py', '.json', '.yaml', '.png', '.svg'} and not any(
                part.startswith('.') or part == '__pycache__' for part in path.relative_to(source).parts
            ):
                name = path.relative_to(source).as_posix()
                data = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode() if name == 'manifest.json' else path.read_bytes()
                archive.writestr(name, data)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', default='dist/purethink.zip')
    parser.add_argument('--source', default='custom_components/purethink')
    args = parser.parse_args()
    build(args.source, args.output, args.version)
