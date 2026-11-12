"""Render saved PCB geometry and persisted routing diagnostics as SVG."""
import math
import xml.etree.ElementTree as ET

from ..annular import copper
from ..design import parse_board_design
from ..eagle import Pad, UnverifiedGeometry
from ..footprints import packages, transform
from ..placement_check import pads as placed_pads
from ..stitch import _restrict_polygon, _restrict_shape

_COLORS = ("#a73037", "#154aa6", "#966008", "#16704e", "#6547a8", "#137984")


def _number(value):
    formatted = f"{value:.8f}".rstrip("0").rstrip(".")
    return "0" if formatted == "-0" else formatted


def _float(node, name):
    return float(node.get(name, "0"))


def _point(node, x="x", y="y"):
    return _float(node, x), _float(node, y)


def _ordered(nodes):
    return sorted(nodes, key=lambda node: tuple(sorted(node.attrib.items())))


def _node(parent, tag, **attributes):
    return ET.SubElement(parent, tag, {
        key: _number(value) if isinstance(value, float) else str(value)
        for key, value in sorted(attributes.items()) if value is not None
    })


def _class(protected, net):
    return "protected" if net in protected else None


def _title(parent, text):
    _node(parent, "title").text = text


class _Drawing:
    def __init__(self):
        self.svg = ET.Element("svg", xmlns="http://www.w3.org/2000/svg", role="img")
        _title(self.svg, "Candidate board")
        _node(self.svg, "style").text = """
text{font:1.1px sans-serif;fill:#172331}
.fixed text{font-weight:bold;fill:#572686}
.protected-mark{fill:none;stroke:#572686;stroke-dasharray:.2 .25}
.failed-endpoint,.failed-link{fill:none;stroke:#9d174d}
.failed-link{stroke-dasharray:.5 .35}
"""
        self.bounds = []

    def point(self, x, y, margin=0):
        self.bounds.extend(((x - margin, -y - margin), (x + margin, -y + margin)))
        return f"{_number(x)} {_number(-y)}"

    def circle(self, group, point, radius, **attrs):
        x, y = point
        self.point(x, y, radius)
        return _node(group, "circle", cx=x, cy=-y, r=radius, **attrs)

    def path(self, group, points, *, closed=False, **attrs):
        start = points[0][:2]
        path = [f"M {self.point(*start)}"]
        for x, y, curve in points[1:]:
            end = self.point(x, y)
            if curve and start != (x, y):
                radius = math.dist(start, (x, y)) / (2 * abs(math.sin(math.radians(curve) / 2)))
                self.point(*start, radius * 2)
                path.append(f"A {_number(radius)} {_number(radius)} 0 {int(abs(curve) > 180)} {int(curve < 0)} {end}")
            else:
                path.append(f"L {end}")
            start = x, y
        if closed:
            path.append("Z")
        return _node(group, "path", d=" ".join(path), **attrs)

    def wire(self, group, wire, xf=lambda x, y: (x, y), mirror=False, **attrs):
        curve = _float(wire, "curve") * (-1 if mirror else 1)
        return self.path(group, [(*xf(*_point(wire, "x1", "y1")), 0),
                                 (*xf(*_point(wire, "x2", "y2")), curve)], **attrs)


def _polygon_points(node):
    points = [(*_point(vertex), _float(vertex, "curve")) for vertex in node.findall("vertex")]
    return [(*a[:2], points[i - 1][2]) for i, a in enumerate(points + points[:1])]


def _pad_definitions(board):
    """Remove only unsupported pad shapes from the renderer's private XML."""
    definitions, unknown = {}, []
    library_packages = packages(board)
    unsupported = set()
    for key, package in library_packages.items():
        for node in package.findall("pad"):
            try:
                pad = Pad.from_xml(node)
                pad.require_supported_geometry(key[1])
                if pad.shape not in {"round", "square", "octagon", "long", "offset"}:
                    raise UnverifiedGeometry(pad.shape)
            except UnverifiedGeometry:
                unsupported.add((*key, node.attrib["name"]))
                package.remove(node)
    for element in board.findall("./elements/element"):
        name = element.attrib["name"]
        key = element.get("library"), element.get("package")
        for library, package, pad in sorted(unsupported):
            if key == (library, package):
                unknown.append(f"{name}.{pad}: unsupported pad geometry")
        if key in library_packages:
            for pad in library_packages[key]:
                if pad.tag in ("pad", "smd"):
                    definitions[name, pad.attrib["name"]] = pad
    return definitions, unknown


def _restricts(drawing, group, container, stack, unknown, element=None):
    reference = element.get("name") if element is not None else None
    xf, _, mirrored = transform(element) if element is not None else (lambda x, y: (x, y), 0, False)
    for node in _ordered(n for n in container if n.get("layer") in ("41", "42", "43")):
        layer = node.attrib["layer"]
        if element is None and node.tag == "circle" and _float(node, "width") == 0:
            drawing.circle(group, _point(node), _float(node, "radius"), **{"data-layer": layer})
            continue
        try:
            if node.tag == "polygon":
                restrict = _restrict_polygon(node, stack, reference, xf, mirrored)
            elif node.tag in ("rectangle", "polygonshape"):
                restrict = _restrict_shape(node, stack, reference, xf, mirrored)
            else:
                raise UnverifiedGeometry(f"{reference or 'Board'}: unsupported {node.tag} on layer {layer}")
        except UnverifiedGeometry as error:
            unknown.append(str(error))
            continue
        points, half = restrict.data[2:]
        drawing.path(group, [(*p, 0) for p in points], closed=True, **{
            "data-layer": ",".join(map(str, sorted(restrict.layers))) or "vias",
            "stroke-width": max(0.12, 2 * half),
        })


def _routing_overlay(drawing, request, diagnostics):
    group = _node(drawing.svg, "g", id="routing-diagnostics")
    failures = []
    for connection in sorted(request.get("connections", []), key=lambda row: row["id"]):
        row = diagnostics[connection["id"]]
        if row["termination"] == "routed":
            continue
        start, goal = connection["start"], connection["goal"]
        failed = _node(group, "g", **{"data-connection": connection["id"]})
        label = f'{connection["net"]}: {connection["label"]} ({row["termination"]})'
        _title(failed, label)
        drawing.path(failed, [(*start, 0), (*goal, 0)], **{"class": "failed-link", "stroke-width": 0.13})
        for point in (start, goal):
            drawing.circle(failed, point, 0.46, **{"class": "failed-endpoint", "stroke-width": 0.17})
        failures.append(("failed-connection", label))
        for blocker in row["candidate_blockers"]:
            names = " / ".join(name for name in (blocker["net"], blocker["reference"]) if name)
            label = f'Candidate blocker: {blocker["type"]} {names} ({blocker["count"]})'
            failures.append(("candidate-blocker", label))
    return group, failures


def render_board_svg(board_xml, policy, request, diagnostics):
    """Draw persisted geometry and overlay diagnostics without searching."""
    root = ET.fromstring(board_xml)
    board = root.find("./drawing/board")
    design = parse_board_design(root)
    stack, rules = design.copper_layers, design.rules
    protected = frozenset(policy["protected_nets"])
    signals = sorted(board.findall("./signals/signal"), key=lambda node: node.attrib["name"])
    layers = sorted(set(stack) | {
        int(node.attrib["layer"]) for signal in signals for node in signal
        if node.tag in ("wire", "polygon") and node.attrib["layer"] != "19"
    })
    colors = {layer: _COLORS[i % len(_COLORS)] for i, layer in enumerate(layers)}
    drawing = _Drawing()
    definitions, unknown = _pad_definitions(board)
    elements = {node.attrib["name"]: node for node in board.findall("./elements/element")}
    plain = board.find("plain")

    outline = _node(drawing.svg, "g", id="outline", stroke="#172331", fill="none", **{"stroke-width": 0.17})
    for wire in _ordered(plain.findall("wire[@layer='20']")):
        drawing.wire(outline, wire)
    for circle in _ordered(plain.findall("circle[@layer='20']")):
        drawing.circle(outline, _point(circle), _float(circle, "radius"))

    pours = _node(drawing.svg, "g", id="pours", fill="none", **{"stroke-dasharray": ".35 .25"})
    marks = _node(drawing.svg, "g", id="protected-nets", **{"class": "protected-mark", "stroke-width": 0.13})
    for signal in signals:
        net = signal.attrib["name"]
        for polygon in _ordered(signal.findall("polygon")):
            layer = int(polygon.attrib["layer"])
            points = _polygon_points(polygon)
            _title(drawing.path(pours, points, closed=True, stroke=colors[layer], **{
                "class": _class(protected, net), "data-layer": layer, "data-net": net,
                "stroke-width": _float(polygon, "width"),
            }), net)
            if net in protected:
                drawing.path(marks, points, closed=True, **{"stroke-dasharray": ".35 .25", "stroke-dashoffset": 0.3})

    keepouts = _node(drawing.svg, "g", id="keepouts", fill="#f7c482", stroke="#82411b",
                     **{"fill-opacity": 0.22, "stroke-width": 0.12})
    _restricts(drawing, keepouts, plain, stack, unknown)

    routes = _node(drawing.svg, "g", id="routes", fill="none", **{"stroke-linecap": "round"})
    vias = _node(drawing.svg, "g", id="vias")
    pads = _node(drawing.svg, "g", id="pads")
    holes = _node(drawing.svg, "g", id="drills", fill="#fff", stroke="#556476", **{"stroke-width": 0.055})
    for signal in signals:
        net = signal.attrib["name"]
        attrs = {"class": _class(protected, net), "data-net": net}
        for wire in _ordered(signal.findall("wire")):
            layer = int(wire.attrib["layer"])
            if layer == 19:
                continue
            drawing.wire(routes, wire, stroke=colors[layer],
                         **attrs, **{"data-layer": layer, "stroke-width": _float(wire, "width")})
            if net in protected:
                drawing.wire(marks, wire)
        for via in _ordered(signal.findall("via")):
            drill = _float(via, "drill")
            diameters = copper(rules, "Via", drill, _float(via, "diameter"), inner=len(stack) > 2)
            drawing.circle(vias, _point(via), max(diameters.values()) / 2, fill="#c38b23",
                           **attrs, **{"data-extent": via.attrib["extent"]})
            drawing.circle(holes, _point(via), drill / 2)
            if net in protected:
                drawing.circle(marks, _point(via), max(diameters.values()) / 2 + 0.12)

    for pad in sorted(placed_pads(root), key=lambda item: (item.ref, item.name)):
        node = definitions[pad.ref, pad.name]
        attrs = {"class": _class(protected, pad.net), "data-pad": f"{pad.ref}.{pad.name}", "data-net": pad.net}
        if pad.smd:
            native_layer = int(node.attrib["layer"])
            if native_layer not in (stack[0], stack[-1]):
                unknown.append(f"{pad.ref}.{pad.name}: unsupported SMD layer {native_layer}")
                continue
            # Placement pad polygons already apply the element transform.
            mirror = transform(elements[pad.ref])[2]
            layer = stack[-1] if (native_layer == stack[0]) == mirror else stack[0]
            drawing.path(pads, [(*p, 0) for p in pad.poly], closed=True,
                         fill=colors[layer], **attrs, **{"data-layer": layer})
        else:
            drill = _float(node, "drill")
            diameter = max(copper(rules, "Pad", drill, _float(node, "diameter"), inner=len(stack) > 2).values())
            if node.get("shape", "round").lower() == "round":
                drawing.circle(pads, (pad.x, pad.y), diameter / 2, fill="#68788c", **attrs)
            else:
                scale = diameter / pad.size
                drawing.path(pads, [(pad.x + (x - pad.x) * scale, pad.y + (y - pad.y) * scale, 0)
                                    for x, y in pad.poly], closed=True, fill="#68788c", **attrs)
            drawing.circle(holes, (pad.x, pad.y), drill / 2)
        if pad.net in protected:
            drawing.path(marks, [(*p, 0) for p in pad.poly], closed=True)

    for hole in _ordered(plain.findall("hole")):
        drawing.circle(holes, _point(hole), _float(hole, "drill") / 2)

    parts = _node(drawing.svg, "g", id="parts")
    pkgs = packages(board)
    for ref, element in sorted(elements.items()):
        xf, _, mirror = transform(element)
        package = pkgs.get((element.get("library"), element.get("package")))
        fixed = element.get("locked") == "yes"
        group = _node(parts, "g", **{"class": "fixed" if fixed else "part", "data-ref": ref})
        if package is not None:
            _restricts(drawing, keepouts, package, stack, unknown, element)
            for hole in _ordered(package.findall("hole")):
                drawing.circle(holes, xf(*_point(hole)), _float(hole, "drill") / 2)
            for wire in _ordered(n for n in package.findall("wire") if n.get("layer") in ("21", "22")):
                drawing.wire(group, wire, xf, mirror, fill="none", stroke="#556476", **{"stroke-width": 0.1})
            for circle in _ordered(n for n in package.findall("circle") if n.get("layer") in ("21", "22")):
                drawing.circle(group, xf(*_point(circle)), _float(circle, "radius"),
                               fill="none", stroke="#556476", **{"stroke-width": 0.1})
        x, y = _point(element)
        drawing.point(x, y, 1 + len(ref) * 0.32)
        drawing.circle(group, (x, y), 0.22, fill="#572686" if fixed else "#172331")
        _node(group, "text", x=x, y=-y - 0.55, **{"text-anchor": "middle"}).text = ref

    # Paint the marks over the copper while preserving the layer colors underneath.
    drawing.svg.remove(marks)
    drawing.svg.append(marks)
    overlay, failures = _routing_overlay(drawing, request, diagnostics)
    xs, ys = zip(*(drawing.bounds or [(0, 0), (1, 1)]))
    left, top, right, bottom = min(xs) - 1, min(ys) - 1, max(xs) + 2, max(ys) + 1
    legend = _node(drawing.svg, "g", id="layer-legend")
    rows = [(f"{layer} {design.layers.get(layer, 'Copper')}", colors[layer]) for layer in layers]
    rows += [("Fixed placement", "#572686"), ("Protected net (purple dashes)", "#572686"),
             ("Via copper / drill", "#c38b23"), ("Keepout", "#82411b"), ("Unfilled pour (dashed)", "#556476")]
    y = top + 1.2
    width = 0
    for label, color in rows:
        _node(legend, "line", x1=right, x2=right + 1.3, y1=y - 0.36, y2=y - 0.36,
              stroke=color, **{"stroke-width": 0.24})
        _node(legend, "text", x=right + 2, y=y).text = label
        width = max(width, len(label))
        y += 1.6
    for kind, label in failures + [("unverified-geometry", row) for row in sorted(set(unknown))]:
        _node(overlay, "text", x=right + 2, y=y, **{"class": kind}).text = label
        width = max(width, len(label))
        y += 1.6
    drawing.svg.set("viewBox", " ".join(map(_number, (left, top, right - left + 4 + width * 0.8,
                                                       max(bottom, y) - top))))
    return ET.tostring(drawing.svg, encoding="utf-8") + b"\n"
