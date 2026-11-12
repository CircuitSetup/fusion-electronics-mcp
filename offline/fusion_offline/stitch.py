"""Via stitching: choose via positions for a net's pours (e.g. GND) on a grid.

Pure geometry over the exported board (classic EAGLE XML), so it can be
tested without Fusion. A candidate grid point is kept when a via there would
be at least `clearance` away from:
- the board outline (plus the board-edge clearance rule),
- copper of OTHER nets on any layer: traces, vias, pads,
- ALL pads, including the stitched net's own (no via-in-pad),
- holes and restrict circles (layers 41/42/43, i.e. keepouts),
and at least `min_spacing` from other stitching vias and the net's existing
vias. Pads use their real shape: SMDs as rotated rectangles, through-hole
pads as circles (diameter from the drill and the restring rule).
Element transforms follow EAGLE: world = T + M(R(angle) * p), rotate then
mirror X (gerber-verified in Groundplane's fabhub).
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass

from .eagle import parse_rot, Pad as LibraryPad, UnverifiedGeometry


def _f(el, k, d=0.0):
    v = el.get(k)
    return float(v) if v not in (None, "") else d


def _mm(v: str | None, default: float) -> float:
    if not v:
        return default
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*([a-z]*)", v)
    if not m:
        return default
    x, unit = float(m.group(1)), m.group(2)
    return x * 0.0254 if unit == "mil" else x


@dataclass
class Obstacle:
    kind: str        # circle | rect | seg | polygon
    net: str | None  # None = no net (holes, keepouts, unconnected pads)
    is_pad: bool
    data: tuple
    layers: frozenset | None = None   # export copper layers it occupies; None = all (THT pads, vias, holes)
    tag: str | None = None            # route_all's connection id on traces/vias it laid (rip-up)
    restrict_layer: int | None = None
    source_ref: str | None = None
    trace_holes: tuple = ()
    trace_holes_for: tuple | None = None
    is_npth: bool = False
    is_via: bool = False

    @property
    def bounds(self):
        d = self.data
        if self.kind == 'polygon':
            points, half = d[2:]
            return (min(p[0] for p in points)-half, min(p[1] for p in points)-half,
                    max(p[0] for p in points)+half, max(p[1] for p in points)+half)
        if self.kind == 'circle':
            return d[0]-d[2], d[1]-d[2], d[0]+d[2], d[1]+d[2]
        if self.kind == 'rect':
            radius = math.hypot(d[2], d[3])
            return d[0]-radius, d[1]-radius, d[0]+radius, d[1]+radius
        return min(d[0], d[2])-d[4], min(d[1], d[3])-d[4], max(d[0], d[2])+d[4], max(d[1], d[3])+d[4]

    def distance(self, x: float, y: float) -> float:
        """Distance from (x, y) to the obstacle's copper/edge (<= 0 inside)."""
        if self.kind == "circle":
            cx, cy, r = self.data
            return math.hypot(x - cx, y - cy) - r
        if self.kind == "rect":
            cx, cy, hw, hh, ang = self.data
            a = math.radians(-ang)
            dx, dy = x - cx, y - cy
            lx, ly = dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
            ox, oy = abs(lx) - hw, abs(ly) - hh
            return math.hypot(max(ox, 0), max(oy, 0)) + min(max(ox, oy), 0)
        if self.kind == 'polygon':
            points, half = self.data[2:]
            distance = min(Obstacle('seg', None, False, (*a, *b, 0)).distance(x, y)
                           for a, b in zip(points, points[1:] + points[:1]))
            distance = (-distance if _inside_poly(points, x, y) else distance) - half
            # Intersection with the complement of each corridor is conservative at corners.
            return max([distance] + [-hole.distance(x, y) for hole in self.trace_holes])
        x1, y1, x2, y2, half = self.data
        vx, vy = x2 - x1, y2 - y1
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * vx + (y - y1) * vy) / L2))
        return math.hypot(x - (x1 + t * vx), y - (y1 + t * vy)) - half


def _stadium(net, cx, cy, dx, dy, ang, layers) -> Obstacle:
    """An oblong pad (round ends): a segment along its long axis widened by half its short side,
    which is its exact shape (a rectangle would over-claim the corners)."""
    long_, short = max(dx, dy), min(dx, dy)
    a = math.radians(ang + (0 if dx >= dy else 90))
    hx, hy = math.cos(a) * (long_ - short) / 2, math.sin(a) * (long_ - short) / 2
    return Obstacle("seg", net, True, (cx - hx, cy - hy, cx + hx, cy + hy, short / 2), layers)


def _ring(rules: dict, kind: str, drill: float) -> float:
    """Annular ring the design rules give a pad or via, as EAGLE/Fusion compute
    it: rv*drill clamped to [rlMin, rlMax] (outer layers). The copper is the
    larger of this and the library/via diameter, so a 3.0 mm pad on a 2.3 mm
    drill with 25%/0.508 mm rules is really 3.316 mm (seen on the IO
    passthrough: a trace passed our model's 3.0 mm peg and failed Fusion's DRC)."""
    key = "Top" if kind == "Pad" else "Outer"
    rv = _mm(rules.get(f"rv{kind}{key}"), 0.25)
    lo = _mm(rules.get(f"rlMin{kind}{key}"), 0.0)
    hi = _mm(rules.get(f"rlMax{kind}{key}"), 0.508)
    return min(max(rv * drill, lo), hi)


def _xf(px, py, x, y, angle, mirror):
    a = math.radians(angle)
    rx, ry = px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)
    if mirror:
        rx = -rx
    return x + rx, y + ry


def segments_intersect(a, b, c, d):
    def cross(p, q, r):
        return (q[0]-p[0])*(r[1]-p[1]) - (q[1]-p[1])*(r[0]-p[0])
    if (max(a[0], b[0]) < min(c[0], d[0])-1e-10 or max(c[0], d[0]) < min(a[0], b[0])-1e-10
            or max(a[1], b[1]) < min(c[1], d[1])-1e-10 or max(c[1], d[1]) < min(a[1], b[1])-1e-10):
        return False
    return cross(a, b, c)*cross(a, b, d) <= 1e-20 and cross(c, d, a)*cross(c, d, b) <= 1e-20


def _restrict_polygon(polygon, stack, source_ref=None, transform=lambda x, y: (x, y), mirror=False, *, checkpoint=None):
    try:
        points = tuple(transform(float(v.attrib['x']), float(v.attrib['y'])) for v in polygon.findall('vertex'))
        half = float(polygon.get('width', '0')) / 2
        if any(float(v.get('curve', '0')) != 0 for v in polygon.findall('vertex')):
            raise ValueError('curved restrict polygons are not supported')
        if points and points[0] == points[-1]:
            points = points[:-1]
        if (len(points) < 3 or len(set(points)) != len(points) or half < 0
                or not all(math.isfinite(n) for p in points for n in p) or not math.isfinite(half)):
            raise ValueError('invalid restrict vertices or width')
        area2 = sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(points, points[1:]+points[:1]))
        if abs(area2) < 1e-10:
            raise ValueError('restrict polygon has no area')
        edges = list(zip(points, points[1:]+points[:1]))
        checked = 0
        for i, (a, b) in enumerate(edges):
            for j in range(i+2, len(edges)):
                if i == 0 and j == len(edges)-1:
                    continue
                if checkpoint:
                    if checked % 64 == 0:
                        checkpoint()
                    checked += 1
                if segments_intersect(a, b, *edges[j]):
                    raise ValueError('restrict polygon intersects itself')
        layer = int(polygon.attrib['layer'])
        layers = _restrict_layers(layer, stack, mirror, polygon.get("restrictlayers"))
    except (ValueError, KeyError, TypeError) as error:
        raise UnverifiedGeometry(f'{source_ref or "board"}: unverified restrict geometry: {error}') from None
    center = tuple(sum(p[i] for p in points)/len(points) for i in (0, 1))
    return Obstacle('polygon', None, False, (*center, points, half), layers,
                    restrict_layer=layer, source_ref=source_ref)


def _restrict_layers(layer, stack, mirror=False, mask=None):
    # An empty trace-layer set marks vRestrict: vias still check every obstacle.
    if mask is None:
        layers = [] if layer == 43 else [stack[0] if layer == 41 else stack[-1]]
    else:
        if layer == 43 or not re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", mask):
            raise ValueError("unsupported native restrictlayers mask")
        layers = list(map(int, mask.split(",")))
        if len(set(layers)) != len(layers) or not set(layers) <= set(stack):
            raise ValueError("native restrictlayers mask must name distinct enabled copper layers")
    if mirror:
        layers = [stack[-1 - stack.index(layer)] for layer in layers]
    return frozenset(layers)


def _restrict_shape(shape, stack, source_ref=None, transform=lambda x, y: (x, y), mirror=False, *, checkpoint=None):
    """Normalize supported native filled outlines without changing the exported XML."""
    try:
        polygon = ET.Element('polygon', layer=shape.attrib['layer'])
        if shape.tag == 'rectangle':
            if (shape.get('rot', 'R0') != 'R0' or len(shape)
                    or set(shape.attrib) - {'x1', 'y1', 'x2', 'y2', 'layer', 'rot', 'restrictlayers'}):
                raise ValueError('rotated or nested restrict rectangles are unsupported')
            x1, y1, x2, y2 = (float(shape.attrib[k]) for k in ('x1', 'y1', 'x2', 'y2'))
            if not all(math.isfinite(v) for v in (x1, y1, x2, y2)) or x1 == x2 or y1 == y2:
                raise ValueError('restrict rectangle must have finite nonzero area')
            for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
                ET.SubElement(polygon, 'vertex', x=str(x), y=str(y))
        else:
            if ([c.tag for c in shape] != ['polygonoutlineobjects', 'polygonoutlinesegments']
                    or set(shape.attrib) - {'layer', 'restrictlayers'}):
                raise ValueError('unsupported native restrict outline or apertures')
            objects, segments = list(shape)
            if any(v.tag != 'vertex' or set(v.attrib) - {'x', 'y', 'curve'} or len(v)
                   for group in (objects, segments) for v in group):
                raise ValueError('unsupported native restrict outline segments')
            def vertices(group):
                return [(float(v.attrib['x']), float(v.attrib['y']), float(v.get('curve', '0'))) for v in group]
            points, closed = vertices(objects), vertices(segments)
            if not points or closed != points + points[:1]:
                raise ValueError('native restrict outline must be a single matching closed contour')
            for vertex in objects:
                ET.SubElement(polygon, 'vertex', vertex.attrib)
        obstacle = _restrict_polygon(polygon, stack, source_ref, transform, mirror, checkpoint=checkpoint)
        mask = shape.get('restrictlayers')
        if shape.tag == 'polygonshape' and mask is None and obstacle.restrict_layer != 43:
            raise ValueError('missing native restrictlayers mask')
        obstacle.layers = _restrict_layers(obstacle.restrict_layer, stack, mirror, mask)
        return obstacle
    except (ValueError, KeyError, TypeError) as error:
        raise UnverifiedGeometry(f'{source_ref or "board"}: unverified restrict geometry: {error}') from None


def board_obstacles(root: ET.Element, *, checkpoint=None) -> tuple[list[Obstacle], dict, list[tuple[float, float, float, float]]]:
    """(obstacles, rules, outline segments) from a board XML root."""
    board = root.find("./drawing/board")
    from .annular import copper as annular_copper
    from .design import parse_board_design
    stack = parse_board_design(root).copper_layers
    rules = {p.get("name"): p.get("value") for p in board.iterfind("./designrules/param")}
    obs: list[Obstacle] = []
    # pad -> net from the signals' contactrefs
    pad_net = {}
    for s in board.iterfind("./signals/signal"):
        if checkpoint:
            checkpoint()
        for c in s.iterfind("contactref"):
            if checkpoint:
                checkpoint()
            pad_net[(c.get("element"), c.get("pad"))] = s.get("name")
        for w in s.iterfind("wire"):
            if checkpoint:
                checkpoint()
            if w.get("layer") in ("19",):
                continue
            # arcs are kept as their chord, widened by the sagitta (conservative)
            c = abs(math.radians(_f(w, "curve")))
            chord = math.hypot(_f(w, "x2") - _f(w, "x1"), _f(w, "y2") - _f(w, "y1"))
            sag = 0.0 if c < 1e-9 else chord / (2 * math.sin(c / 2)) * (1 - math.cos(c / 2))
            obs.append(Obstacle("seg", s.get("name"), False,
                                (_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2"), _f(w, "width") / 2 + sag),
                                frozenset({int(w.get("layer"))}), w.get("mcp_conn")))
        for v in s.iterfind("via"):
            if checkpoint:
                checkpoint()
            diameters = annular_copper(rules, "Via", _f(v, "drill"), _f(v, "diameter"), len(stack) > 2)
            for side, layers in (("outer", (stack[0], stack[-1])), ("inner", stack[1:-1])):
                if layers:
                    obs.append(Obstacle("circle", s.get("name"), False,
                                        (_f(v, "x"), _f(v, "y"), diameters[side] / 2),
                                        frozenset(layers), v.get("mcp_conn"), is_via=True))
    pkgs = {}
    for lib in board.iterfind("./libraries/library"):
        if checkpoint:
            checkpoint()
        for pk in lib.iterfind("./packages/package"):
            if checkpoint:
                checkpoint()
            pkgs[(lib.get("name"), pk.get("name"))] = pk
    for el in board.iterfind("./elements/element"):
        if checkpoint:
            checkpoint()
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        ang, mir = parse_rot(el.get("rot"))
        ex, ey = _f(el, "x"), _f(el, "y")
        for smd in pk.iterfind("smd"):
            if checkpoint:
                checkpoint()
            cx, cy = _xf(_f(smd, "x"), _f(smd, "y"), ex, ey, ang, mir)
            prot, _ = parse_rot(smd.get("rot"))
            a = (ang + prot) * (-1 if mir else 1)
            side = stack[-1] if (int(smd.get("layer")) == stack[0]) == mir else stack[0]
            net_ = pad_net.get((el.get("name"), smd.get("name")))
            dx, dy = _f(smd, "dx"), _f(smd, "dy")
            if _f(smd, "roundness") >= 100:
                obs.append(_stadium(net_, cx, cy, dx, dy, a, frozenset({side})))
            else:
                obs.append(Obstacle("rect", net_, True, (cx, cy, dx / 2, dy / 2, a), frozenset({side})))
        for pad in pk.iterfind("pad"):
            if checkpoint:
                checkpoint()
            LibraryPad.from_xml(pad).require_supported_geometry(el.get("name"))
            cx, cy = _xf(_f(pad, "x"), _f(pad, "y"), ex, ey, ang, mir)
            drill = _f(pad, "drill")
            dia = max(_f(pad, "diameter"), drill + 2 * _ring(rules, "Pad", drill))
            net_ = pad_net.get((el.get("name"), pad.get("name")))
            shape = (pad.get("shape") or "round").lower()
            if shape in ("long", "offset"):
                # LONG: elongated to 2x the diameter along the pad's x (psElongationLong 100%), with
                # round ends; OFFSET: the same shape, shifted to one side
                prot, _ = parse_rot(pad.get("rot"))
                a = (ang + prot) * (-1 if mir else 1)
                if shape == "offset":
                    ra = math.radians(a)
                    cx, cy = cx + math.cos(ra) * dia / 2, cy + math.sin(ra) * dia / 2
                obs.append(_stadium(net_, cx, cy, 2 * dia, dia, a, None))
            else:
                obs.append(Obstacle("circle", net_, True, (cx, cy, dia / 2)))
        for h in pk.iterfind("hole"):
            if checkpoint:
                checkpoint()
            cx, cy = _xf(_f(h, "x"), _f(h, "y"), ex, ey, ang, mir)
            obs.append(Obstacle("circle", None, True, (cx, cy, _f(h, "drill") / 2), is_npth=True))
        for shape in list(pk.findall('rectangle')) + list(pk.findall('polygonshape')):
            if checkpoint:
                checkpoint()
            if shape.get('layer') in ('41', '42', '43'):
                obs.append(_restrict_shape(shape, stack, el.get('name'),
                           lambda x, y: _xf(x, y, ex, ey, ang, mir), mir, checkpoint=checkpoint))
        for polygon in pk.findall('polygon'):
            if checkpoint:
                checkpoint()
            if polygon.get('layer') in ('41', '42', '43'):
                obs.append(_restrict_polygon(polygon, stack, el.get('name'),
                           lambda x, y: _xf(x, y, ex, ey, ang, mir), mir, checkpoint=checkpoint))
    outline = []
    for w in board.iterfind("./plain/wire"):
        if checkpoint:
            checkpoint()
        if w.get("layer") == "20":
            outline.append((_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2")))
    for h in board.iterfind("./plain/hole"):
        if checkpoint:
            checkpoint()
        obs.append(Obstacle("circle", None, True, (_f(h, "x"), _f(h, "y"), _f(h, "drill") / 2), is_npth=True))
    for c in board.iterfind("./plain/circle"):
        if checkpoint:
            checkpoint()
        if c.get("layer") in ("41", "42", "43") and _f(c, "width") == 0:
            layer = int(c.attrib["layer"])
            try:
                layers = _restrict_layers(layer, stack, mask=c.get("restrictlayers"))
            except ValueError as error:
                raise UnverifiedGeometry(f"board: unverified restrict circle: {error}") from None
            obs.append(Obstacle("circle", None, True, (_f(c, "x"), _f(c, "y"), _f(c, "radius")),
                                layers, restrict_layer=layer))
    for shape in list(board.findall('./plain/rectangle')) + list(board.findall('./plain/polygonshape')):
        if checkpoint:
            checkpoint()
        if shape.get('layer') in ('41', '42', '43'):
            obs.append(_restrict_shape(shape, stack, checkpoint=checkpoint))
    for polygon in board.findall('./plain/polygon'):
        if checkpoint:
            checkpoint()
        if polygon.get('layer') in ('41', '42', '43'):
            obs.append(_restrict_polygon(polygon, stack, checkpoint=checkpoint))
    return obs, rules, outline


def outline_vertices(segments, *, checkpoint=None):
    """Walk a single closed contour from unordered straight Dimension segments."""
    neighbors = defaultdict(set)
    for x1, y1, x2, y2 in segments:
        if checkpoint:
            checkpoint()
        a, b = (x1, y1), (x2, y2)
        if a == b or b in neighbors[a]:
            raise ValueError("outline contains a zero-length or repeated segment")
        neighbors[a].add(b)
        neighbors[b].add(a)
    if len(neighbors) < 3 or any(len(row) != 2 for row in neighbors.values()):
        raise ValueError("outline is open or branched")
    start = min(neighbors)
    vertices = [start]
    previous, cursor = start, min(neighbors[start])
    while cursor != start:
        if checkpoint:
            checkpoint()
        vertices.append(cursor)
        previous, cursor = cursor, next(iter(neighbors[cursor] - {previous}))
    if len(vertices) != len(neighbors):
        raise UnverifiedGeometry("outline has more than one closed contour")
    return tuple(vertices)


def _inside_poly(poly, x, y) -> bool:
    """Even-odd point-in-polygon over a vertex list."""
    c = False
    for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            c = not c
    return c


def _inside(x, y, segs) -> bool:
    """Even-odd point-in-polygon over the outline's straight segments."""
    inside = False
    for x1, y1, x2, y2 in segs:
        if (y1 > y) != (y2 > y):
            xc = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xc > x:
                inside = not inside
    return inside


def plan_stitching(root: ET.Element, net: str, pitch: float = 2.0, drill: float | None = None,
                   clearance: float | None = None, min_spacing: float | None = None,
                   keep_away: dict[str, float] | None = None, diameter: float | None = None,
                   under_parts: str | None = None, pin_field_margin: float = 2.0) -> dict:
    """keep_away: {net regex: mm} larger clearances to some nets' copper, e.g. keep
    stitching vias as far from impedance-controlled pairs as the pours are.
    under_parts: refdes regex of parts vias may go under (big through-hole connectors: open
    ground under the body); their pads still keep the clearance. Other parts stay via-free."""
    import re
    away = [(re.compile(k), float(v)) for k, v in (keep_away or {}).items()]
    obs, rules, outline = board_obstacles(root)
    if not outline:
        raise ValueError("board has no outline")
    # 0.3 mm is the smallest drill at JLC's standard price; smaller costs extra, so the design's
    # minimum (often 0.2) is NOT used unless asked for
    drill = drill or max(0.3, _mm(rules.get("msDrill"), 0.3))
    ring = max(0.25 * drill, _mm(rules.get("rlMinViaOuter"), 0.2032))
    r = max(drill + 2 * ring, diameter or 0.6) / 2
    allow = re.compile(under_parts) if under_parts else None
    bodies = [b for ref, b in part_bodies(root) if not (allow and allow.fullmatch(ref))]
    if allow:
        # under a connector's body is fine, its pin field is not (the user removed every via
        # between and just beside big connectors' pin rows, kept the overhang ones)
        bodies += [b for ref, b in pin_fields(root, pin_field_margin) if allow.fullmatch(ref)]
    clr = clearance if clearance is not None else max(_mm(rules.get("mdWireVia"), 0.2), _mm(rules.get("mdPadVia"), 0.2))
    edge = _mm(rules.get("mdCopperDimension"), 0.3)
    spacing = min_spacing if min_spacing is not None else pitch * 0.9
    xs = [v for s in outline for v in (s[0], s[2])]
    ys = [v for s in outline for v in (s[1], s[3])]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    edge_segs = [Obstacle("seg", None, False, (a, b, c, d, 0.0)) for a, b, c, d in outline]
    existing = [(o.data[0], o.data[1]) for o in obs if o.kind == "circle" and o.net == net and not o.is_pad]
    # where another net pours (a power island over the plane), a via would only nick that pour
    # without tying the net's copper together: skip those spots
    foreign = []
    for sg in root.iterfind("./drawing/board/signals/signal"):
        if sg.get("name") == net:
            continue
        for pg in sg.iterfind("polygon"):
            foreign.append([(_f(v, "x"), _f(v, "y")) for v in pg.iterfind("vertex")])

    def in_foreign(x, y):
        for poly in foreign:
            inside = False
            for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
                if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                    inside = not inside
            if inside:
                return True
        return False
    chosen: list[tuple[float, float]] = []
    reasons = {"edge": 0, "copper": 0, "pad": 0, "keepout_or_hole": 0, "spacing": 0}
    nx, ny = int((x1 - x0) / pitch), int((y1 - y0) / pitch)
    ox, oy = x0 + ((x1 - x0) - nx * pitch) / 2, y0 + ((y1 - y0) - ny * pitch) / 2
    for i in range(nx + 1):
        for j in range(ny + 1):
            x, y = round(ox + i * pitch, 4), round(oy + j * pitch, 4)
            if not _inside(x, y, outline) or min(e.distance(x, y) for e in edge_segs) < edge + r:
                reasons["edge"] += 1
                continue
            if any(b[0] - r < x < b[2] + r and b[1] - r < y < b[3] + r for b in bodies):
                reasons["pad"] += 1           # not under a part
                continue
            if in_foreign(x, y):
                reasons.setdefault("other_net_pour", 0)
                reasons["other_net_pour"] += 1
                continue
            bad = None
            for o in obs:
                d = o.distance(x, y)
                if o.net is None:
                    if d < r + (clr if o.is_pad else 0.0):
                        bad = "keepout_or_hole" if not o.is_pad or o.kind == "circle" else "pad"
                        break
                elif o.is_pad:
                    if d < r + clr:
                        bad = "pad"
                        break
                elif o.net != net and d < r + max([clr] + [v for rx, v in away if rx.search(o.net)]):
                    bad = "copper"
                    break
            if bad:
                reasons[bad] += 1
                continue
            if any(math.hypot(x - a, y - b) < spacing for a, b in chosen + existing):
                reasons["spacing"] += 1
                continue
            chosen.append((x, y))
    return {"net": net, "vias": chosen, "drill_mm": drill, "diameter_mm": round(2 * r, 4),
            "clearance_mm": clr, "edge_clearance_mm": edge, "pitch_mm": pitch, "rejected": reasons}


def pad_center(root: ET.Element, ref: str, pad: str) -> tuple[float, float] | None:
    board = root.find("./drawing/board")
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    for el in board.iterfind("./elements/element"):
        if el.get("name") != ref:
            continue
        pk = pkgs.get((el.get("library"), el.get("package")))
        ang, mir = parse_rot(el.get("rot"))
        for p in list(pk.iterfind("smd")) + list(pk.iterfind("pad")):
            if p.get("name") == pad:
                return _xf(_f(p, "x"), _f(p, "y"), _f(el, "x"), _f(el, "y"), ang, mir)
    return None


def pin_fields(root: ET.Element, margin: float = 2.0) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(refdes, box) around each part's pads, grown by `margin`."""
    from .eagle import parse_rot
    board = root.find("./drawing/board")
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    out = []
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        ang, mir = parse_rot(el.get("rot"))
        pts = [_xf(_f(p, "x"), _f(p, "y"), _f(el, "x"), _f(el, "y"), ang, mir)
               for p in list(pk.iterfind("pad")) + list(pk.iterfind("smd"))]
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            out.append((el.get("name"), (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)))
    return out


def part_bodies(root: ET.Element, margin: float = 0.15) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(ref, (x0, y0, x1, y1)) per part: the box around its pads' copper and its silk / documentation
    outline: the area under its body, where a via does not belong (on a test board vias landed
    between cap pads, and inside an electrolytic's can outline)."""
    board = root.find("./drawing/board")
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    out = []
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        ang, mir = parse_rot(el.get("rot"))
        xs, ys = [], []
        for p in list(pk.iterfind("smd")) + list(pk.iterfind("pad")):
            hw = (_f(p, "dx") or _f(p, "diameter") or 1.0) / 2
            hh = (_f(p, "dy") or _f(p, "diameter") or 1.0) / 2
            for dx, dy in ((-hw, -hh), (hw, hh), (-hw, hh), (hw, -hh)):
                x, y = _xf(_f(p, "x") + dx, _f(p, "y") + dy, _f(el, "x"), _f(el, "y"), ang, mir)
                xs.append(x)
                ys.append(y)
        for w in pk.iterfind("wire"):          # the outline drawn on silk / documentation (a can, a body)
            if w.get("layer") in ("21", "22", "51", "52"):
                for k in (("x1", "y1"), ("x2", "y2")):
                    x, y = _xf(_f(w, k[0]), _f(w, k[1]), _f(el, "x"), _f(el, "y"), ang, mir)
                    xs.append(x)
                    ys.append(y)
        if len(xs) >= 8:                       # two pads or more: there is a body between them
            out.append((el.get("name"), (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)))
    return out


def plan_fanout(root: ET.Element, ref: str, pad: str, net: str, trace_width: float = 0.25,
                max_dist: float = 3.0, step: float = 0.1, drill: float | None = None,
                diameter: float | None = None, trace_layer: int | None = None) -> dict | None:
    """Nearest via position for a pad's fanout: the via clears other nets'
    copper, every pad (including this one: no via-in-pad), holes, keepouts and
    the edge; and the straight trace from the pad centre to it clears other
    nets' copper and restrictions on its actual layer. Returns
    {via: (x, y), from: (x, y), distance} or None."""
    obs, rules, outline = board_obstacles(root)
    c = pad_center(root, ref, pad)
    if c is None:
        raise KeyError(f"no pad {ref}.{pad}")
    from .router import pad_target
    pad_layers = pad_target(root, ref, pad)[2]
    trace_layer = pad_layers[0] if trace_layer is None else trace_layer
    if trace_layer not in pad_layers:
        raise ValueError('Fanout trace layer does not reach the source pad')
    # 0.3 mm is the smallest drill at JLC's standard price; smaller costs extra, so the design's
    # minimum (often 0.2) is NOT used unless asked for
    drill = drill or max(0.3, _mm(rules.get("msDrill"), 0.3))
    ring = max(0.25 * drill, _mm(rules.get("rlMinViaOuter"), 0.2032))
    r = max(drill + 2 * ring, diameter or 0.6) / 2
    bodies = [b for _, b in part_bodies(root)]
    clr = max(_mm(rules.get("mdWireVia"), 0.2), _mm(rules.get("mdPadVia"), 0.2))
    wclr = max(_mm(rules.get("mdWireWire"), 0.15), _mm(rules.get("mdWirePad"), 0.15))
    edge = _mm(rules.get("mdCopperDimension"), 0.3)
    edges = [Obstacle("seg", None, False, (a, b, cc, d, 0.0)) for a, b, cc, d in outline]
    others = [o for o in obs if o.net != net and o.net is not None]
    blockers = [o for o in obs if o.net is None or o.is_pad]
    trace_obstacles = [o for o in obs if o.net != net and (o.layers is None or trace_layer in o.layers)]
    best = None
    n = int(max_dist / step)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            x, y = c[0] + i * step, c[1] + j * step
            dist = math.hypot(x - c[0], y - c[1])
            if dist > max_dist or (best and dist >= best["distance"]):
                continue
            if not _inside(x, y, outline) or min(e.distance(x, y) for e in edges) < edge + r:
                continue
            if any(b[0] - r < x < b[2] + r and b[1] - r < y < b[3] + r for b in bodies):
                continue                       # not under a part
            if any(o.distance(x, y) < r + (clr if (o.is_pad or o.net) else 0.0) for o in blockers + others):
                continue
            ok = True
            for k in range(21):
                px, py = c[0] + (x - c[0]) * k / 20, c[1] + (y - c[1]) * k / 20
                if any(o.distance(px, py) < trace_width / 2 + wclr for o in trace_obstacles):
                    ok = False
                    break
            if ok:
                best = {"via": (round(x, 4), round(y, 4)), "from": (round(c[0], 4), round(c[1], 4)),
                        "distance": round(dist, 3), "drill": drill, "diameter": round(2 * r, 4)}
    return best


def pad_centres(root: ET.Element) -> dict[str, list[tuple[float, float, "Obstacle"]]]:
    """net -> [(cx, cy, pad shape)] for every connected pad."""
    obs, _, _ = board_obstacles(root)
    out: dict[str, list] = {}
    for o in obs:
        if o.is_pad and o.net:
            out.setdefault(o.net, []).append((o.data[0], o.data[1], o))
    return out


def stub_candidates(root: ET.Element, tol: float = 1e-3) -> list[dict]:
    """Segments with an end that is not at a same-net wire end/interior, via
    or pad CENTRE (Fusion's wire-stub rule). Each carries `on_pad`: the
    centre of a same-net pad whose copper the loose end lies on (an
    off-centre end, electrically connected: fix by snapping), or None (a
    true dangling end: delete)."""
    board = root.find("./drawing/board")
    pads = pad_centres(root)
    out = []
    for s in board.iterfind("./signals/signal"):
        net = s.get("name")
        wires = [w for w in s.iterfind("wire") if w.get("layer") != "19"]
        vias = [(_f(v, "x"), _f(v, "y")) for v in s.iterfind("via")]
        segs = [(_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2"), w.get("layer"), _f(w, "width")) for w in wires]
        for i, (x1, y1, x2, y2, layer, width) in enumerate(segs):
            for (px, py) in ((x1, y1), (x2, y2)):
                ok = any(math.hypot(px - a, py - b) <= tol for a, b in vias) or                     any(math.hypot(px - cx, py - cy) <= tol for cx, cy, _ in pads.get(net, []))
                if not ok:
                    ok = any(j != i and l2 == layer and
                             Obstacle("seg", net, False, (a1, b1, a2, b2, 0.0)).distance(px, py) <= tol
                             for j, (a1, b1, a2, b2, l2, _) in enumerate(segs))
                if not ok:
                    on = next(((cx, cy) for cx, cy, o in pads.get(net, []) if o.distance(px, py) <= tol), None)
                    # the last point along the segment (from its other end) where something joins it
                    ox, oy = (x2, y2) if (px, py) == (x1, y1) else (x1, y1)
                    L = math.hypot(px - ox, py - oy)
                    joins = [(a, b) for a, b in vias] + [q for j, (a1, b1, a2, b2, l2, _) in enumerate(segs)
                                                         if j != i and l2 == layer for q in ((a1, b1), (a2, b2))]
                    seg = Obstacle("seg", net, False, (x1, y1, x2, y2, 0.0))
                    ts = [((q[0] - ox) * (px - ox) + (q[1] - oy) * (py - oy)) / (L * L) for q in joins
                          if L > 0 and seg.distance(*q) <= tol]
                    ts = [t for t in ts if 1e-6 < t < 1 - 1e-6]
                    keep_to = None
                    if ts:
                        t = max(ts)
                        keep_to = (round(ox + (px - ox) * t, 4), round(oy + (py - oy) * t, 4))
                    out.append({"net": net, "layer": int(layer), "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                "width": width, "dangling": (px, py), "on_pad": on, "keep_to": keep_to,
                                "length": round(math.hypot(x2 - x1, y2 - y1), 4)})
                    break
    return out


def find_stubs(root: ET.Element, tol: float = 1e-3) -> list[dict]:
    """Copper wire segments with an end that connects to nothing: not on a
    same-net pad's copper (anywhere on the pad, as Fusion counts it, not only
    its centre), a same-net via, or another same-net wire on that layer.
    Pours are ignored (a stub inside a pour is still a stub to DRC)."""
    board = root.find("./drawing/board")
    obs, _, _ = board_obstacles(root)
    pads_by_net: dict[str, list[Obstacle]] = {}
    for o in obs:
        if o.is_pad and o.net:
            pads_by_net.setdefault(o.net, []).append(o)
    stubs = []
    for s in board.iterfind("./signals/signal"):
        net = s.get("name")
        wires = [w for w in s.iterfind("wire") if w.get("layer") != "19"]
        vias = [(_f(v, "x"), _f(v, "y")) for v in s.iterfind("via")]
        segs = [(_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2"), w.get("layer"), _f(w, "width")) for w in wires]
        for i, (x1, y1, x2, y2, layer, width) in enumerate(segs):
            for (px, py) in ((x1, y1), (x2, y2)):
                connected = any(math.hypot(px - a, py - b) <= tol for a, b in vias) or                     any(p.distance(px, py) <= tol for p in pads_by_net.get(net, []))
                if not connected:
                    for j, (a1, b1, a2, b2, l2, _) in enumerate(segs):
                        if j == i or l2 != layer:
                            continue
                        if Obstacle("seg", net, False, (a1, b1, a2, b2, 0.0)).distance(px, py) <= tol:
                            connected = True
                            break
                if not connected:
                    stubs.append({"net": net, "layer": int(layer), "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                  "dangling": (px, py), "length": round(math.hypot(x2 - x1, y2 - y1), 4)})
                    break
    return stubs
