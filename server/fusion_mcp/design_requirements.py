"""Persistent document-bound requirements and shared planning/read-back checks."""
from pathlib import Path
import contextlib
import hashlib
import json
import math
from fusion_offline import requirements as R, design as D, placement_check as PC
from fusion_offline import pin_evidence as PE
from . import datasheets, design_store, pin_evidence


def identity(document):
    return document.get('data_file_id') or document.get('document_id')


def _load(binding):
    path = Path(binding['path'])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != binding['sha256']:
        raise ValueError('Design requirements changed since binding; inspect and bind the new hash')
    spec = R.validate(json.loads(path.read_text(encoding='utf-8-sig')))
    evidence = {}
    for key, source in spec.get('sources', {}).items():
        source_path = Path(source['manifest_path'])
        if not source_path.is_absolute(): source_path = path.parent / source_path
        evidence[key] = datasheets.load(source_path)
        if evidence[key]['sha256'] != source['sha256']:
            raise ValueError('Datasheet source changed since requirements were extracted')
    return spec, evidence, path.parent


def bound(session):
    context = session.context() or {}
    doc = context.get('active_document') or {}
    if doc.get('kind') != 'board':
        for candidate in context.get('documents', []):
            if candidate.get('kind') == 'board' and identity(candidate) and design_store.load(identity(candidate)).get('datasheet_requirements'):
                raise ValueError('Activate the exact constrained PCB before planning/writing; no name-based document inference')
        return None
    key = identity(doc)
    binding = design_store.load(key).get('datasheet_requirements') if key else None
    return _load(binding) if binding else None


def bind(session, path, expected_sha256, expected_document_id):
    lock = getattr(session.bridge, 'lock', None)
    with lock.hold('bind design requirements') if lock else contextlib.nullcontext():
        doc = (session.context() or {}).get('active_document') or {}
        if doc.get('kind') != 'board' or doc.get('document_id') != expected_document_id:
            raise ValueError('Activate the exact board document before binding requirements')
        binding = {'path': str(Path(path).resolve()), 'sha256': expected_sha256}
        loaded = _load(binding)
        snapshot = session.snapshot(schematic=False)
        result = report(session, D.read_xml(snapshot.board_xml), loaded)
        after = (session.context() or {}).get('active_document') or {}
        if after.get('document_id') != expected_document_id:
            raise ValueError('Active document changed while binding requirements')
        design_store.update(identity(doc), lambda data: data.update(datasheet_requirements=binding))
        return {'bound': True, 'document_id': expected_document_id, 'requirements_sha256': expected_sha256,
                'report': result, 'notice': 'Binding is not approval; unresolved findings remain visible.'}


def report(session, root, loaded=None, phase='all'):
    loaded = loaded if loaded is not None else bound(session)
    if not loaded: return R.check(root, None, {})
    spec, evidence, directory = loaded
    result = R.check(root, spec, evidence, phase)
    for ref in {r['device'] for r in spec['requirements']}:
        device = spec['devices'][ref]
        row = {'id': 'pin_evidence:' + ref, 'device': ref, 'status': 'unverified',
               'observed': 'No reviewed pin evidence and exact library part supplied'}
        if device.get('pin_evidence_path') and device.get('part_path'):
            ev, actual = pin_evidence.load(directory / device['pin_evidence_path'])
            part = json.loads((directory / device['part_path']).read_text(encoding='utf-8-sig'))
            checks = PE.validate_pin_evidence(part, ev, actual)
            schematic = session.snapshot(board=False).sch_xml
            if schematic:
                checks += PE.compare_pin_evidence(D.parse_schematic_design(D.read_xml(schematic)),
                    {part['id']: part}, {part['id']: ev}, {part['id']: [ref]})
            else:
                checks.append(PE.finding('active_pin_mapping', 'unknown', refs=[ref]))
            mismatched = ev.get('mpn') != device['mpn'] or ev.get('package') != device['package']
            row.update(status='fail' if mismatched or any(f['status'] == 'fail' for f in checks) else
                       'unverified' if any(f['status'] == 'unknown' for f in checks) else 'pass', observed=checks)
        result['findings'].append(row)
    states = {f['status'] for f in result['findings']}
    result['status'] = 'fail' if 'fail' in states else 'unverified' if 'unverified' in states else result['status']
    return result


def guard(session, before, after, loaded=None, phase='all'):
    old = report(session, before, loaded, phase)
    new = report(session, after, loaded, phase)
    bad = R.regressions(old, new)
    if bad:
        raise ValueError('Datasheet requirement regression: ' + ', '.join(bad))
    return new


def plan_placement(session, root):
    loaded = bound(session)
    if not loaded: return {'moves': [], 'unresolved': ['No datasheet constraints supplied'], 'report': report(session, root)}
    spec, evidence, _ = loaded
    work = root
    moves = []
    unresolved = []
    for r in spec['requirements']:
        if r['type'] != 'distance': continue
        result = R.check(work, {**spec, 'requirements': [r]}, evidence, 'placement')['findings'][-1]
        if result['status'] == 'pass': continue
        if result['status'] == 'unverified' or not r.get('max_mm'):
            unresolved.append(r['id']); continue
        anchor, target = r['pads']
        ref = target.split('.')[0]
        if ref == r['device']:
            unresolved.append(r['id']); continue
        ax, ay, _, _ = R._pad(work, anchor)
        chosen = None
        for angle in (0, 90, 180, 270):
            rotated = PC.apply_moves(work, PC.resolve_moves(work, [{'ref': ref, 'angle': angle}]))
            px, py, _, _ = R._pad(rotated, target)
            el = next(e for e in rotated.findall('.//board/elements/element') if e.get('name') == ref)
            for radius in (r['max_mm'] * .75, r['max_mm'] * .5, r['max_mm']):
                for direction in range(8):
                    theta = direction * math.pi / 4
                    move = {'ref': ref, 'angle': angle, 'x_mm': float(el.get('x')) + ax + radius*math.cos(theta) - px,
                            'y_mm': float(el.get('y')) + ay + radius*math.sin(theta) - py}
                    proposed = PC.apply_moves(work, PC.resolve_moves(work, [move]))
                    check = R.check(proposed, spec, evidence, 'placement')
                    if any(f['id'] == r['id'] and f['status'] != 'pass' for f in check['findings']): continue
                    if R.regressions(R.check(work, spec, evidence, 'placement'), check): continue
                    if any(f['kind'] in ('pads', 'courtyard') for f in PC.move_effects(work, proposed, [ref])['introduced']): continue
                    chosen = move, proposed
                    break
                if chosen: break
            if chosen: break
        if chosen:
            moves.append(chosen[0]); work = chosen[1]
        else: unresolved.append(r['id'])
    return {'moves': moves, 'unresolved': unresolved, 'report': report(session, work, loaded),
            'notice': 'Candidate pad-specific placement; bounds retain their source/origin. Verify after routing.'}


def route_options(loaded, net, width, **options):
    policy = R.routing_policy(loaded[0] if loaded else None, net)
    if not policy: return width, options
    width = max(width, policy.get('min_width_mm', 0))
    if 'layers' in policy: options['layers'] = tuple(policy['layers'])
    if 'via_spans' in policy: options['via_spans'] = policy['via_spans']
    options['vias'] = options.get('vias', True) and policy.get('allow_vias', True)
    for key, name in [('clearance_mm', 'clearance'), ('min_drill_mm', 'via_drill'), ('min_diameter_mm', 'via_d')]:
        if policy.get(key): options[name] = max(options.get(name) or 0, policy[key])
    # A minimum width includes pad escapes; never silently neck below that bound.
    if policy.get('min_width_mm'): options.update(neck_width=width, neck_len=0)
    return width, options
