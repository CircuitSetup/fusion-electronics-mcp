"""Source-linked deterministic design checks; never a substitute for simulation or review."""
import heapq
import math
from . import router as RT
from . import design as D
from . import stitch
from .eagle import UnverifiedGeometry

PLACEMENT = {'distance', 'separation', 'keepout'}
METRICS = {'resistance_ohm', 'capacitance_f', 'effective_capacitance_f', 'esr_ohm',
           'inductance_h', 'voltage_rating_v', 'current_rating_a', 'power_rating_w',
           'saturation_current_a', 'rms_current_a', 'vds_rating_v', 'vgs_rating_v',
           'id_rating_a', 'rds_on_ohm', 'gate_charge_c', 'frequency_hz', 'tolerance_fraction'}
FIELDS = {
    'distance': {'pads', 'net', 'max_mm', 'min_mm', 'same_side'},
    'separation': {'pads', 'net', 'max_mm', 'min_mm', 'same_side'},
    'path_length': {'pads', 'net', 'max_mm', 'min_mm', 'same_side'},
    'loop': {'segments', 'max_mm'},
    'keepout': {'region_mm', 'layers', 'allowed_nets'},
    'selection': {'component', 'metric', 'min', 'max'},
    'routing': {'net', 'layers', 'min_width_mm', 'clearance_mm', 'priority', 'allow_vias',
                'min_drill_mm', 'min_diameter_mm', 'via_technology', 'via_spans'},
    'thermal_vias': {'pad', 'net', 'radius_mm', 'min_count', 'min_drill_mm', 'min_diameter_mm'},
    'pour': {'net', 'layer', 'clearance_mm', 'solid'},
}


def validate(spec):
    if not isinstance(spec, dict) or spec.get('version') != 1:
        raise ValueError('Design requirements version must be 1')
    for key in ('devices', 'sources', 'operating_conditions', 'components'):
        if not isinstance(spec.get(key, {}), dict):
            raise ValueError(f'{key} must be an object')
    if not isinstance(spec.get('requirements'), list):
        raise ValueError('requirements must be a list')
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Requirements contain nonfinite numeric value')
        if isinstance(value, dict):
            for v in value.values(): finite(v)
        if isinstance(value, list):
            for v in value: finite(v)
    finite(spec)
    for device in spec.get('devices', {}).values():
        if not all(isinstance(device.get(k), str) and device[k].strip() for k in ('mpn', 'package')):
            raise ValueError('Each device needs an exact MPN and package')
    ids = set()
    for r in spec['requirements']:
        if not isinstance(r, dict) or not all(r.get(k) for k in ('id', 'type', 'device', 'statement')):
            raise ValueError('Each requirement needs id, type, device and statement')
        if r['id'] in ids or r['device'] not in spec.get('devices', {}):
            raise ValueError('Duplicate requirement ID or unknown device')
        ids.add(r['id'])
        if r.get('origin') not in ('manufacturer', 'assumption'):
            raise ValueError('Requirement origin must be manufacturer or assumption')
        if r['origin'] == 'assumption' and not r.get('rationale'):
            raise ValueError('Engineering assumptions need a rationale')
        if r.get('qualitative') and r['origin'] != 'assumption' and any(k in r for k in ('max_mm', 'min_mm')):
            raise ValueError('A numerical bound chosen for qualitative text must be an assumption')
        common = {'id', 'type', 'device', 'origin', 'source', 'pages', 'statement', 'conditions', 'rationale', 'qualitative'}
        if r['type'] in FIELDS and set(r) - common - FIELDS[r['type']]:
            raise ValueError(f"Unsupported requirement field: {sorted(set(r) - common - FIELDS[r['type']])}")
        if 'layers' in r and (not isinstance(r['layers'], list) or not r['layers'] or
                            any(type(l) is not int or l <= 0 for l in r['layers'])):
            raise ValueError('layers must be a nonempty list of copper IDs')
        for key in ('allow_vias', 'same_side', 'solid', 'qualitative'):
            if key in r and type(r[key]) is not bool: raise ValueError(f'{key} must be boolean')
        if 'min_count' in r and (type(r['min_count']) is not int or r['min_count'] < 1):
            raise ValueError('min_count must be a positive integer')
        for key in ('max_mm', 'min_mm', 'radius_mm', 'min_width_mm', 'clearance_mm', 'min_drill_mm', 'min_diameter_mm', 'priority'):
            if key in r and (type(r[key]) not in (float, int) or r[key] < 0):
                raise ValueError(f'{key} must be a nonnegative finite number')
        for key in ('min', 'max'):
            if key in r and type(r[key]) not in (float, int):
                raise ValueError(f'{key} must be numeric')
    return spec


def _conditions(row, spec):
    conditions = row.get('conditions')
    if not isinstance(conditions, dict) or not conditions:
        return False
    actual = spec.get('operating_conditions', {})
    for key, limit in conditions.items():
        if key not in actual:
            return False
        value = actual[key]
        if isinstance(limit, list):
            if len(limit) != 2 or not all(type(v) in (float, int) for v in [*limit, value]) or not limit[0] <= value <= limit[1]:
                return False
        elif value != limit:
            return False
    return True


def _source(row, device, spec, evidence):
    source = evidence.get(row.get('source'), {})
    declared = spec.get('sources', {}).get(row.get('source'), {})
    pages = row.get('pages')
    if not source or source.get('sha256') != declared.get('sha256'):
        return 'unverified', 'Source missing or stale hash'
    if source.get('mpn') != device.get('mpn') or source.get('package') != device.get('package'):
        return 'fail', 'Source is for a different MPN/package'
    if not source.get('revision') or not source.get('source_url') or not isinstance(pages, list) or not pages:
        return 'unverified', 'Missing source revision, URL or page references'
    if any(type(p) is not int or not 1 <= p <= source.get('page_count', 0)
           or source.get('inspected_pages', {}).get(str(p), {}).get('source_sha256') != source['sha256'] for p in pages):
        return 'unverified', 'Referenced pages have not been delivered for text/diagram inspection'
    if not _conditions(row, spec):
        return 'unverified', 'Operating conditions missing or outside cited applicability'
    return 'pass', ''


def _pad(root, name):
    ref, sep, pad = name.partition('.')
    if not sep:
        raise ValueError('Pad references use REF.PAD')
    return RT.pad_target(root, ref, pad)


def path_length(root, pads, net):
    """Shortest actual straight-wire path, layer-aware; no invented connectivity through pours."""
    targets = [_pad(root, p) if isinstance(p, str) else p for p in pads]
    if any(t[3] != net for t in targets):
        return None
    sig = next((s for s in root.findall('.//board/signals/signal') if s.get('name') == net), None)
    if sig is None:
        return None
    wires = RT._net_traces(root, net)
    layers = tuple(D.parse_board_design(root).copper_layers)
    nodes = {(round(p[0], 4), round(p[1], 4), l) for l, a, b in wires for p in (a, b)}
    joins = []
    for c in sig.findall('contactref'):
        x, y, ls, _ = RT.pad_target(root, c.get('element'), c.get('pad'))
        group = [(x, y, l) for l in ls]
        nodes.update(group)
        joins.append(group)
    for v in sig.findall('via'):
        ends = [int(n) for n in v.get('extent', f'{layers[0]}-{layers[-1]}').split('-')]
        if len(ends) != 2 or any(n not in layers for n in ends):
            return None
        i, j = sorted(layers.index(n) for n in ends)
        group = [(round(float(v.get('x')), 4), round(float(v.get('y')), 4), l) for l in layers[i:j+1]]
        nodes.update(group)
        joins.append(group)
    for x, y, ls, _ in targets:
        nodes.update((x, y, l) for l in ls)
    graph = {p: [] for p in nodes}
    def edge(a, b, distance):
        graph[a].append((b, distance)); graph[b].append((a, distance))
    for layer, a, b in wires:
        on = [p for p in nodes if p[2] == layer and RT._seg_dist(p[0], p[1], (*a, *b)) <= .001]
        on.sort(key=lambda p: math.dist(p[:2], a))
        for p, q in zip(on, on[1:]): edge(p, q, math.dist(p[:2], q[:2]))
    for group in joins:
        for p, q in zip(group, group[1:]): edge(p, q, 0)
    starts = {(targets[0][0], targets[0][1], l) for l in targets[0][2]}
    goals = {(targets[-1][0], targets[-1][1], l) for l in targets[-1][2]}
    queue = [(0., p) for p in starts if p in graph]; heapq.heapify(queue)
    seen = set()
    while queue:
        distance, point = heapq.heappop(queue)
        if point in seen: continue
        if point in goals: return distance
        seen.add(point)
        for dest, cost in graph[point]:
            if dest not in seen: heapq.heappush(queue, (distance + cost, dest))
    return None


def _evaluate(root, r, spec, evidence):
    kind = r['type']
    def bounds(value, lo=None, hi=None):
        return ('pass' if (lo is None or value >= lo - 1e-9) and (hi is None or value <= hi + 1e-9) else 'fail', value)
    if kind == 'selection':
        if r.get('metric') not in METRICS:
            return 'unverified', 'Unsupported component analysis (including SOA/thermal simulation)'
        component = r['component']
        fact = spec.get('components', {}).get(component, {}).get('parameters', {}).get(r['metric'])
        if not fact or component not in spec['devices']:
            return 'unverified', 'Missing candidate part/rating or worst-case interval'
        status, reason = _source(fact, spec['devices'][component], spec, evidence)
        if status != 'pass': return status, reason
        if not set(r.get('conditions', {})) <= set(fact.get('conditions', {})):
            return 'unverified', 'Candidate evidence does not cover every required operating condition'
        low, high = fact.get('min'), fact.get('max')
        if type(low) not in (int, float) or type(high) not in (int, float) or low > high:
            return 'unverified', 'Candidate needs a finite worst-case min/max, including tolerance/derating'
        if 'min' not in r and 'max' not in r:
            return 'unverified', 'No selection bound supplied'
        return ('pass' if low >= r.get('min', -math.inf) and high <= r.get('max', math.inf) else 'fail', [low, high])
    if kind in ('distance', 'separation', 'path_length'):
        a, b = [_pad(root, p) for p in r['pads']]
        if r.get('net') and any(p[3] != r['net'] for p in (a, b)):
            return 'fail', 'Pad is not on specified net'
        if r.get('same_side') and not set(a[2]) & set(b[2]):
            return 'fail', 'Pads are on different copper layers'
        if kind == 'path_length':
            length = path_length(root, r['pads'], r['net'])
            if length is None: return 'unverified', 'No verified wire/via path; pours/arcs require additional analysis'
        else:
            length = math.dist(a[:2], b[:2])
        if 'max_mm' not in r and 'min_mm' not in r:
            return 'unverified', 'Qualitative placement has no numerical engineering bound'
        return bounds(length, r.get('min_mm'), r.get('max_mm'))
    if kind == 'loop':
        paths = [path_length(root, segment['pads'], segment['net']) for segment in r['segments']]
        if not paths or any(p is None for p in paths):
            return 'unverified', 'Critical loop is not fully routed with supported geometry'
        return bounds(sum(paths), hi=r['max_mm'])
    if kind == 'keepout':
        box = r['region_mm']
        if len(box) != 4 or box[0] >= box[2] or box[1] >= box[3]: raise ValueError('Invalid keepout rectangle')
        # Conservative obstacle bounding boxes; a false collision is safer than an untested crossing.
        bad = []
        for o in stitch.board_obstacles(root)[0]:
            if o.restrict_layer is not None: continue
            if o.net in r.get('allowed_nets', []): continue
            if o.layers is not None and not set(o.layers) & set(r['layers']): continue
            ob = o.bounds
            if ob[0] < box[2] and ob[2] > box[0] and ob[1] < box[3] and ob[3] > box[1]: bad.append(o.net or 'hole')
        if bad: return 'fail', bad
        for sg in root.findall('.//board/signals/signal'):
            if sg.get('name') in r.get('allowed_nets', []): continue
            for polygon in sg.findall('polygon'):
                if int(polygon.get('layer')) not in r['layers']: continue
                points = [(float(v.get('x')), float(v.get('y'))) for v in polygon.findall('vertex')]
                if points and min(p[0] for p in points) < box[2] and max(p[0] for p in points) > box[0] and min(p[1] for p in points) < box[3] and max(p[1] for p in points) > box[1]:
                    return 'unverified', 'Pour intersects keepout bounds; actual fill geometry unavailable'
        return 'pass', []
    if kind in ('routing', 'thermal_vias', 'pour'):
        sig = next((s for s in root.findall('.//board/signals/signal') if s.get('name') == r['net']), None)
        if sig is None: return 'fail', 'Specified net missing'
        if kind == 'thermal_vias':
            pad = _pad(root, r['pad'])
            if pad[3] != r['net']: return 'fail', 'Thermal pad is on wrong net'
            vs = [v for v in sig.findall('via') if math.dist(pad[:2], (float(v.get('x')), float(v.get('y')))) <= r['radius_mm']]
            good = [v for v in vs if float(v.get('drill')) >= r.get('min_drill_mm', 0)
                    and float(v.get('diameter', '0')) >= r.get('min_diameter_mm', 0)
                    and v.get('extent') == f'{D.parse_board_design(root).copper_layers[0]}-{D.parse_board_design(root).copper_layers[-1]}']
            if len(good) < r['min_count']: return 'fail', len(good)
            connected = [v for v in good if path_length(root, [r['pad'], (float(v.get('x')), float(v.get('y')),
                         RT.via_layers(root, v), r['net'])], r['net']) is not None]
            return ('pass' if len(connected) >= r['min_count'] else 'unverified',
                    f'{len(good)} vias in pattern; {len(connected)} connected by supported copper geometry (no thermal simulation)')
        if kind == 'pour':
            ps = [p for p in sig.findall('polygon') if int(p.get('layer')) == r['layer']]
            if not ps: return 'unverified', 'Required pour missing'
            valid = all(float(p.get('isolate', '0')) >= r.get('clearance_mm', 0)
                        and (not r.get('solid') or (p.get('pour', 'solid') == 'solid' and p.get('thermals') == 'no')) for p in ps)
            return ('pass' if valid else 'fail', 'Polygon attributes only; actual fill/return continuity is not certified')
        layers = D.parse_board_design(root).copper_layers
        concrete = {'layers', 'min_width_mm', 'clearance_mm', 'min_drill_mm', 'min_diameter_mm', 'allow_vias', 'via_spans'}
        if not concrete.intersection(r) or (r.get('qualitative') and r['origin'] != 'assumption'):
            return 'unverified', 'No concrete engineering bounds for qualitative routing requirement'
        if r.get('via_technology', 'through') != 'through': return 'unverified', 'Unsupported via technology'
        wires = [w for w in sig.findall('wire') if int(w.get('layer')) in layers]
        if not wires and not sig.findall('via'): return 'unverified', 'No routed copper on required net'
        bad = []
        if any(l not in layers for l in r.get('layers', [])): return 'fail', 'Assigned layer is absent from stack'
        for w in wires:
            if int(w.get('layer')) not in r.get('layers', layers): bad.append('layer')
            if float(w.get('width')) < r.get('min_width_mm', 0) - 1e-6: bad.append('width')
        for v in sig.findall('via'):
            if not r.get('allow_vias', True): bad.append('via forbidden')
            if v.get('extent') != f'{layers[0]}-{layers[-1]}': bad.append('unsupported via span')
            if 'via_spans' in r and [layers[0], layers[-1]] not in r['via_spans']: bad.append('via span forbidden')
            if float(v.get('drill')) < r.get('min_drill_mm', 0): bad.append('via drill')
            if float(v.get('diameter', '0')) < r.get('min_diameter_mm', 0): bad.append('via diameter')
        if bad: return 'fail', sorted(set(bad))
        if any(float(w.get('curve', '0')) for w in wires): return 'unverified', 'Curved routing clearance not supported by this checker'
        if r.get('clearance_mm'):
            from . import placement_check as PC, footprints as FP
            obstacles = stitch.board_obstacles(root)[0]
            def shape(o):
                return (PC._circle(*o.data) if o.kind == 'circle' else
                        PC._box(*o.data) if o.kind == 'rect' else PC._stadium(*o.data))
            for own in (o for o in obstacles if o.net == r['net']):
                for o in obstacles:
                    if o.net == r['net'] or (o.layers is not None and own.layers is not None and not o.layers & own.layers): continue
                    if o.kind == 'polygon':
                        a, b = shape(own), o.data[2]
                        ae, be = list(zip(a, a[1:]+a[:1])), list(zip(b, b[1:]+b[:1]))
                        overlap = (any(stitch._inside_poly(b, *p) for p in a)
                                   or any(stitch._inside_poly(a, *p) for p in b)
                                   or any(stitch.segments_intersect(*u, *v) for u in ae for v in be))
                        gap = -o.data[3] if overlap else min(FP._seg_dist(*u, *v) for u in ae for v in be)-o.data[3]
                    else:
                        gap = FP.gap(shape(own), shape(o))
                    if gap < r['clearance_mm'] - 1e-6: bad.append('copper clearance')
            if not bad and root.find('.//board/signals/signal/polygon') is not None:
                return 'unverified', 'Wire/pad/via clearance checked; actual poured copper geometry unavailable'
        if not bad and not wires: return 'unverified', 'Via constraints checked; no routed wires on required net'
        return ('fail' if bad else 'pass', sorted(set(bad)))
    return 'unverified', f'Unsupported analysis: {kind}'


def check(root, spec, evidence, phase='all'):
    if spec is None:
        return {'status': 'not_supplied', 'findings': [], 'notice': 'No datasheet constraints supplied; DRC/ERC is not datasheet compliance.'}
    validate(spec)
    elements = {e.get('name'): e for e in root.findall('.//board/elements/element')}
    findings = []
    device_status = {}
    for ref, device in spec.get('devices', {}).items():
        el = elements.get(ref)
        attrs = {a.get('name'): a.get('value') for a in el.findall('attribute')} if el is not None else {}
        status = ('unverified' if el is None or not (attrs.get('MPN') or attrs.get('MP')) else
                  'pass' if el.get('package') == device.get('package') and (attrs.get('MPN') or attrs.get('MP')) == device.get('mpn') else 'fail')
        if el is not None and 'value' in device and el.get('value') != device['value']:
            status = 'fail'
        findings.append(dict(id='device:' + ref, status=status, observed='Exact board MPN/package binding', device=ref))
        device_status[ref] = status
    for r in spec['requirements']:
        if phase == 'placement' and r['type'] not in PLACEMENT: continue
        status, reason = _source(r, spec['devices'][r['device']], spec, evidence)
        pad_refs = [p.split('.')[0] for p in r.get('pads', [])]
        if 'pad' in r: pad_refs.append(r['pad'].split('.')[0])
        pad_refs.extend(p.split('.')[0] for seg in r.get('segments', []) for p in seg.get('pads', []))
        for ref in (r['device'], r.get('component', r['device']), *pad_refs):
            if device_status.get(ref, 'unverified') != 'pass':
                status, reason = device_status.get(ref, 'unverified'), 'Exact active device identity not verified'
        if status == 'pass':
            try:
                status, reason = _evaluate(root, r, spec, evidence)
            except KeyError as ex:
                status, reason = 'unverified', f'Missing geometry or requirement field: {ex}'
            except UnverifiedGeometry as ex:
                status, reason = 'unverified', str(ex)
        source = evidence.get(r.get('source'), {})
        findings.append(dict(id=r['id'], type=r['type'], status=status, observed=reason, origin=r['origin'],
                             device=r['device'], statement=r['statement'], conditions=r.get('conditions'),
                             source_url=source.get('source_url'), revision=source.get('revision'),
                             sha256=source.get('sha256'), pages=r.get('pages'), rationale=r.get('rationale')))
    states = {f['status'] for f in findings}
    status = 'fail' if 'fail' in states else 'unverified' if 'unverified' in states or not spec['requirements'] else 'pass'
    return {'status': status, 'findings': findings, 'notice': 'Pass applies only to listed checks under cited conditions; DRC/ERC and evidence delivery are not engineering approval.'}


def regressions(before, after):
    old = {f['id']: f['status'] for f in before['findings']}
    return [f['id'] for f in after['findings'] if (f['status'] == 'fail' and old.get(f['id']) != 'fail')
            or (old.get(f['id']) == 'pass' and f['status'] != 'pass')]


def routing_policy(spec, net):
    """Merge constraints conservatively; no implicit manufacturer's numeric bounds."""
    policy = {}
    for r in (spec or {}).get('requirements', []):
        if r['type'] != 'routing' or r.get('net') != net: continue
        if r.get('via_technology', 'through') != 'through': raise ValueError('Unsupported via technology: through vias only')
        if 'via_spans' in r:
            policy['via_spans'] = r['via_spans'] if 'via_spans' not in policy else [s for s in policy['via_spans'] if s in r['via_spans']]
        if 'layers' in r:
            policy['layers'] = sorted(set(policy.get('layers', r['layers'])) & set(r['layers']))
            if not policy['layers']: raise ValueError('Conflicting net layer assignments')
        for key in ('min_width_mm', 'clearance_mm', 'min_drill_mm', 'min_diameter_mm', 'priority'):
            policy[key] = max(policy.get(key, 0), r.get(key, 0))
        policy['allow_vias'] = policy.get('allow_vias', True) and r.get('allow_vias', True)
    return policy
