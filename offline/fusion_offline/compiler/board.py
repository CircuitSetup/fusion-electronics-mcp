"""Build a flat EAGLE board from pinned packages and emitted schematic nets."""
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal
import xml.etree.ElementTree as ET

from ..design import parse_board_design, parse_schematic_design, transform
from ..stitch import _inside_poly, _restrict_polygon, segments_intersect
from .canonical import eagle_xml_bytes
from .libraries import ResolvedLibraries
from .model import DesignSpec, ROUTING_BUDGET_DEFAULTS
from .schematic import apply_net_classes


def _append(parent, tag, row, exclude=()):
    return ET.SubElement(parent, tag, {
        key: ("yes" if value else "no") if isinstance(value, bool) else str(value)
        for key, value in row.items() if key not in exclude
    })


def _polygon(parent, vertices, row, exclude=()):
    polygon = _append(parent, "polygon", row, exclude)
    for x, y in vertices:
        _append(polygon, "vertex", {"x": x, "y": y})
    return polygon


def _check_stack(root, sch_root, expected):
    design = parse_board_design(root)
    stack = tuple(design.copper_layers)
    board_layers = root.findall("./drawing/layers/layer")
    sch_layers = sch_root.findall("./drawing/layers/layer")
    schematic_names = {int(layer.attrib["number"]): layer.get("name") for layer in sch_layers}
    setup = design.rules.get("layerSetup", "")
    if (
        not any(char.isdigit() for char in setup)
        or len(stack) < 2 or tuple(sorted(stack)) != stack
        or len(board_layers) != len(design.layers) or len(sch_layers) != len(schematic_names)
        or any(not design.layers.get(layer) or design.layers[layer] != schematic_names.get(layer)
               for layer in stack)
        or (expected is not None and expected != stack)
    ):
        raise ValueError(f"incompatible template copper stack {stack}; expected {expected}")
    return stack, design.layers


def _emit_plain(plain, board, stack, layers):
    outline = board["outline"]
    if outline:
        if layers.get(20) != "Dimension":
            raise ValueError("board template must declare the Dimension layer")
        vertices = outline[:-1] if outline[0] == outline[-1] else outline
        _restrict_polygon(_polygon(ET.Element("plain"), vertices, {"layer": 41}), stack, "outline")
        for node in plain.findall("*[@layer='20']"):
            plain.remove(node)
        for (x1, y1), (x2, y2) in zip(vertices, vertices[1:] + vertices[:1]):
            _append(plain, "wire", dict(x1=x1, y1=y1, x2=x2, y2=y2, width=0, layer=20))
    for hole in board["holes"]:
        _append(plain, "hole", hole)
    restrict = {stack[0]: (41, "tRestrict"), stack[-1]: (42, "bRestrict")}
    for keepout in board["keepouts"]:
        for layer in keepout["layers"]:
            if layer not in restrict:
                raise ValueError(f"unsupported native restrict geometry for copper layer {layer}")
            number, name = restrict[layer]
            if layers.get(number) != name:
                raise ValueError(f"board template must declare the {name} layer")
            polygon = _polygon(plain, keepout["vertices"], {"layer": number, "width": 0})
            _restrict_polygon(polygon, stack)


def _emit_elements(elements, spec, libraries, stack):
    separate = {row["ref"]: row for row in spec.board["placements"]}
    outline = spec.board["outline"]
    if outline and outline[0] == outline[-1]:
        outline = outline[:-1]
    edges = tuple(zip(outline, outline[1:] + outline[:1]))
    packages = {(name, node.attrib["name"]): node
                for name, library in libraries.libraries.items()
                for node in library.findall("./packages/package")}
    centres = {}
    for row in spec.parts:
        ref = row["ref"]
        part = libraries.parts[ref]
        if not part.physical:
            continue
        placement = separate.get(ref, row["board"])
        x, y, angle = (placement[key] for key in ("x", "y", "rotation"))
        mirror = placement["side"] == "bottom"
        if outline and not placement.get("allow_outside"):
            point = (x, y)
            if not _inside_poly(outline, x, y) and not any(
                segments_intersect(a, b, point, point) for a, b in edges
            ):
                raise ValueError(f"{ref}: placement is outside the board outline")
        element = _append(elements, "element", {
            "name": ref, "library": part.library, "package": part.package,
            "value": row["value"], "x": x, "y": y, "locked": placement["fixed"],
            "rot": f'{"M" if mirror else ""}R{Decimal(str(angle)):f}',
        })
        for name, value in sorted((dict(part.attributes) | dict(row["attributes"])).items()):
            _append(element, "attribute", dict(name=name, value=value, display="off"))
        package = packages[part.library, part.package]
        for pad in package:
            if pad.tag not in ("pad", "smd"):
                continue
            px, py = transform(float(pad.get("x", "0")), float(pad.get("y", "0")), x, y, angle, mirror)
            pad_layers = stack
            if pad.tag == "smd":
                layer = int(pad.attrib["layer"])
                if layer not in (stack[0], stack[-1]):
                    raise ValueError(f"{ref}.{pad.get('name')}: unsupported SMD layer {layer}")
                pad_layers = (stack[-1] if (layer == stack[0]) == mirror else stack[0],)
            centres[ref, pad.get("name")] = {(px, py, layer) for layer in pad_layers}
    return centres


def _check_schematic_parts(schematic, pinned_parts):
    identity_fields = ("library", "deviceset", "device", "technology", "package")
    for ref, part in schematic.parts.items():
        pinned = pinned_parts[ref]
        if any(getattr(part, field) != getattr(pinned, field) for field in identity_fields):
            raise ValueError(f"{ref}: schematic part identity differs from the pinned device")
        expected = {(gate, pin, pad) for (gate, pin), pads in pinned.pads.items() for pad in pads}
        if part.connects != expected:
            raise ValueError(f"{ref}: schematic pad connections differ from the pinned device")


def _contacts(net, parts):
    contacts = set()
    for pin in net.pins:
        part = parts.get(pin.part)
        gate = part.gates.get(pin.gate) if part else None
        if gate is None or pin.pin not in gate.pins:
            raise ValueError(f"{net.name}: unknown schematic pin {pin.part}.{pin.gate}.{pin.pin}")
        if part.physical:
            pads = part.pads.get((pin.gate, pin.pin))
            if pads is None:
                raise ValueError(f"{net.name}: no physical pads for pin {pin.part}.{pin.gate}.{pin.pin}")
            contacts.update((pin.part, pad) for pad in pads)
    return sorted(contacts)


def _emit_preroute(signal, net, stack, routing, contact_centres):
    wires, vias = net.get("wires", ()), net.get("vias", ())
    if not wires and not vias:
        return
    name = net["name"]

    policy = routing["policies"].get(name, {})
    allowed = set(policy.get("allowed_layers", stack))
    if not allowed <= set(stack):
        raise ValueError(f"{name}: policy includes an absent copper layer")
    wire_graph = defaultdict(set)
    degree = Counter()
    for wire in wires:
        layer = wire["layer"]
        if layer not in allowed:
            raise ValueError(f"{name}: wire copper layer {layer} is absent or disallowed")
        start = wire["x1"], wire["y1"], layer
        end = wire["x2"], wire["y2"], layer
        if start == end or end in wire_graph[start]:
            raise ValueError(f"{name}: zero-length or duplicate wire")
        wire_graph[start].add(end)
        wire_graph[end].add(start)
        degree.update((start, end))

    via_points = set()
    via_nodes = set()
    for via in vias:
        if via["extent"] != f"{stack[0]}-{stack[-1]}":
            raise ValueError(f"{name}: unsupported via span {via['extent']}")
        if not set(stack) <= allowed:
            raise ValueError(f"{name}: via span includes a disallowed copper layer")
        xy = via["x"], via["y"]
        if xy in via_points:
            raise ValueError(f"{name}: duplicate via")
        via_points.add(xy)
        nodes = {(*xy, layer) for layer in stack}
        touched = nodes & degree.keys()
        if not touched:
            raise ValueError(f"{name}: via has no trace endpoint at its centre")
        via_nodes.update(touched)
        for node in touched:
            wire_graph[node].update(touched - {node})

    anchors = set().union(*contact_centres)
    if any(count == 1 and node not in anchors and node not in via_nodes for node, count in degree.items()):
        raise ValueError(f"{name}: trace endpoint must meet a pad or via centre or another trace vertex")
    for pad in contact_centres:
        touched = pad & degree.keys()
        for node in touched:
            wire_graph[node].update(touched - {node})

    budget_key = "max_vias_per_connection"
    job_limit = routing["budget"].get(budget_key, ROUTING_BUDGET_DEFAULTS[budget_key])
    limit = min(job_limit, policy.get(budget_key, job_limit))
    remaining = set(wire_graph)
    while remaining:
        component = set()
        pending = [remaining.pop()]
        while pending:
            node = pending.pop()
            component.add(node)
            adjacent = wire_graph[node] & remaining
            remaining.difference_update(adjacent)
            pending.extend(adjacent)
        if not component & anchors:
            raise ValueError(f"{name}: disconnected trace endpoints have no contact pad")
        # shortcut: an explicit copper component has no per-connection ownership; count its vias together.
        if len({node[:2] for node in component} & via_points) > limit:
            raise ValueError(f"{name}: explicit connection exceeds its via budget of {limit}")
    for wire in wires:
        _append(signal, "wire", wire)
    for via in vias:
        _append(signal, "via", via)


def build_board(
    spec: DesignSpec, template_xml: bytes, libraries: ResolvedLibraries, schematic_xml: bytes,
) -> bytes:
    """Emit explicit copper and exact gate-qualified schematic contacts."""
    root, sch_root = ET.fromstring(template_xml), ET.fromstring(schematic_xml)
    boards = root.findall("./drawing/board")
    schematics = sch_root.findall("./drawing/schematic")
    if root.tag != "eagle" or len(boards) != 1:
        raise ValueError("expected one EAGLE board template")
    if sch_root.tag != "eagle" or len(schematics) != 1 or sch_root.get("version") != root.get("version"):
        raise ValueError("expected a compatible EAGLE schematic")
    schematic = schematics[0]
    if schematic.find("./modules/*") is not None or schematic.find(".//moduleinst") is not None:
        raise ValueError("schematic must be flat")
    document = boards[0]
    plain, embedded, elements, signals = (document.find(tag) for tag in ("plain", "libraries", "elements", "signals"))
    if any(node is None for node in (plain, embedded, elements, signals)):
        raise ValueError("board template must contain plain, libraries, elements, and signals")
    if len(elements) or len(signals) or document.find(".//moduleinst") is not None:
        raise ValueError("board template must be empty and flat")
    stack, layers = _check_stack(root, sch_root, spec.board.get("copper_layers"))

    copper_layers = {str(layer) for layer in stack}
    if any(node.get("layer") in copper_layers for node in plain):
        raise ValueError("board template must be empty of copper")
    _emit_plain(plain, spec.board, stack, layers)
    for library in list(embedded):
        embedded.remove(library)
    for library in libraries.libraries.values():
        packages = library.find("packages")
        if packages is not None and len(packages):
            board_library = ET.SubElement(embedded, "library", library.attrib)
            board_library.append(deepcopy(packages))
    classes = apply_net_classes(document, spec.board["net_classes"])
    centres = _emit_elements(elements, spec, libraries, stack)

    design = parse_schematic_design(sch_root)
    nets = {row["name"]: row for row in spec.nets}
    if set(design.parts) != set(libraries.parts) or design.nets.keys() != nets.keys():
        raise ValueError("schematic parts and nets must match the design spec")
    _check_schematic_parts(design, libraries.parts)
    board_signals = {}
    assigned = set()
    for name, net in sorted(design.nets.items()):
        contacts = _contacts(net, libraries.parts)
        if not contacts:
            if nets[name].get("wires") or nets[name].get("vias"):
                raise ValueError(f"{name}: explicit copper needs physical contacts")
            continue
        if assigned.intersection(contacts):
            raise ValueError(f"{name}: physical pad belongs to multiple nets")
        assigned.update(contacts)
        if net.net_class not in classes:
            raise ValueError(f"{name}: unknown board net class {net.net_class!r}")
        signal = ET.SubElement(signals, "signal", name=name, **{"class": classes[net.net_class]})
        board_signals[name] = signal
        for ref, pad in contacts:
            ET.SubElement(signal, "contactref", element=ref, pad=pad)
        _emit_preroute(signal, nets[name], stack, spec.routing, [centres[contact] for contact in contacts])
    for pour in spec.board["pours"]:
        name = pour["net"]
        if name not in board_signals:
            raise ValueError(f"{name}: pour needs physical contacts")

        if pour["layer"] not in stack:
            raise ValueError(f"{name}: pour uses absent copper layer {pour['layer']}")
        polygon = _polygon(board_signals[name], pour["vertices"], pour, ("net", "vertices"))
        _restrict_polygon(polygon, stack, f"{name} pour")
    return eagle_xml_bytes(root, template_xml)
