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
from ..pairs import _seg_dist
from ..placement_check import pads as placed_pads
from ..stitch import _inside_poly, _restrict_polygon, board_obstacles, segments_intersect
from .board import route_geometry, route_owners
from .canonical import canonical_digest
from .manifest import finding_status
from .model import _validate_source_xml

# Router taps round to four decimals when landing in a trace interior.
_TAP_TOLERANCE = math.sqrt(2) * 0.00005 + 1e-10
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
    neighbors = defaultdict(set)
    for edge in contour:
        a, b = _point(edge, "x1", "y1"), _point(edge, "x2", "y2")
        if a == b or b in neighbors[a]:
            raise ValueError("outline contains a zero-length or repeated segment")
        neighbors[a].add(b)
        neighbors[b].add(a)
    if any(len(row) != 2 for row in neighbors.values()):
        raise ValueError("outline is open or branched")
    start = min(neighbors)
    vertices = [start]
    previous, cursor = start, min(neighbors[start])
    while cursor != start:
        vertices.append(cursor)
        previous, cursor = cursor, next(iter(neighbors[cursor] - {previous}))
    if len(vertices) != len(neighbors):
        raise UnverifiedGeometry("outline has more than one closed contour")
    polygon = ET.Element("polygon", layer="41")
    for x, y in vertices:
        ET.SubElement(polygon, "vertex", x=str(x), y=str(y))
    try:
        _restrict_polygon(polygon, (1, 16), "outline")
    except UnverifiedGeometry as exc:
        raise ValueError(str(exc)) from exc
    return tuple(vertices)


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


def _wire_junctions(wires, components):
    """Join same-layer intersections and four-decimal interior taps."""
    tapped = set()
    active = []
    wires = sorted(wires, key=lambda ends: min(ends[0][0], ends[1][0]))
    for a, b in wires:
        left = min(a[0], b[0]) - _TAP_TOLERANCE
        active = [(c, d) for c, d in active if max(c[0], d[0]) >= left]
        for c, d in active:
            if {a, b} & {c, d}:
                continue
            on = {p for p, q, r in ((a, c, d), (b, c, d), (c, a, b), (d, a, b))
                  if _seg_dist(p[:2], p[:2], q[:2], r[:2]) <= _TAP_TOLERANCE}
            if on or segments_intersect(a[:2], b[:2], c[:2], d[:2]):
                components.join((a, c))
                tapped.update(on)
        active.append((a, b))
    return tapped


def _route_components(signal, pads, stack, allowed, via_limit, via_owners, outline):
    """Return contact groups proved by pad centres, straight traces and through vias."""
    components = _Components()
    errors, unknown = [], []
    anchors = set()
    for contact in signal.contacts:
        centers = pads[contact]
        components.join(centers)
        anchors.update(centers)

    layer_wires = defaultdict(list)
    degree = Counter()
    seen_wires = set()
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
            seen_wires.add(frozenset((a, b)))
            components.join((a, b))
            degree.update((a, b))
            layer_wires[wire.layer].append((a, b))
            if outline and not _trace_inside(outline, a[:2], b[:2]):
                errors.append("trace leaves the board outline")

    tapped = set().union(*(_wire_junctions(wires, components) for wires in layer_wires.values()))
    via_points, via_nodes = set(), set()
    vias = []
    for via, owner in zip(signal.vias, via_owners):
        point = via.x, via.y
        nodes = [(*point, layer) for layer in stack]
        if (via.extent != f"{stack[0]}-{stack[-1]}" or not set(stack) <= allowed
                or via.drill <= 0 or via.diameter < 0
                or not all(math.isfinite(n) for n in (*point, via.drill, via.diameter))):
            errors.append("unsupported or disallowed via extent or dimensions")
        elif point in via_points:
            errors.append("duplicate via")
        elif not degree.keys() & set(nodes):
            errors.append("via has no trace endpoint at its centre")
        else:
            via_points.add(point)
            via_nodes.update(nodes)
            components.join(nodes)
            vias.append((owner, nodes[0]))
            if outline and not _inside_outline(outline, point):
                errors.append("via centre is outside the board outline")
    endpoints = anchors | via_nodes | tapped
    if any(count == 1 and node not in endpoints for node, count in degree.items()):
        errors.append("trace endpoint misses its pad, via or joined trace")
    anchor_roots = {components.root(node) for node in anchors}
    if any(components.root(node) not in anchor_roots for node in degree):
        errors.append("trace component has no contact pad")
    # Explicit copper has no per-connection ownership. Bound its connected groups.
    counts = Counter(tag if tag else components.root(node) for tag, node in vias)
    if any(count > via_limit for count in counts.values()):
        errors.append(f"connection exceeds its via budget of {via_limit}")

    groups = defaultdict(list)
    for contact in sorted(signal.contacts):
        groups[components.root(pads[contact][0])].append(contact)
    return sorted(groups.values()), errors, unknown


def _restricts_layer(restriction, layer, stack):
    if restriction.layers is not None:
        return layer in restriction.layers
    return (restriction.restrict_layer, layer) in ((41, stack[0]), (42, stack[-1]))


def _area_hits(points, half, other, *, closed=True):
    """Test a closed polygon or straight stroke against a native restriction."""
    edges = list(pairwise((*points, points[0]) if closed else points))
    if other.kind == "circle":
        x, y, r = other.data
        return ((closed and _inside_poly(points, x, y))
                or any(_seg_dist(a, b, (x, y), (x, y)) <= r + half for a, b in edges))
    polygon, margin = other.data[2:]
    return (_inside_poly(polygon, *points[0])
            or (closed and _inside_poly(points, *polygon[0]))
            or any(_seg_dist(a, b, c, d) <= half + margin for a, b in edges
                   for c, d in zip(polygon, polygon[1:] + polygon[:1])))


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


def _pad_copper(root, board, rules, stack):
    library_packages = packages(board)
    definitions = {}
    for el in board.findall("./elements/element"):
        package = library_packages[el.attrib["library"], el.attrib["package"]]
        _, mirrored = parse_rot(el.get("rot"))
        for node in package:
            if node.tag in ("pad", "smd"):
                definitions[el.attrib["name"], node.attrib["name"]] = node, mirrored
    for pad in placed_pads(root):
        label = f"{pad.ref}.{pad.name}"
        node, mirrored = definitions[pad.ref, pad.name]
        if pad.size <= 0 or not math.isfinite(pad.size):
            raise ValueError(f"{label}: invalid copper pad size")
        if pad.smd:
            yield label, _smd_layer(node, stack, mirrored), pad.poly
        else:
            shape = node.get("shape", "round").lower()
            if shape not in ("round", "square", "octagon", "long", "offset"):
                raise UnverifiedGeometry(f"{label}: unsupported copper pad shape {shape}")
            if any(rules.get(rule, "-1") != "-1" for rule in ("psTop", "psBottom", "psFirst")):
                raise UnverifiedGeometry(f"{label}: unsupported design-rule pad shape override")
            if shape in ("long", "offset") and float(rules.get("psElongationLong", "100")) != 100:
                raise UnverifiedGeometry(f"{label}: unsupported design-rule pad elongation")
            diameters = annular_copper(
                rules, "Pad", float(node.attrib["drill"]), float(node.get("diameter", "0")), inner=False,
            )
            for layer, side in ((stack[0], "top"), (stack[-1], "bottom")):
                # The existing pad polygons use the top ring. Cover larger side-specific rings.
                scale = max(1, diameters[side] / pad.size)
                points = [(pad.x + (x - pad.x) * scale, pad.y + (y - pad.y) * scale) for x, y in pad.poly]
                yield label, layer, points


def _keepouts(root, board, stack):
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
                raise ValueError(f"{label} overlaps restrict layer {restriction.restrict_layer}")
        for copper in obstacles:
            if copper.restrict_layer or copper.is_pad:
                continue
            if copper.layers is not None and not any(
                _restricts_layer(restriction, layer, stack) for layer in copper.layers
            ):
                continue
            if _copper_hits(copper, restriction):
                raise ValueError(f"{copper.net or 'unassigned pad'} overlaps restrict layer {restriction.restrict_layer}")
    for polygon in board.findall("./signals/signal/polygon"):
        layer = int(polygon.attrib["layer"])
        region = _restrict_polygon(polygon, stack)
        for restriction in restrictions:
            if (_restricts_layer(restriction, layer, stack)
                    and _area_hits(*region.data[2:], restriction)):
                raise UnverifiedGeometry("polygon outline intersects a keepout; actual fill is unavailable")


def validate_candidate(schematic_xml: bytes, board_xml: bytes, routing_intent: dict, *, provenance=None) -> dict:
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
    findings = [_check("pair_signature", lambda: signature(sch_root, board_root))]
    sch, design = parse_schematic_design(sch_root), parse_board_design(board_root)
    stack = tuple(design.copper_layers)
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
    routing = routing_intent["routing"]
    groups, routing_errors, routing_unknown = {}, [], []
    for name, signal in sorted(design.signals.items()):
        if not set(signal.contacts) <= pads.keys():
            routing_unknown.append(f"{name}: board pad centres could not be resolved")
            continue
        policy = routing["policies"].get(name, {})
        allowed = set(policy.get("allowed_layers", stack))
        if not allowed <= set(stack):
            routing_errors.append(f"{name}: policy names absent copper layers")
        limit = routing["budget"]["max_vias_per_connection"]
        xml_signal = next(s for s in board.findall("./signals/signal") if s.get("name") == name)
        if any(int(p.attrib["layer"]) not in allowed for p in xml_signal.findall("polygon")):
            routing_errors.append(f"{name}: polygon uses absent or disallowed copper layer")
        groups[name], errors, unknown = _route_components(
            signal, pads, stack, allowed & set(stack),
            min(limit, policy.get("max_vias_per_connection", limit)),
            [owners.get((name, canonical_digest(route_geometry(via)))) for via in xml_signal.findall("via")],
            outline,
        )
        routing_errors.extend(f"{name}: {error}" for error in sorted(set(errors)))
        routing_unknown.extend(f"{name}: {reason}" for reason in sorted(set(unknown)))
    findings.append(_finding(
        "routing_geometry", "fail" if routing_errors else "unverified" if routing_unknown else "pass",
        "; ".join(routing_errors + routing_unknown),
    ))
    findings.append(_check("keepouts", lambda: _keepouts(board_root, board, stack)))
    airwires_by_net = {name: max(0, len(contacts) - 1) for name, contacts in groups.items()}
    airwires = sum(airwires_by_net.values())
    findings.append(_finding(
        "full_routing", "fail" if airwires else "unverified" if routing_unknown else "pass",
        f"{airwires} unjoined contact pairs from proven copper",
        routing["require_fully_routed"],
    ))
    for check in ("native_erc", "native_drc", "native_polygon_refill", "native_3d", "manufacturing_review"):
        findings.append(_finding(check, "unverified", "Requires a separate acceptance step", False))
    return {
        "status": finding_status(findings), "findings": findings, "airwires": airwires,
        "airwires_by_net": airwires_by_net, "contact_components": groups,
    }
