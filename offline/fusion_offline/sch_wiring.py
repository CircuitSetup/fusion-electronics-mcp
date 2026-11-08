"""Deterministic orthogonal schematic wiring shared by previews and live commands."""
from collections import defaultdict
import heapq
import math
import xml.etree.ElementTree as ET

from . import design as D, symbols as S

GAP = 2.54
TOL = 1e-4


def point(x, y):
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError('Nonfinite schematic coordinate')
    return round(x, 4), round(y, 4)


def on(p, a, b):
    return (abs((p[0]-a[0])*(b[1]-a[1])-(p[1]-a[1])*(b[0]-a[0])) <= TOL*max(1,math.dist(a,b))
            and min(a[0], b[0])-TOL <= p[0] <= max(a[0], b[0])+TOL
            and min(a[1], b[1])-TOL <= p[1] <= max(a[1], b[1])+TOL)


def edges(net):
    return [(point(float(w.get('x1')), float(w.get('y1'))), point(float(w.get('x2')), float(w.get('y2'))))
            for w in net.findall('./segment/wire') if not float(w.get('curve','0')) and w.get('layer') == '91']


def components(lines, points=()):
    groups = [{p} for p in sorted(set(points) | {p for ab in lines for p in ab})]
    for a, b in lines:
        hits = [g for g in groups if any(on(p, a, b) for p in g)]
        groups = [g for g in groups if g not in hits] + [set().union(*hits)]
    return sorted(groups, key=lambda g: min(g))


def clear(a, b, boxes):
    # Box boundaries provide the requested clearance; only their interiors are blocked.
    return not any(min(a[0], b[0]) < x1-TOL and max(a[0], b[0]) > x0+TOL
                   and min(a[1], b[1]) < y1-TOL and max(a[1], b[1]) > y0+TOL
                   for x0, y0, x1, y1 in boxes)


def cut_at_wires(path, lines):
    """NET stops at an existing wire; make each meeting point an explicit command end."""
    out = [path[0]]
    for a,b in zip(path,path[1:]):
        cuts = {a,b}
        for c,d in lines:
            for p in (c,d):
                if on(p,a,b): cuts.add(p)
            axis = 0 if a[0] == b[0] else 1
            if c[axis] != d[axis]:
                t = (a[axis]-c[axis])/(d[axis]-c[axis])
                p = point(c[0]+t*(d[0]-c[0]),c[1]+t*(d[1]-c[1]))
                if 0 <= t <= 1 and on(p,a,b): cuts.add(p)
        out.extend(sorted(cuts-{a},key=lambda p: abs(p[0]-a[0])+abs(p[1]-a[1])))
    return out


def route(starts, goals, boxes):
    starts = {p for p in starts if clear(p,p,boxes)}
    goals = {p for p in goals if clear(p,p,boxes)}
    xs = sorted({*(p[0] for p in starts | goals), *(round(v,4) for b in boxes for v in (b[0], b[2]))})
    ys = sorted({*(p[1] for p in starts | goals), *(round(v,4) for b in boxes for v in (b[1], b[3]))})
    if len(xs)*len(ys) > 60000:
        raise ValueError('Schematic routing is too dense; divide it into functional blocks')
    cost = {(xs.index(p[0]),ys.index(p[1]),0): 0 for p in starts}
    queue = [(0,s) for s in cost]; heapq.heapify(queue); prev = {}
    while queue:
        dist, state = heapq.heappop(queue)
        if dist != cost[state]: continue
        ix, iy, axis = state; a = xs[ix], ys[iy]
        if a in goals:
            path = [a]
            while state in prev:
                state = prev[state]; path.append((xs[state[0]], ys[state[1]]))
            path.reverse()
            simplified = []
            for p in path:
                if len(simplified) >= 2 and on(simplified[-1], simplified[-2], p): simplified.pop()
                simplified.append(p)
            return simplified
        for jx, jy, direction in ((ix-1, iy, 1), (ix+1, iy, 1), (ix, iy-1, 2), (ix, iy+1, 2)):
            if not (0 <= jx < len(xs) and 0 <= jy < len(ys)): continue
            b = xs[jx], ys[jy]
            if not clear(a, b, boxes): continue
            candidate = jx, jy, direction
            total = dist + abs(a[0]-b[0]) + abs(a[1]-b[1]) + (GAP if axis and axis != direction else 0)
            if total < cost.get(candidate, math.inf):
                cost[candidate] = total; prev[candidate] = state
                heapq.heappush(queue, (total, candidate))
    raise ValueError('No clear schematic wire path; space or reorient the related parts before connecting')


def body_boxes(root, sch, sheet):
    libraries = root.findall('./drawing/schematic/libraries/library')
    cache = {}; boxes = {}
    for inst in sch.instances:
        if inst.sheet != sheet: continue
        part = sch.parts[inst.part]
        raw = root.find(f'./drawing/schematic/parts/part[@name="{inst.part}"]')
        libs = [lib for lib in libraries if lib.get('name') == part.library]
        exact = [lib for lib in libs if lib.get('urn','') == raw.get('library_urn','')]
        if exact: libs = exact
        if len(libs) != 1: raise ValueError(f'Ambiguous symbol library for {inst.part}')
        lib = libs[0]
        if id(lib) not in cache: cache[id(lib)] = S.unit_geometry(lib)
        geo = cache[id(lib)][(part.deviceset+part.device, inst.gate)]
        if not geo.pins: continue
        x0, y0, x1, y1 = geo.body()
        corners = [D.transform(x, y, inst.x, inst.y, inst.angle, inst.mirror)
                   for x in (x0, x1) for y in (y0, y1)]
        boxes[inst.part, inst.gate] = (min(p[0] for p in corners)-GAP/2, min(p[1] for p in corners)-GAP/2,
                                      max(p[0] for p in corners)+GAP/2, max(p[1] for p in corners)+GAP/2)
    return boxes


def plan(root, net, targets, style='wired', labels=True, allow_merge=False):
    if style not in ('wired', 'labels'): raise ValueError("style is 'wired' or 'labels'")
    if not targets: raise ValueError('Explicit pins required')
    keys = [(p.part, p.gate, p.pin) for p in targets]
    if len(set(keys)) != len(keys): raise ValueError('Duplicate pins')
    sch = D.parse_schematic_design(root)
    allowed = {net}
    for p in targets:
        current = sch.net_of(p.part, p.pin, p.gate)
        if current and current != net:
            if not allow_merge: raise ValueError(f'{p.part}.{p.gate}.{p.pin} already belongs to {current}; implicit net merge refused')
            allowed.add(current)
    paths = []; named = []; junctions = []; sheets = root.findall('./drawing/schematic/sheets/sheet')
    by_sheet = defaultdict(list)
    for p in targets: by_sheet[p.sheet].append(p)
    if style == 'wired':
        for p in sch.pins:
            if sch.net_of(p.part,p.pin,p.gate) in allowed: by_sheet.setdefault(p.sheet,[])
    for sheet, requested in sorted(by_sheet.items()):
        nets = [n for n in sheets[sheet-1].findall('./nets/net') if n.get('name') in allowed]
        lines = [ab for n in nets for ab in edges(n)]
        pins = list(requested)
        if style == 'wired':
            for p in sch.pins:
                if p.sheet == sheet and sch.net_of(p.part, p.pin, p.gate) in allowed and p not in pins: pins.append(p)
            if len(pins) > 256 or len(lines) > 1024:
                raise ValueError('Schematic net is too dense; divide it into functional blocks')
        bodies = body_boxes(root, sch, sheet) if style == 'wired' else {}
        foreign = []
        if style == 'wired':
            for p in sch.pins:
                if p.sheet == sheet and p not in pins:
                    foreign.append((p.x-.25, p.y-.25, p.x+.25, p.y+.25))
            for n in sheets[sheet-1].findall('./nets/net'):
                for w in n.findall('./segment/wire'):
                    if float(w.get('curve', '0')): raise ValueError('Curved schematic wires need manual routing')
                if n.get('name') not in allowed:
                    for a, b in edges(n):
                        foreign.append((min(a[0], b[0])-.25, min(a[1], b[1])-.25,
                                        max(a[0], b[0])+.25, max(a[1], b[1])+.25))
        added = []
        for p in pins:
            at = point(p.x, p.y)
            if style == 'labels' and sch.net_of(p.part, p.pin, p.gate) == net: continue
            if (style == 'wired' and sch.net_of(p.part,p.pin,p.gate) in (None,net)
                    and any(on(at, a, b) for a, b in lines)): continue
            ox, oy = p.outward
            if (ox, oy) not in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                raise ValueError('Schematic pins must face a right angle for orthogonal wiring')
            length = GAP if style == 'wired' else GAP*2
            if style == 'wired':
                box = bodies[p.part,p.gate]
                length = max(length, (box[2]-p.x+GAP/2) if ox > 0 else (p.x-box[0]+GAP/2) if ox < 0
                             else (box[3]-p.y+GAP/2) if oy > 0 else (p.y-box[1]+GAP/2))
            end = point(p.x+ox*length, p.y+oy*length)
            obstacles = foreign + [box for key, box in bodies.items() if key != (p.part, p.gate)]
            if style == 'wired' and not clear(at, end, obstacles):
                raise ValueError(f'Blocked pin escape at {p.part}.{p.gate}.{p.pin}; space the related parts')
            path = cut_at_wires([at,end],lines) if style == 'wired' else [at,end]
            added.append(path); lines.extend(zip(path,path[1:]))
            if style == 'labels' and labels: named.append((sheet, ((at[0]+end[0])/2, (at[1]+end[1])/2), end))
        if style == 'wired':
            groups = components(lines, [point(p.x, p.y) for p in pins])
            while len(groups) > 1:
                # Join nearby components first, reusing existing wire ends as branch points.
                path = cut_at_wires(route(groups[1], groups[0], foreign+list(bodies.values())),lines)
                added.append(path); lines.extend(zip(path, path[1:]))
                groups = components(lines, [point(p.x, p.y) for p in pins])
            if labels and not any(n.find('./segment/label') is not None for n in nets):
                a, b = lines[0]
                named.append((sheet, ((a[0]+b[0])/2, (a[1]+b[1])/2), b))
            for p in sorted({p for path in added for p in path}):
                degree = sum(1 if p in (a,b) else 2 for a,b in set(lines) if on(p,a,b))
                if degree >= 3: junctions.append((sheet,p))
        paths.extend((sheet, path) for path in added)
    return {'paths': paths, 'labels': named, 'junctions': junctions}


def covered(a, b, lines):
    vertical = a[0] == b[0]
    axis = 1 if vertical else 0
    lo, hi = sorted((a[axis], b[axis]))
    spans = sorted(tuple(sorted((c[axis], d[axis]))) for c,d in lines
                   if abs(c[1-axis]-a[1-axis]) <= TOL and abs(d[1-axis]-a[1-axis]) <= TOL)
    end = lo
    for left,right in spans:
        if left > end+TOL: break
        end = max(end,right)
    return end >= hi-TOL


def wire_signature(root):
    """Ignore Fusion's splitting/merging of collinear wires, but retain their actual geometry."""
    grouped = defaultdict(list); other = []
    for si, sheet in enumerate(root.findall('./drawing/schematic/sheets/sheet'), 1):
        for n in sheet.findall('./nets/net'):
            for w in n.findall('./segment/wire'):
                a, b = point(float(w.get('x1')), float(w.get('y1'))), point(float(w.get('x2')), float(w.get('y2')))
                curve = float(w.get('curve', '0'))
                if not curve and (a[0] == b[0] or a[1] == b[1]):
                    vertical = a[0] == b[0]
                    key = si, n.get('name'), w.get('layer'), vertical, a[0] if vertical else a[1]
                    grouped[key].append(tuple(sorted((a[1] if vertical else a[0], b[1] if vertical else b[0]))))
                else: other.append((si, n.get('name'), w.get('layer'), a, b, curve))
    out = []
    for key, spans in sorted(grouped.items()):
        merged = []
        for lo, hi in sorted(spans):
            if merged and lo <= merged[-1][1]+TOL: merged[-1] = merged[-1][0], max(hi, merged[-1][1])
            else: merged.append((lo, hi))
        out.append((key, merged))
    return out, sorted(other)


def simulate(root, net, targets, wiring):
    """Append the planned wires and preserve all existing geometry and labels."""
    sch = D.parse_schematic_design(root)
    sheets = root.findall('./drawing/schematic/sheets/sheet')
    affected = {p.sheet for p in targets} | {s for s,_ in wiring['paths']} | {s for s,_,_ in wiring['labels']}
    for sheet in sorted(affected):
        new_pins = [p for p in targets if p.sheet == sheet and sch.net_of(p.part,p.pin,p.gate) != net]
        if not new_pins and not any(s == sheet for s,_ in wiring['paths']) and not any(s == sheet for s,_,_ in wiring['labels']):
            continue
        ns = sheets[sheet-1].find('nets')
        n = next((n for n in ns if n.get('name') == net), None)
        if n is None: n = ET.SubElement(ns, 'net', name=net, **{'class': '0'})
        seg = ET.SubElement(n, 'segment')
        for p in new_pins:
            if sch.net_of(p.part, p.pin, p.gate): raise ValueError('Implicit net merge refused')
            ET.SubElement(seg, 'pinref', part=p.part, gate=p.gate, pin=p.pin)
        for si, pts in wiring['paths']:
            if si != sheet: continue
            for a, b in zip(pts, pts[1:]):
                ET.SubElement(seg, 'wire', x1=str(a[0]), y1=str(a[1]), x2=str(b[0]), y2=str(b[1]), width='0', layer='91')
        for si, _, at in wiring['labels']:
            if si == sheet: ET.SubElement(seg, 'label', x=str(at[0]), y=str(at[1]), size='1.27', layer='95')
        for si,at in wiring['junctions']:
            if si == sheet: ET.SubElement(seg,'junction',x=str(at[0]),y=str(at[1]))
    return root
