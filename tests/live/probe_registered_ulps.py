"""Live registered-ULP smoke check: read, preview (never apply), and a no-op legacy ULP."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import uuid
import xml.etree.ElementTree as ET

from fusion_mcp import custom_ulp as M
from fusion_mcp.addin.FusionElectronicsMCP import ulp_support as U
from fusion_mcp import server as S


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source_dir = Path(U.__file__).parent / 'ulps'
    names = []
    prefix = 'smoke-' + uuid.uuid4().hex[:8]
    report = {}
    with S.session.bridge.lock.hold('registered ULP live smoke'), tempfile.TemporaryDirectory() as temporary:
        before = M.snapshot(S.session)
        assert before['document']['kind'] == 'board', 'Activate a board first'
        doc = before['document']['document_id']
        def register(suffix, path, mode, count):
            name = prefix + '-' + suffix
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            entry = S.register_ulp(name, str(path), mode, ['board'], count, sha)
            names.append(name)
            return entry
        try:
            report['fusion'] = S.session.bridge.call('ping')
            entry = register('read', source_dir / 'example-counts.ulp', 'read', 0)
            report['read'] = S.run_registered_ulp(entry['name'], [], doc)
            elements = ET.fromstring(before['board']).findall('./drawing/board/elements/element')
            assert report['read']['data']['elements'] == len(elements)
            entry = register('edit', source_dir / 'example-move.ulp', 'edit', 1)
            report['preview'] = S.preview_ulp_edit(entry['name'], [elements[0].get('name')], doc)
            source = Path(temporary) / 'noop.ulp'; source.write_text('exit(0);\n')
            entry = register('legacy', source, 'legacy', 0)
            report['legacy'] = S.run_legacy_ulp(entry['name'], [], doc, entry['sha256'])
            assert report['legacy']['status'] == 'completed_unverified'
            assert not any(d['changed'] for d in report['legacy']['diff'].values())
            report['discovery'] = S.discover_fusion_ulps(limit=3)
            report['unchanged'] = M.fingerprint(before) == M.fingerprint(M.snapshot(S.session))
            assert report['unchanged']
        finally:
            for name in names: S.unregister_ulp(name)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__': main()
