"""Plan part origins and readable PCB designators from exported geometry."""

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from itertools import product
import math
import re
import xml.etree.ElementTree as ET

from . import footprints as FP, placement_check as PC, solder_mask
from .design import parse_board_design
from .eagle import UnverifiedGeometry
from .gerbers import design_holes
from .net_classes import _mm
from .pairs import _seg_dist
from .stitch import _inside_poly, board_obstacles, outline_vertices


def reference(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_$][A-Za-z0-9_.$+!~-]*", value):
        raise ValueError(f"Invalid reference designator {value!r}")
    return value


def next_reference(prefix, occupied):
    prefix = reference(prefix)
    existing = {ref.casefold() for ref in occupied}
    for number in range(1, len(existing) + 2):
        ref = f"{prefix}{number}"
        if ref.casefold() not in existing:
            return ref


def placement_grid(root):
    grid = root.find("./drawing/grid")
    if grid is None:
        return None
    unit = grid.get("unitdist", grid.get("unit", "mm"))
    pitch = _mm(grid.attrib["distance"] + unit)
    if not pitch or pitch <= 0:
        raise ValueError("Board placement grid spacing must be positive")
    origin = tuple(_mm(grid.get(f"origin{axis}", "0") + unit) for axis in "xy")
    return pitch, origin


def _point(node):
    return tuple(float(node.get(axis, "0")) for axis in "xy")


def _courtyards(root):
    actual = FP.courtyards(root)
    return list(actual.values()) + list(PC.derived_courtyards(root, actual).values())


def verify_names(root, exemptions=()):
    for element in root.findall("./drawing/board/elements/element"):
        ref = reference(element.get("name"))
        name = element.find("attribute[@name='NAME']")
        context = _label_context(root, ref, ref in exemptions)
        reason = "missing reference label" if name is None else label_conflict(ref, name.attrib, context)
        if reason:
            raise ValueError(f"{ref}: {reason}")


def _edge_clear(poly, outline, gap):
    return not outline or (all(_inside_poly(outline, *p) for p in poly) and all(
        _seg_dist(a, b, c, d) >= max(gap, 1e-6)
        for a, b in zip(poly, poly[1:] + poly[:1])
        for c, d in zip(outline, outline[1:] + outline[:1])))


def _outline(edges):
    try:
        return outline_vertices(edges) if edges else []
    except ValueError as error:
        raise UnverifiedGeometry(str(error)) from error


def default_origin(root):
    _, _, edges = board_obstacles(root)
    if not edges:
        raise ValueError("Supply PCB placement coordinates for a board without an outline")
    left, bottom, right, top = PC._bbox(_outline(edges))
    return (left + right) / 2, (bottom + top) / 2


def _copper_shapes(obstacles, layer):
    for o in obstacles:
        if o.layers is not None and layer not in o.layers:
            continue
        if o.restrict_layer == 43 or o.is_pad and not o.is_npth and not o.restrict_layer:
            continue
        if o.kind == "circle":
            yield PC._circle(*o.data), o.net or "hole or restriction", bool(o.restrict_layer)
        elif o.kind == "seg":
            yield PC._stadium(*o.data), o.net or "copper", False
        elif o.kind == "polygon":
            yield FP.convex_hull(o.data[2]), "copper restriction", True


def _points(preferred, grid, exact):
    if exact or grid is None:
        yield preferred
    if exact:
        return
    pitch, origin = grid or (0.5, preferred)
    base = [int((Decimal(str((coord - offset) / pitch))).to_integral_value(rounding=ROUND_HALF_UP))
            for coord, offset in zip(preferred, origin)]
    radius = min(24, max(2, math.ceil(8 / pitch)))
    points = [(round(origin[0] + (base[0] + i) * pitch, 4), round(origin[1] + (base[1] + j) * pitch, 4))
              for i, j in product(range(-radius, radius + 1), repeat=2)]
    yield from sorted(points, key=lambda point: (math.dist(point, preferred), point))


def plan(root, ref, preferred, *, exact=False, allow_outside=False, check_copper=True):
    """Search grid points first, using the requested point for a local copper-clearance exception."""
    board = root.find("./drawing/board")
    element = next(el for el in board.findall("./elements/element") if el.get("name") == ref)
    preferred = tuple(round(float(v), 4) for v in preferred)
    if not all(math.isfinite(v) for v in preferred):
        raise ValueError(f"{ref}: placement coordinates must be finite")
    origin = _point(element)
    cys = _courtyards(root)
    own = [c for c in cys if c.ref == ref]
    if not own:
        raise ValueError(f"{ref}: footprint has no usable courtyard, body or pads")
    obstacles, rules, edges = board_obstacles(root)
    outline = _outline(edges) if not allow_outside else []
    edge_gap = _mm(rules.get("mdCopperDimension")) or 0.0
    pads = PC.pads(root)
    stack = parse_board_design(root).copper_layers
    copper = {side: list(_copper_shapes(obstacles, layer)) for side, layer in zip(("top", "bottom"), (stack[0], stack[-1]))}
    minimum = _mm(rules.get("mdWirePad")) or 0.0

    def conflict(at):
        delta = tuple(b - a for a, b in zip(origin, at))
        for cy in own:
            for poly in cy.polygons:
                moved = [(x + delta[0], y + delta[1]) for x, y in poly]
                if not _edge_clear(moved, outline, 0):
                    return "board edge"
                for other in cys:
                    if other.ref != ref and other.side == cy.side and any(
                            FP.separation(moved, p) < -1e-5 for p in other.polygons):
                        return f"footprint {other.ref}"
        for pad in pads:
            if pad.ref != ref:
                continue
            moved = [(x + delta[0], y + delta[1]) for x, y in pad.poly]
            if not _edge_clear(moved, outline, edge_gap):
                return f"board edge clearance at {ref}.{pad.name}"
            for other in pads:
                if other.ref == ref or not pad.sides & other.sides:
                    continue
                key = "mdSmdSmd" if pad.smd and other.smd else "mdSmdPad" if pad.smd or other.smd else "mdPadPad"
                gap = _mm(rules.get(key)) or 0.0
                if FP.separation(moved, other.poly) < gap - 1e-6:
                    return f"pad {other.ref}.{other.name}"
            for side in pad.sides if check_copper else ():
                for poly, net, restriction in copper[side]:
                    if (restriction or not pad.net or pad.net != net) and FP.separation(moved, poly) < minimum + 1e-6:
                        return f"routing clearance at {ref}.{pad.name} beside {net}"
        return None

    grid = placement_grid(root)
    first_conflict, routing_conflict = None, None
    for at in _points(preferred, grid, exact):
        reason = conflict(at)
        if reason is None:
            return {"ref": ref, "at": list(at), "rotation": element.get("rot", "R0"), "off_grid_reason": None,
                    "allow_outside": allow_outside}
        first_conflict = first_conflict or reason
        if reason.startswith("routing clearance"):
            routing_conflict = routing_conflict or reason
    if grid and not exact and routing_conflict and conflict(preferred) is None:
        return {"ref": ref, "at": list(preferred), "rotation": element.get("rot", "R0"),
                "off_grid_reason": routing_conflict, "allow_outside": allow_outside}
    raise ValueError(f"{ref}: no legal nearby placement at {preferred}. Conflict with {first_conflict}")


def _label_polygon(ref, attrs):
    size = float(attrs["size"])
    stroke = size * float(attrs.get("ratio", "10")) / 200
    angle, mirror = FP.parse_rot(attrs.get("rot"))
    angle, align = FP.readable(angle, attrs.get("align", "bottom-left"), attrs.get("rot"))
    x, y = _point(attrs)
    poly = PC._text_poly(x, y, ref, size, angle, align)
    if mirror:
        poly = [(2 * x - px, py) for px, py in poly]
    return FP.convex_hull([(x + dx, y + dy) for x, y in poly for dx, dy in product((-stroke, stroke), repeat=2)])


def _label_context(root, ref, allow_outside=False):
    board = root.find("./drawing/board")
    el = next(e for e in board.findall("./elements/element") if e.get("name") == ref)
    _, mirror = FP.parse_rot(el.get("rot"))
    layer, silk, side = ("26", "22", "bottom") if mirror else ("25", "21", "top")
    cys = _courtyards(root)
    own = [p for c in cys if c.ref == ref and c.side == side for p in c.polygons]
    if not own:
        raise ValueError(f"{ref}: cannot locate its footprint for a PCB designator")
    bounds = PC._bbox([p for poly in own for p in poly])
    obs, rules, edges = board_obstacles(root)
    outline = [] if allow_outside else _outline(edges)
    clearance = max(_mm(rules.get("mdSmdStop")) or 0, 0.1)
    obstacles = [(p, c.ref, clearance) for c in cys if c.side == side for p in c.polygons]
    obstacles.extend((p.poly, f"{p.ref}.{p.name}", clearance + (solder_mask.frame(rules, p.size) if p.stop else 0))
                     for p in PC.pads(root) if side in p.sides)
    obstacles.extend((s["poly"], s["owner"] or "board text or silk", clearance) for s in PC.silk_shapes(root)
                     if s["layer"] == silk and not (s["owner"] == ref and s["what"].startswith("name ")))
    for text in board.findall("./plain/text"):
        if text.get("layer") == layer and (text.text or "").strip():
            obstacles.append((_label_polygon(text.text, text.attrib), text.text, clearance))
    drilled, _ = design_holes(root)
    obstacles.extend((PC._circle(x, y, drill / 2), "drill", clearance) for drill, x, y in drilled.holes)
    stack = parse_board_design(root).copper_layers
    outer = stack[-1] if mirror else stack[0]
    limit = _mm(rules.get("mlViaStopLimit")) or 0.0
    exposed = {(float(v.get("x")), float(v.get("y"))) for v in board.findall("./signals/signal/via")
               if v.get("stop") == "yes" or (v.get("stop") != "no" and float(v.get("drill")) > limit)}
    obstacles.extend((PC._circle(*o.data), o.net or "exposed via", clearance + solder_mask.frame(rules, 2 * o.data[2]))
                     for o in obs if o.is_via and outer in o.layers and o.data[:2] in exposed)
    names = [a for e in board.findall("./elements/element") if e is not el
             for a in e.findall("attribute[@name='NAME']") if a.get("layer") == layer and a.get("display") != "off"
             and a.get("x") is not None and float(a.get("size", "0")) > 0]
    style = min(names, key=lambda a: math.dist(_point(a), _point(el))).attrib if names else {}
    if not style:
        packages = FP.packages(board)
        other_labels = []
        for other in board.findall("./elements/element"):
            if other is el or FP.parse_rot(other.get("rot"))[1] != mirror:
                continue
            package = packages.get((other.get("library"), other.get("package")))
            if package is None:
                continue
            for node in package.findall("text"):
                if (node.text or "").strip().upper() == ">NAME" and FP.board_layer(node.get("layer"), mirror) == layer:
                    other_labels.append((math.dist(_point(other), _point(el)), node.attrib))
        if other_labels:
            style = min(other_labels, key=lambda row: row[0])[1]
    return el, layer, bounds, style, obstacles, outline, clearance


def label_conflict(ref, attrs, context):
    _, layer, bounds, _, obstacles, outline, clearance = context
    if (attrs.get("layer") != layer or attrs.get("display") == "off" or attrs.get("x") is None or attrs.get("y") is None
            or not all(math.isfinite(v) for v in (*_point(attrs), float(attrs.get("size", "0"))))
            or float(attrs.get("size", "0")) <= 0):
        return "missing or hidden text on the names layer"
    poly = _label_polygon(ref, attrs)
    left, bottom, right, top = bounds
    x, y = _point(attrs)
    if math.hypot(max(left - x, x - right, 0), max(bottom - y, y - top, 0)) > 4:
        return "text is too far from its footprint"
    if not _edge_clear(poly, outline, clearance):
        return "board edge"
    box = PC._bbox(poly)
    for other, name, gap in obstacles:
        x0, y0, x1, y1 = PC._bbox(other)
        if PC._in_region(box, (x0 - gap, y0 - gap, x1 + gap, y1 + gap)) and FP.separation(poly, other) < gap - 1e-6:
            return name
    return None


def plan_label(root, ref, *, allow_outside=False):
    context = _label_context(root, ref, allow_outside)
    el, layer, (left, bottom, right, top), style, _, _, gap = context
    current = el.find("attribute[@name='NAME']")
    if current is not None and label_conflict(ref, current.attrib, context) is None:
        return {**current.attrib, "font": current.get("font", "proportional"), "ratio": current.get("ratio", "10"),
                "align": current.get("align", "bottom-left"), "rot": current.get("rot", "R0")}
    style = style or (current.attrib if current is not None else {})
    size = float(style.get("size", "0.8")) or 0.8
    angle = FP.readable(FP.parse_rot(style.get("rot"))[0], "center", None)[0]
    attrs = {"name": "NAME", "layer": layer, "display": "value", "size": str(size),
             "font": style.get("font", "proportional"), "ratio": style.get("ratio", "10"), "align": "center",
             "rot": f"{'M' if layer == '26' else ''}R{angle:g}"}
    x, y = (left + right) / 2, (bottom + top) / 2
    blocked = None
    for step in (0.3, 0.6, 1.0, 1.5, 2.0, 2.5):
        distance = gap + step + size / 2
        for px, py in ((x, top + distance), (x, bottom - distance),
                       (right + distance + len(ref) * size / 2, y), (left - distance - len(ref) * size / 2, y)):
            attrs.update(x=str(round(px, 4)), y=str(round(py, 4)))
            reason = label_conflict(ref, attrs, context)
            if reason is None:
                return attrs
            blocked = blocked or reason
    raise ValueError(f"{ref}: no clean nearby PCB reference label. Conflict with {blocked}")


def apply(root, placement):
    ref = placement["ref"]
    el = root.find(f"./drawing/board/elements/element[@name='{ref}']")
    x, y = placement["at"]
    old_x, old_y = _point(el)
    el.attrib.update(x=str(x), y=str(y))
    for attr in el.findall("attribute"):
        if attr.get("x") is not None and attr.get("y") is not None:
            ax, ay = _point(attr)
            attr.attrib.update(x=str(round(ax + x - old_x, 4)), y=str(round(ay + y - old_y, 4)))
    if label := placement.get("label"):
        old = el.find("attribute[@name='NAME']")
        if old is not None:
            el.remove(old)
        el.set("smashed", "yes")
        ET.SubElement(el, "attribute", label)


def verify(root, placement):
    ref = placement["ref"]
    el = root.find(f"./drawing/board/elements/element[@name='{ref}']")
    if el is None or math.dist(_point(el), placement["at"]) > 0.001 or (
            FP.parse_rot(el.get("rot")) != FP.parse_rot(placement["rotation"])):
        raise ValueError(f"{ref}: PCB origin or orientation differs from the placement plan")
    plan(root, ref, _point(el), exact=True, allow_outside=placement["allow_outside"],
         check_copper=placement.get("check_copper", True))
    if label := placement.get("label"):
        actual = el.find("attribute[@name='NAME']")
        if actual is None:
            actual = next((t for t in root.findall("./drawing/board/plain/text") if t.text == ref
                           and t.get("layer") == label["layer"] and math.dist(_point(t), _point(label)) <= .001), None)
        if (actual is None or math.dist(_point(actual), _point(label)) > .001
                or abs(float(actual.get("size", "0")) - float(label["size"])) > .001
                or FP.parse_rot(actual.get("rot")) != FP.parse_rot(label.get("rot"))
                or actual.get("align", "bottom-left") != label.get("align", "bottom-left")
                or actual.get("font", "proportional") != label.get("font", "proportional")
                or float(actual.get("ratio", "10")) != float(label.get("ratio", "10"))):
            raise ValueError(f"{ref}: PCB reference label differs from the placement plan")
        label_root = deepcopy(root)
        for text in label_root.findall("./drawing/board/plain/text"):
            if text.text == ref and text.get("layer") == label["layer"] and math.dist(_point(text), _point(label)) <= .001:
                label_root.find("./drawing/board/plain").remove(text)
        if reason := label_conflict(ref, actual.attrib, _label_context(label_root, ref, placement["allow_outside"])):
            raise ValueError(f"{ref}: PCB reference label conflicts with {reason}")


def place_existing(root, ref, preferred=None, *, exact=False):
    element = root.find(f"./drawing/board/elements/element[@name='{ref}']")
    placement = plan(root, ref, preferred if preferred is not None else _point(element), exact=exact)
    apply(root, placement)
    placement["label"] = plan_label(root, ref)
    return placement


def read_placement(root, ref):
    el = root.find(f"./drawing/board/elements/element[@name='{ref}']")
    if el is None:
        raise ValueError(f"{ref}: reference exists in the schematic but is missing from the PCB")
    name = el.find("attribute[@name='NAME']")
    candidates = ([name] if name is not None else []) + [
        text for text in root.findall("./drawing/board/plain/text") if text.text == ref]
    for candidate in candidates:
        placement = {"ref": ref, "at": list(_point(el)), "rotation": el.get("rot", "R0"), "off_grid_reason": None,
                     "allow_outside": False, "label": dict(candidate.attrib)}
        try:
            verify(root, placement)
        except ValueError:
            continue
        return placement
    return None
