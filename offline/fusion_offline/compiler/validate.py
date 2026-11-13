"""Cross-file validation over saved EAGLE documents."""
from collections import Counter, defaultdict
from itertools import pairwise
import math
import xml.etree.ElementTree as ET

from fusion_mcp.addin.FusionElectronicsMCP.eagle_import import signature

from ..annular import copper as annular_copper
from ..design import parse_board_design, parse_schematic_design, transform
from ..eagle import UnverifiedGeometry, parse_rot
from ..footprints import packages
from ..gerbers import design_holes
from ..junctions import overlapping_runs
from ..net_classes import _mm as rule_mm
from ..pairs import _seg_dist
from ..placement_check import pads as placed_pads
from ..part_placement import verify_names
from ..router import route_minimums
from ..stitch import _inside_poly, _restrict_polygon, board_obstacles, outline_vertices, segments_intersect
from .board import route_geometry, route_owners
from .canonical import canonical_digest
from .manifest import finding_status
from .model import _validate_source_xml

_RESTRICT = {"41", "42", "43"}


def _finding(check, status, detail, required=True):
    return {"check": check, "status": status, "detail": detail, "required": required}


def _check(check, operation):
    try:
        operation()
    except UnverifiedGeometry as exc:
        return _finding(check, "unverified", str(exc))
    except (ValueError, KeyError) as exc:
        return _finding(check, "fail", str(exc))
    return _finding(check, "pass", "")


def _unique(names, kind):
    if not all(names) or len({name.casefold() for name in names}) != len(names):
        raise ValueError(f"{kind} must have unique names")


def _contacts(sch_root, board, sch, pads):
    _unique([p.get("name") for p in sch_root.findall("./drawing/schematic/parts/part")], "schematic parts")
    _unique([e.get("name") for e in board.findall("./elements/element")], "board elements")
    _unique([s.get("name") for s in board.findall("./signals/signal")], "board signals")
    _unique(list(sch.nets), "schematic nets")
    for sheet in sch_root.findall("./drawing/schematic/sheets/sheet"):
        _unique([n.get("name") for n in sheet.findall("./nets/net")], "nets on each sheet")

    expected = {}
    assigned_pins = set()
    actual_pins = {(p.part, p.gate, p.pin) for p in sch.pins}
    for name, net in sch.nets.items():
        contacts = set()
        for pin in net.pins:
            key = (pin.part, pin.gate, pin.pin)
            if key not in actual_pins or key in assigned_pins:
                raise ValueError(f"{name}: unknown or repeated schematic pin {'.'.join(key)}")
            assigned_pins.add(key)
            part = sch.parts[pin.part]
            if part.package:
                physical = {(part.name, pad) for gate, pin_name, pad in part.connects
                            if (gate, pin_name) == (pin.gate, pin.pin)}
                if not physical or not physical <= pads.keys():
                    raise ValueError(f"{name}: missing physical mapping for {'.'.join(key)}")
                contacts.update(physical)
        if contacts:
            expected[name] = contacts

    actual = {}
    for signal in board.findall("./signals/signal"):
        name = signal.attrib["name"]
        refs = [(ref.attrib["element"], ref.attrib["pad"]) for ref in signal.findall("contactref")]
        if len(set(refs)) != len(refs):
            raise ValueError(f"{name}: repeated board contact")
        actual[name] = set(refs)
    if actual != expected:
        raise ValueError("exact schematic/board contact mappings differ")
    counts = Counter(contact for contacts in actual.values() for contact in contacts)
    if any(count > 1 for count in counts.values()):
        raise ValueError("a physical pad belongs to multiple nets")


def _point(node, x="x", y="y"):
    point = float(node.attrib[x]), float(node.attrib[y])
    if not all(math.isfinite(value) for value in point):
        raise ValueError("nonfinite board coordinates")
    return point


def _outline(board):
    contour = board.findall("./plain/*[@layer='20']")
    if any(edge.tag != "wire" or float(edge.get("curve", "0")) != 0 for edge in contour):
        raise UnverifiedGeometry("outline requires a straight, single closed wire contour")
    if len(contour) < 3:
        raise ValueError("outline must have at least three segments")
    vertices = outline_vertices(
        (*_point(edge, "x1", "y1"), *_point(edge, "x2", "y2")) for edge in contour
    )
    polygon = ET.Element("polygon", layer="41")
    for x, y in vertices:
        ET.SubElement(polygon, "vertex", x=str(x), y=str(y))
    try:
        _restrict_polygon(polygon, (1, 16), "outline")
    except UnverifiedGeometry as exc:
        raise ValueError(str(exc)) from exc
    return vertices


def _inside_outline(outline, point):
    return _inside_poly(outline, *point) or any(
        segments_intersect(a, b, point, point) for a, b in pairwise((*outline, outline[0]))
    )


def _trace_inside(outline, a, b):
    if not (_inside_outline(outline, a) and _inside_outline(outline, b)):
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    axis = int(abs(dy) > abs(dx))
    cuts = {0.0, 1.0}
    for c, d in pairwise((*outline, outline[0])):
        if not segments_intersect(a, b, c, d):
            continue
        sx, sy = d[0] - c[0], d[1] - c[1]
        denominator = dx * sy - dy * sx
        if denominator:
            t = ((c[0] - a[0]) * sy - (c[1] - a[1]) * sx) / denominator
            cuts.add(max(0.0, min(1.0, t)))
        else:
            # Collinear boundary spans can separate inside and outside intervals.
            cuts.update(max(0.0, min(1.0, (p[axis] - a[axis]) / (b[axis] - a[axis]))) for p in (c, d))
    for start, end in pairwise(sorted(cuts)):
        t = (start + end) / 2
        if not _inside_outline(outline, (a[0] + t * dx, a[1] + t * dy)):
            return False
    return True


def _placements(board, outline, exemptions):
    if not outline:
        raise UnverifiedGeometry("cannot check placements without a supported outline")
    for element in board.findall("./elements/element"):
        if element.attrib["name"] not in exemptions and not _inside_outline(outline, _point(element)):
            raise ValueError(f"{element.attrib['name']}: placement is outside the board outline")


def _smd_layer(pad, stack, mirrored):
    layer = int(pad.attrib["layer"])
    if layer not in (stack[0], stack[-1]):
        raise ValueError(f"{pad.attrib['name']}: unsupported SMD layer {layer}")
    return stack[-1] if (layer == stack[0]) == mirrored else stack[0]


def _pad_centres(board, schematic, stack):
    packages = {(lib.attrib["name"], pkg.attrib["name"]): pkg
                for lib in board.findall("./libraries/library") for pkg in lib.findall("./packages/package")}
    centers = {}
    for el in board.findall("./elements/element"):
        ref, lib, package = (el.attrib[key] for key in ("name", "library", "package"))
        part = schematic.parts.get(ref)
        if not part or (lib, package) != (part.library, part.package):
            raise ValueError(f"{ref}: board library and package differ from the schematic")
        pkg = packages.get((lib, package))
        if pkg is None:
            raise ValueError(f"{ref}: board package is missing")
        pads = [pad for pad in pkg if pad.tag in ("pad", "smd")]
        _unique([pad.get("name") for pad in pads], f"{ref} pads")
        x, y = _point(el)
        angle, mirror = parse_rot(el.get("rot"))
        for pad in pads:
            px, py = transform(*_point(pad), x, y, angle, mirror)
            layers = stack
            if pad.tag == "smd":
                layers = (_smd_layer(pad, stack, mirror),)
            centers[ref, pad.attrib["name"]] = [(px, py, layer) for layer in layers]
    return centers


class _Components:
    def __init__(self):
        self.parents = {}

    def root(self, point):
        self.parents.setdefault(point, point)
        while self.parents[point] != point:
            self.parents[point] = self.parents[self.parents[point]]
            point = self.parents[point]
        return point

    def join(self, points):
        first, *rest = points
        root = self.root(first)
        for point in rest:
            self.parents[self.root(point)] = root


def _fill_contacts(fill, nodes, wires, footprints):
    """Join an island only where it overlaps sampled copper or a trace centerline."""
    rings = fill["rings"]
    layer = fill["layer"]

    def inside(point):
        if not _inside_poly(rings[0], *point) or any(_inside_poly(hole, *point) for hole in rings[1:]):
            return False
        return all(_seg_dist(point, point, a, b) > 1e-7
                   for ring in rings for a, b in pairwise((*ring, ring[0])))

    touched = {node for node in nodes if node[2] == layer and (
        any(inside(point) for point in footprints.get(node, (node[:2],)))
    )}
    for a, b in wires:
        if a[2] != layer or a in touched or b in touched:
            continue
        dx, dy = b[0] - a[0], b[1] - a[1]
        cuts = {0.0, 1.0}
        for ring in rings:
            for c, d in pairwise((*ring, ring[0])):
                ex, ey = d[0] - c[0], d[1] - c[1]
                denominator = dx * ey - dy * ex
                if abs(denominator) < 1e-12:
                    continue
                px, py = c[0] - a[0], c[1] - a[1]
                t, u = (px * ey - py * ex) / denominator, (px * dy - py * dx) / denominator
                if 0 < t < 1 and 0 <= u <= 1:
                    cuts.add(t)
        if any(inside((a[0] + dx * (lo + hi) / 2, a[1] + dy * (lo + hi) / 2))
               for lo, hi in pairwise(sorted(cuts))):
            touched.add(a)
    return sorted(touched)


def _route_components(signal, pads, stack, allowed, via_limit, via_owners, outline, rules, via_spans, minimums,
                      *, fills=(), has_pour=False, fill_pad_copper=None):
    """Return contact groups proved by pad centres, straight traces and through vias."""
    components = _Components()
    errors, unknown = [], []
    minimum_width, minimum_drill = minimums
    anchors = set()
    footprints = {}
    for contact in signal.contacts:
        centers = pads[contact]
        components.join(centers)
        anchors.update(centers)
        if fill_pad_copper:
            for node in centers:
                footprints[node] = fill_pad_copper.get((*contact, node[2]), ())

    degree = Counter()
    seen_wires = set()
    wires = []
    runs_by_layer = defaultdict(list)
    for wire in signal.wires:
        if wire.layer == 19:
            continue
        a, b = (wire.x1, wire.y1, wire.layer), (wire.x2, wire.y2, wire.layer)
        if wire.layer not in allowed:
            errors.append(f"wire uses absent or disallowed layer {wire.layer}")
        elif wire.curve:
            unknown.append("curved copper traces are unsupported")
        elif a == b or wire.width <= 0 or frozenset((a, b)) in seen_wires:
            errors.append("zero-length, zero-width, or duplicate copper trace")
        elif not all(math.isfinite(number) for number in (*a, *b, wire.width)):
            errors.append("nonfinite copper trace geometry")
        else:
            runs_by_layer[wire.layer].append((a[:2], b[:2]))
            seen_wires.add(frozenset((a, b)))
            components.join((a, b))
            degree.update((a, b))
            wires.append((a, b))
            if outline and not _trace_inside(outline, a[:2], b[:2]):
                errors.append("trace leaves the board outline")
            if wire.width < minimum_width - 1e-6:
                errors.append(f"trace at {a[:2]} on layer {wire.layer} is {wire.width:g} mm wide, below {minimum_width:g} mm")

    for layer, runs in runs_by_layer.items():
        ordered = sorted(tuple(sorted(run)) for run in runs)
        for index, (a, b) in enumerate(ordered):
            for next_index in range(index + 1, len(ordered)):
                c, d = ordered[next_index]
                if c[0] > b[0] + 0.000071:
                    break
                if overlapping_runs(a, b, c, d):
                    errors.append(f"overlapping collinear traces on layer {layer}: {a}–{b} and {c}–{d}")

    via_points, via_nodes = set(), set()
    vias = []
    for via, owner in zip(signal.vias, via_owners):
        point = via.x, via.y
        nodes = [(*point, layer) for layer in stack]
        if (via.extent != f"{stack[0]}-{stack[-1]}"
                or via_spans is not None and (stack[0], stack[-1]) not in map(tuple, via_spans)
                or via.drill <= 0 or via.diameter < 0
                or not all(math.isfinite(n) for n in (*point, via.drill, via.diameter))):
            errors.append("unsupported or disallowed via extent or dimensions")
        elif point in via_points:
            errors.append("duplicate via")
        elif not has_pour and not (degree.keys() | anchors) & set(nodes):
            errors.append("via has no trace endpoint at its centre")
        else:
            if via.drill < minimum_drill - 1e-6:
                errors.append(f"via at {point} has {via.drill:g} mm drill, below {minimum_drill:g} mm")
            minimum = annular_copper(rules, "Via", via.drill, 0, inner=False)["outer"]
            if 0 < via.diameter < minimum - 1e-6:
                errors.append(f"explicit via diameter is smaller than the {minimum:g} mm board-rule diameter")
            via_points.add(point)
            via_nodes.update(nodes)
            components.join(nodes)
            vias.append((owner, nodes[0]))
            if fills:
                diameters = annular_copper(rules, "Via", via.drill, via.diameter, inner=len(stack) > 2)
                for x, y, layer in nodes:
                    side = "outer" if layer in (stack[0], stack[-1]) else "inner"
                    points = _ring_samples(x, y, via.drill, diameters[side])
                    footprints[x, y, layer] = [*footprints.get((x, y, layer), ()), *points]
            if outline and not _inside_outline(outline, point):
                errors.append("via centre is outside the board outline")
    endpoints = anchors | via_nodes
    # Count routed vias before adding any plane connections to the trace graph.
    counts = Counter(tag if tag else components.root(node) for tag, node in vias)
    if any(count > via_limit for count in counts.values()):
        errors.append(f"connection exceeds its via budget of {via_limit}")
    filled_endpoints = set()
    for fill in fills:
        touched = _fill_contacts(fill, endpoints | degree.keys(), wires, footprints)
        if touched:
            components.join(touched)
            filled_endpoints.update(_fill_contacts(fill, touched, (), footprints))
    if any(count == 1 and node not in endpoints | filled_endpoints for node, count in degree.items()):
        errors.append("trace endpoint misses its pad, via or joined trace")
    anchor_roots = {components.root(node) for node in anchors}
    if any(components.root(node) not in anchor_roots for node in degree):
        if not has_pour:
            errors.append("trace component has no contact pad")
    groups = defaultdict(list)
    for contact in sorted(signal.contacts):
        groups[components.root(pads[contact][0])].append(contact)
    return sorted(groups.values()), errors, unknown


def _restricts_layer(restriction, layer, stack):
    if restriction.layers is not None:
        return layer in restriction.layers
    return (restriction.restrict_layer, layer) in ((41, stack[0]), (42, stack[-1]))


def _area_hits(points, half, other, *, closed=True, holes=()):
    """Test a closed polygon or straight stroke against a native restriction."""
    edges = list(pairwise((*points, points[0]) if closed else points))
    if other.kind == "circle":
        x, y, r = other.data
        if any(_inside_poly(hole, x, y) and all(
                _seg_dist(a, b, (x, y), (x, y)) > r + half
                for a, b in pairwise((*hole, hole[0]))) for hole in holes):
            return False
        return ((closed and _inside_poly(points, x, y))
                or any(_seg_dist(a, b, (x, y), (x, y)) <= r + half for a, b in edges))
    polygon, margin = other.data[2:]
    border = list(pairwise((*polygon, polygon[0])))
    if any(_inside_poly(hole, *polygon[0]) and all(
            _seg_dist(a, b, c, d) > half + margin for a, b in pairwise((*hole, hole[0])) for c, d in border
    ) for hole in holes):
        return False
    return (_inside_poly(polygon, *points[0])
            or (closed and _inside_poly(points, *polygon[0]))
            or any(_seg_dist(a, b, c, d) <= half + margin for a, b in edges for c, d in border))


def _copper_hits(copper, restriction):
    if copper.kind == "circle":
        x, y, radius = copper.data
        return restriction.distance(x, y) <= radius
    if copper.kind == "seg":
        x, y, xx, yy, half = copper.data
        return _area_hits(((x, y), (xx, yy)), half, restriction, closed=False)
    x, y, hw, hh, angle = copper.data
    points = [transform(px, py, x, y, angle, False)
              for px, py in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
    return _area_hits(points, 0, restriction)


def _ring_samples(x, y, drill, diameter):
    return [(x + radius * math.cos(i * math.pi / 8), y + radius * math.sin(i * math.pi / 8))
            for radius in ((drill + diameter) / 4, diameter / 2 - 1e-7) if radius > drill / 2
            for i in range(16)]


def _pad_copper(root, board, rules, stack, *, samples=False):
    library_packages = packages(board)
    definitions = {}
    for el in board.findall("./elements/element"):
        package = library_packages[el.attrib["library"], el.attrib["package"]]
        _, mirrored = parse_rot(el.get("rot"))
        for node in package:
            if node.tag in ("pad", "smd"):
                definitions[el.attrib["name"], node.attrib["name"]] = node, mirrored
    for pad in placed_pads(root):
        label = pad.ref, pad.name
        node, mirrored = definitions[pad.ref, pad.name]
        if pad.size <= 0 or not math.isfinite(pad.size):
            raise ValueError(f"{'.'.join(label)}: invalid copper pad size")
        if pad.smd:
            points = pad.poly
            if samples:
                # Rounded copper contains the central disk even when the clearance polygon encloses it.
                edge = (_ring_samples(pad.x, pad.y, 0, pad.size) if float(node.get("roundness", "0"))
                        else points)
                points = [(pad.x, pad.y), *edge]
            yield label, _smd_layer(node, stack, mirrored), points
        else:
            shape = node.get("shape", "round").lower()
            if shape not in ("round", "square", "octagon", "long", "offset"):
                raise UnverifiedGeometry(f"{'.'.join(label)}: unsupported copper pad shape {shape}")
            if any(rules.get(rule, "-1") != "-1" for rule in ("psTop", "psBottom", "psFirst")):
                raise UnverifiedGeometry(f"{'.'.join(label)}: unsupported design-rule pad shape override")
            elongation = "psElongationOffset" if shape == "offset" else "psElongationLong"
            if shape in ("long", "offset") and float(rules.get(elongation, "100")) != 100:
                raise UnverifiedGeometry(f"{'.'.join(label)}: unsupported design-rule pad elongation")
            diameters = annular_copper(
                rules, "Pad", float(node.attrib["drill"]), float(node.get("diameter", "0")), inner=len(stack) > 2,
            )
            for layer in stack:
                side = "top" if layer == stack[0] else "bottom" if layer == stack[-1] else "inner"
                if samples:
                    diameter = diameters[side] * (math.cos(math.pi / 8) if shape == "octagon" else 1)
                    yield label, layer, _ring_samples(pad.x, pad.y, float(node.attrib["drill"]), diameter)
                    continue
                # The existing pad polygons use the top ring. Cover larger side-specific rings.
                scale = diameters[side] / pad.size
                points = [(pad.x + (x - pad.x) * scale, pad.y + (y - pad.y) * scale) for x, y in pad.poly]
                yield label, layer, points


def _check_fill_rings(fill):
    rings = fill["rings"]
    if fill.get("curved") or not rings or any(
        len(ring) < 3 or any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in ring)
        for ring in rings
    ):
        raise UnverifiedGeometry(f"{fill['net']}: unsupported measured fill contours on copper layer {fill['layer']}")
    return rings


def check_fill_keepouts(root, measured_fills, *, restrictions=None):
    """Check measured fill contours against the restrictions on their export layers."""
    if restrictions is None:
        restrictions = [obstacle for obstacle in board_obstacles(root)[0] if obstacle.restrict_layer]
    stack = parse_board_design(root).copper_layers
    measured = set()
    for fill in sorted(measured_fills, key=lambda row: (row["net"], row["layer"])):
        name, layer = fill["net"], fill["layer"]
        if layer not in stack:
            raise UnverifiedGeometry(f"{name}: measured fill layer {layer} is absent from the exported stack")
        applicable = [r for r in restrictions if _restricts_layer(r, layer, stack)]
        if not applicable:
            continue
        outer, *holes = _check_fill_rings(fill)
        if any(_area_hits(outer, 0, restriction, holes=holes) for restriction in applicable):
            raise ValueError(f"{name}: measured copper fill overlaps a keepout on copper layer {layer}")
        measured.add((name, layer))
    return measured


def _keepouts(root, board, stack, measured_fills=None):
    obstacles, rules, _ = board_obstacles(root)
    restrictions = [obs for obs in obstacles if obs.restrict_layer]
    for container in [board.find("plain"), *board.findall("./libraries/library/packages/package")]:
        for node in container:
            if restrictions and node.get("layer") in {str(layer) for layer in stack} and node.tag != "smd":
                raise UnverifiedGeometry("unsupported extra copper geometry in board or package")
            if node.get("layer") not in _RESTRICT:
                continue
            supported = {"polygon", "rectangle", "polygonshape"}
            if container.tag == "plain" and float(node.get("width", "0")) == 0:
                supported.add("circle")
            if node.tag not in supported:
                raise UnverifiedGeometry(f"unsupported restrict geometry on layer {node.attrib['layer']}")
    pad_copper = list(_pad_copper(root, board, rules, stack)) if restrictions else []
    for restriction in restrictions:
        for label, layer, points in pad_copper:
            if _restricts_layer(restriction, layer, stack) and _area_hits(points, 0, restriction):
                raise ValueError(f"{'.'.join(label)} overlaps restrict layer {restriction.restrict_layer}")
        for copper in obstacles:
            if copper.restrict_layer or copper.is_pad:
                continue
            if restriction.restrict_layer == 43:
                if not copper.is_via:
                    continue
            elif copper.layers is not None and not any(
                    _restricts_layer(restriction, layer, stack) for layer in copper.layers):
                continue
            if _copper_hits(copper, restriction):
                raise ValueError(f"{copper.net or 'unassigned pad'} overlaps restrict layer {restriction.restrict_layer}")
    by_layer = {layer: [r for r in restrictions if _restricts_layer(r, layer, stack)] for layer in stack}
    measured = check_fill_keepouts(root, measured_fills, restrictions=restrictions) if measured_fills else set()
    outlines = Counter((s.attrib["name"], int(p.attrib["layer"])) for s in board.findall("./signals/signal")
                       for p in s.findall("polygon") if p.get("pour") != "cutout")
    for signal in board.findall("./signals/signal"):
        for polygon in signal.findall("polygon"):
            if polygon.get("pour") == "cutout":
                continue
            layer = int(polygon.attrib["layer"])
            key = signal.get("name"), layer
            if key in measured and outlines[key] == 1:
                continue
            region = _restrict_polygon(polygon, stack)
            if any(_area_hits(*region.data[2:], r) for r in by_layer.get(layer, ())):
                raise UnverifiedGeometry("polygon outline intersects a keepout; actual fill is unavailable")


def _element_attributes(board):
    incomplete = []
    for element in board.findall("./elements/element"):
        _, mirrored = parse_rot(element.get("rot"))
        for attribute in element.findall("attribute"):
            name = attribute.get("name", "")
            if name.upper() in ("NAME", "VALUE"):
                continue
            try:
                x, y, size = (float(attribute.attrib[field]) for field in ("x", "y", "size"))
            except (KeyError, ValueError):
                x, y, size = 0, 0, 0
            if (not name or "value" not in attribute.attrib or size <= 0
                    or not all(math.isfinite(value) for value in (x, y, size))
                    or attribute.get("layer") != ("28" if mirrored else "27")
                    or attribute.get("display") != "off"):
                incomplete.append(f"{element.attrib['name']}.{name or '<unnamed>'}")
    if incomplete:
        raise ValueError("incomplete native element attributes: " + ", ".join(sorted(incomplete)))


def _drill_spacing(root, rules):
    drilled, _ = design_holes(root)
    holes = sorted(drilled.holes, key=lambda hole: (hole[1], hole[2], hole[0]))
    minimum = rule_mm(rules.get("mdDrill")) or 0.0
    largest_radius = max((drill / 2 for drill, _, _ in holes), default=0.0)
    violations = []
    for index, (drill, x, y) in enumerate(holes):
        for next_index in range(index + 1, len(holes)):
            other, xx, yy = holes[next_index]
            if xx - x >= minimum + drill / 2 + largest_radius:
                break
            gap = math.hypot(xx - x, yy - y) - (drill + other) / 2
            if gap < minimum - 1e-6:
                violations.append(f"({x:g}, {y:g}) to ({xx:g}, {yy:g}): {gap:.4f} mm")
    if violations:
        raise ValueError(f"{len(violations)} drill spacings below {minimum:g} mm: " + ", ".join(violations[:10]))


def _via_annulus_overlaps(design, stack):
    vias = sorted((via.x, via.y, name, via.drill, via.diameter)
                  for name, signal in design.signals.items() for via in signal.vias
                  if via.extent == f"{stack[0]}-{stack[-1]}" and via.drill > 0
                  and all(math.isfinite(n) for n in (via.x, via.y, via.drill, via.diameter)))
    radii = [annular_copper(design.rules, "Via", drill, diameter, inner=len(stack) > 2)
             for _, _, _, drill, diameter in vias]
    largest = max((max(row.values()) / 2 for row in radii), default=0)
    same_net, foreign = [], []
    for i, (x, y, net, _, _) in enumerate(vias):
        for j in range(i + 1, len(vias)):
            xx, yy, other, _, _ = vias[j]
            if xx - x >= 2 * largest:
                break
            threshold = max((diameter + radii[j][side]) / 2 for side, diameter in radii[i].items())
            if math.hypot(xx - x, yy - y) < threshold - 1e-6:
                (same_net if net == other else foreign).append(f"{net} ({x:g}, {y:g}) / {other} ({xx:g}, {yy:g})")
    return same_net, foreign


def validate_candidate(schematic_xml: bytes, board_xml: bytes, routing_intent: dict, *,
                       provenance=None, measured_fills=None, measured_fills_complete=False) -> dict:
    """Check paired bytes and return deterministic findings and physical contact groups."""
    _validate_source_xml(schematic_xml, "schematic")
    _validate_source_xml(board_xml, "board")
    sch_root, board_root = ET.fromstring(schematic_xml), ET.fromstring(board_xml)
    board = board_root.find("./drawing/board")
    if (sch_root.tag != "eagle" or board_root.tag != "eagle"
            or len(sch_root.findall("./drawing/schematic")) != 1
            or len(board_root.findall("./drawing/board")) != 1):
        raise ValueError("expected one flat EAGLE schematic and board")
    owners = route_owners(board_root, provenance) if provenance else {}
    findings = [_check("pair_signature", lambda: signature(sch_root, board_root)),
                _check("element_attributes", lambda: _element_attributes(board))]
    sch, design = parse_schematic_design(sch_root), parse_board_design(board_root)
    stack = tuple(design.copper_layers)
    findings.append(_check("drill_spacing", lambda: _drill_spacing(board_root, design.rules)))
    try:
        shared_annuli, foreign_annuli = _via_annulus_overlaps(design, stack)
        findings.append(_finding("via_annuli", "fail" if foreign_annuli else "pass",
                                 f"{len(foreign_annuli)} different-net and {len(shared_annuli)} same-net overlaps"
                                 + (f". Different-net: {', '.join(foreign_annuli[:10])}" if foreign_annuli else "")))
    except (ValueError, OverflowError) as error:
        findings.append(_finding("via_annuli", "unverified", str(error)))
    pads = {}

    def verify_contacts():
        pads.update(_pad_centres(board, sch, stack))
        _contacts(sch_root, board, sch, pads)

    findings.append(_check("contacts", verify_contacts))
    outline = ()

    def verify_outline():
        nonlocal outline
        outline = _outline(board)
        if any(not _inside_outline(outline, _point(hole)) for hole in board.findall("./plain/hole")):
            raise ValueError("drill centre is outside the board outline")

    findings.append(_check("outline", verify_outline))
    findings.append(_check("placements", lambda: _placements(board, outline, routing_intent["allow_outside"])))
    if routing_intent.get("verify_reference_labels"):
        findings.append(_check("reference_labels", lambda: verify_names(board_root, routing_intent["allow_outside"])))
    if exceptions := routing_intent.get("placement_grid_exceptions"):
        findings.append(_finding("placement_grid_exceptions", "pass",
                                 ", ".join(f"{ref}: {reason}" for ref, reason in sorted(exceptions.items())), False))
    routing = routing_intent["routing"]
    groups, routing_errors, routing_unknown = {}, [], []
    poured = set()
    plane_results, fills_by_net, unsupported_fills = {}, defaultdict(list), set()
    for fill in measured_fills or ():
        try:
            _check_fill_rings(fill)
        except UnverifiedGeometry:
            unsupported_fills.add(fill["net"])
            continue
        fills_by_net[fill["net"]].append(fill)
    fill_pad_copper = {}
    if measured_fills_complete and fills_by_net and pads:
        try:
            fill_pad_copper = {(ref, name, layer): points for (ref, name), layer, points in
                               _pad_copper(board_root, board, design.rules, stack, samples=True)}
        except (KeyError, ValueError):
            unsupported_fills.update(fills_by_net)
    xml_signals = {signal.get("name"): signal for signal in board.findall("./signals/signal")}
    for name, signal in sorted(design.signals.items()):
        if not set(signal.contacts) <= pads.keys():
            routing_unknown.append(f"{name}: board pad centres could not be resolved")
            continue
        policy = routing["policies"].get(name, {})
        allowed = set(policy.get("allowed_layers", stack))
        if not allowed <= set(stack):
            routing_errors.append(f"{name}: policy names absent copper layers")
        if any(tuple(span) != (stack[0], stack[-1]) for span in policy.get("via_spans", ())):
            routing_errors.append(f"{name}: policy names an unsupported via span")
        limit = routing["budget"]["max_vias_per_connection"]
        xml_signal = xml_signals[name]
        pours = [p for p in xml_signal.findall("polygon") if p.get("pour") != "cutout"]
        if pours:
            poured.add(name)
        if any(int(p.attrib["layer"]) not in allowed for p in xml_signal.findall("polygon")):
            routing_errors.append(f"{name}: polygon uses absent or disallowed copper layer")
        fill_layers = {int(p.attrib["layer"]) for p in pours}
        fills = [row for row in fills_by_net[name] if row["layer"] in fill_layers & allowed & set(stack)]
        width, drill = route_minimums(board, xml_signal.get("class", "0"), design.rules)
        groups[name], errors, unknown = _route_components(
            signal, pads, stack, allowed & set(stack),
            min(limit, policy.get("max_vias_per_connection", limit)),
            [owners.get((name, canonical_digest(route_geometry(via)))) for via in xml_signal.findall("via")],
            outline, design.rules, policy.get("via_spans"), (max(width, policy.get("min_width_mm", 0)), drill),
            fills=fills if measured_fills_complete and name not in unsupported_fills else (),
            has_pour=bool(pours), fill_pad_copper=fill_pad_copper,
        )
        if pours:
            missing = sorted(fill_layers - {row["layer"] for row in fills})
            count = max(0, len(groups[name]) - 1)
            if missing or name in unsupported_fills or not measured_fills_complete:
                status = "unverified"
            else:
                status = "fail" if count else "pass"
            plane_results[name] = {"status": status, "unjoined_contact_pairs": count, "unmeasured_layers": missing}
        routing_errors.extend(f"{name}: {error}" for error in sorted(set(errors)))
        routing_unknown.extend(f"{name}: {reason}" for reason in sorted(set(unknown)))
    findings.append(_finding(
        "routing_geometry", "fail" if routing_errors else "unverified" if routing_unknown else "pass",
        "; ".join(routing_errors + routing_unknown),
    ))
    findings.append(_check("keepouts", lambda: _keepouts(board_root, board, stack, measured_fills)))
    airwires_by_net = {name: max(0, len(contacts) - 1) for name, contacts in groups.items()}
    airwires = sum(airwires_by_net.values())
    pending_pours = sorted(name for name, row in plane_results.items() if row["status"] == "unverified")
    pending_connections = [name for name in pending_pours if airwires_by_net[name]]
    open_pours = sorted(name for name, row in plane_results.items() if row["status"] == "fail")
    unrouted = sum(count for name, count in airwires_by_net.items() if name not in poured or name in open_pours)
    findings.append(_finding(
        "plane_connectivity", "fail" if open_pours else "unverified" if pending_pours else "pass",
        ", ".join(f"{name}: {row['status']} ({row['unjoined_contact_pairs']} unjoined contact pairs)"
                  for name, row in sorted(plane_results.items())),
        measured_fills_complete,
    ))
    findings.append(_finding(
        "full_routing", "fail" if unrouted else "unverified" if routing_unknown or pending_connections else "pass",
        f"{airwires} unjoined contact pairs from serialized traces, vias and available measured fills"
        + (f". Native fill connectivity required for {', '.join(pending_connections)}" if pending_connections else ""),
        routing["require_fully_routed"],
    ))
    for check in ("native_erc", "native_drc", "native_polygon_refill", "native_3d", "manufacturing_review"):
        findings.append(_finding(check, "unverified", "Requires a separate acceptance step", False))
    return {
        "status": finding_status(findings), "findings": findings, "airwires": airwires,
        "airwires_by_net": airwires_by_net, "contact_components": groups, "planes": plane_results,
    }
