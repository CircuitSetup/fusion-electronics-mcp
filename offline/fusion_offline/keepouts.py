"""Explicit per-connection planning exceptions; never change native footprint geometry."""

from dataclasses import replace
import hashlib
import math
import xml.etree.ElementTree as ET

from . import router, stitch


def digest(element):
    return hashlib.sha256(ET.canonicalize(ET.tostring(element, encoding='unicode'), strip_text=True).encode()).hexdigest()


def identity(root, source_ref):
    board = root.find('./drawing/board')
    elements = [e for e in board.findall('./elements/element') if e.get('name') == source_ref]
    if len(elements) != 1:
        raise ValueError('Keepout exception needs one exact source reference')
    element = elements[0]
    packages = [p for library in board.findall('./libraries/library') if library.get('name') == element.get('library')
                for p in library.findall('./packages/package') if p.get('name') == element.get('package')]
    if len(packages) != 1:
        raise ValueError('Keepout source package is missing or ambiguous')
    return {'source_ref': source_ref, 'library': element.get('library'), 'package': element.get('package'),
            'package_sha256': digest(packages[0]), 'source_element_sha256': digest(element)}


def _box(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(n) not in (float, int) or not math.isfinite(n) for n in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise ValueError('Keepout regions must be finite nonempty [x0, y0, x1, y1] rectangles')
    return tuple(value)


def _rectangle(box, **kwargs):
    x0, y0, x1, y1 = box
    return stitch.Obstacle('rect', None, False, ((x0+x1)/2, (y0+y1)/2, (x1-x0)/2, (y1-y0)/2, 0), **kwargs)


def header_connection_obstacles(root, declaration, from_pad, to_pad):
    """Return a connection-scoped router cache and explicit scope report. No XML is changed."""
    fields = {'source_ref', 'package_sha256', 'source_element_sha256', 'header_refs',
              'protected_region_mm', 'layers', 'connections', 'reason'}
    if not isinstance(declaration, dict) or set(declaration) != fields:
        raise ValueError(f'Keepout exception requires exactly {sorted(fields)}')
    if not isinstance(declaration['reason'], str) or not declaration['reason'].strip():
        raise ValueError('Keepout exception needs an explicit authorization reason')
    source = identity(root, declaration['source_ref'])
    if any(declaration[key] != source[key] for key in ('package_sha256', 'source_element_sha256')):
        raise ValueError('Keepout source package or element changed; review the new identity')
    headers = declaration['header_refs']
    refs = {e.get('name') for e in root.findall('./drawing/board/elements/element')}
    if (not isinstance(headers, list) or not headers or any(not isinstance(ref, str) or ref not in refs for ref in headers)
            or len(set(headers)) != len(headers) or source['source_ref'] in headers):
        raise ValueError('Declare exact existing header references distinct from the keepout source')
    core = _box(declaration['protected_region_mm'])
    obstacles, rules, outline = stitch.board_obstacles(root)
    source_obstacles = [o for o in obstacles if o.source_ref == source['source_ref'] and o.restrict_layer in (41, 42)]
    from .footprints import area, clip
    corners = [(core[0], core[1]), (core[2], core[1]), (core[2], core[3]), (core[0], core[3])]
    core_area = (core[2]-core[0])*(core[3]-core[1])
    if not any(math.isclose(area(clip(o.data[2], corners)), core_area, rel_tol=1e-9, abs_tol=1e-9) for o in source_obstacles):
        raise ValueError('Protected central region must lie inside a source trace restriction')
    layers = declaration['layers']
    available = {layer for o in source_obstacles for layer in o.layers}
    if (not isinstance(layers, list) or not layers or any(type(layer) is not int or layer not in available for layer in layers)
            or len(set(layers)) != len(layers)):
        raise ValueError('Exception layers must be unique source trace-restrict copper layers')
    connections = declaration['connections']
    if not isinstance(connections, list) or not connections:
        raise ValueError('Declare exact header pad connections and their corridors')
    seen, selected = set(), None
    for connection in connections:
        if not isinstance(connection, dict) or set(connection) != {'pads', 'corridor_mm'}:
            raise ValueError('Each header connection requires exactly pads and corridor_mm')
        pads = connection['pads']
        if (not isinstance(pads, list) or len(pads) != 2 or any(not isinstance(p, str) or '.' not in p for p in pads)
                or len(set(pads)) != 2 or not any(p.partition('.')[0] in headers for p in pads)):
            raise ValueError('Connection must name two distinct pads including a declared header')
        pair = frozenset(pads)
        if pair in seen:
            raise ValueError('Duplicate header connection')
        seen.add(pair)
        corridor = _box(connection['corridor_mm'])
        if not (corridor[2] < core[0] or corridor[0] > core[2] or corridor[3] < core[1] or corridor[1] > core[3]):
            raise ValueError('Header corridor touches or intersects the protected central region')
        try:
            targets = [router.pad_target(root, *p.split('.', 1)) for p in pads]
        except KeyError as error:
            raise ValueError(f'Header pad missing: {error}') from None
        if targets[0][3] is None or targets[0][3] != targets[1][3]:
            raise ValueError('Header connection pads must share one actual net')
        if any(core[0] <= t[0] <= core[2] and core[1] <= t[1] <= core[3] for t in targets):
            raise ValueError('A connection endpoint enters the protected central region')
        if not any(p.partition('.')[0] in headers and corridor[0] <= t[0] <= corridor[2]
                   and corridor[1] <= t[1] <= corridor[3] for p, t in zip(pads, targets)):
            raise ValueError('Corridor must contain a declared header connection pad')
        if pair == frozenset((from_pad, to_pad)):
            selected = corridor, targets
    if selected is None:
        raise ValueError('Requested pad pair has no declared header corridor')
    corridor, targets = selected
    scope_key = (targets[0][3], tuple(sorted((tuple(t[:2]) for t in targets))))
    result = []
    for obstacle in obstacles:
        if obstacle in source_obstacles and obstacle.layers & set(layers):
            # Keep the full restriction for vias, including when the source has no vRestrict.
            result.append(replace(obstacle, layers=frozenset()))
            obstacle = replace(obstacle, trace_holes=(_rectangle(corridor),), trace_holes_for=scope_key)
        result.append(obstacle)
    result.append(_rectangle(core, restrict_layer=43, source_ref=source['source_ref']))
    scope = {**source, 'header_refs': list(headers), 'connection': [from_pad, to_pad], 'net': targets[0][3],
             'corridor_mm': list(corridor), 'protected_region_mm': list(core), 'layers': list(layers),
             'reason': declaration['reason'], 'native_keepout_unchanged': True, 'native_drc_waived': False}
    return (result, rules, outline), scope
