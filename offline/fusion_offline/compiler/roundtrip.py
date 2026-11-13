"""Compare the declared rules and copper restrictions of EAGLE board exports."""

from functools import partial
import math
import xml.etree.ElementTree as ET

from ..design import BoardDesign
from ..eagle import UnverifiedGeometry, parse_rot
from ..net_classes import _class_numbers, _mm
from ..rules_edit import CLEARANCE
from ..stitch import Obstacle, _restrict_layers, _restrict_polygon, _restrict_shape, _xf


def _length(value):
    number = _mm(value)
    if number is None or not math.isfinite(number):
        raise ValueError(f"Missing or nonfinite rule length: {value!r}")
    return number


def _parameter(name, value):
    if name in {"mtCopper", "mtIsolate"}:
        return [_length(token) for token in value.split()]
    if name == "layerSetup":
        return "".join(value.split())
    if name.startswith(("md", "ms", "rl", "ml", "slThermalIsolate")) or name == "dpMaxLengthDifference":
        return _length(value)
    return value


def _rule_values(board, native, expected=None):
    params = {}
    for param in board.findall("./designrules/param"):
        name = param.attrib["name"]
        if expected is None or name in expected["parameters"]:
            value = _parameter(name, param.attrib["value"])
            if name in params and params[name] != value:
                raise ValueError(f"Conflicting board rule parameter {name}")
            params[name] = value
    classes, pairs = {}, {}
    for cls in board.findall("./classes/class"):
        number = int(cls.attrib["number"])
        if number in classes:
            raise ValueError(f"Duplicate net class {number}")
        classes[number] = {"name": cls.attrib["name"], "width_mm": _length(cls.get("width", "0")),
                           "drill_mm": _length(cls.get("drill", "0"))}
        for clearance in cls.findall("clearance"):
            pair = tuple(sorted((number, int(clearance.attrib["class"]))))
            value = _length(clearance.attrib["value"])
            if pair in pairs and pairs[pair] != value:
                raise ValueError(f"Conflicting symmetric clearance for classes {pair}")
            pairs[pair] = value
    assignments = {net.attrib["name"]: int(net.get("class", "0")) for net in board.findall("./signals/signal")}
    if any(set(pair) - classes.keys() for pair in pairs):
        raise ValueError("Clearance names an undefined class")

    if native is not None:
        _native_rules(native, params, classes, pairs, expected)
    return {"parameters": params, "classes": classes,
            "class_clearances_mm": {f"{a}:{b}": v for (a, b), v in sorted(pairs.items())},
            "net_classes": assignments}


def _native_rules(native, params, classes, pairs, expected):
    # Lowest priority number wins for the supported class/global scopes.
    overrides = {}
    wanted = params if expected is None else expected["parameters"]
    for rule in sorted(native.iter("rule"), key=lambda row: int(row.get("priority", "0"))):
        if rule.get("enabled", "yes") != "yes" or rule.get("samesignal") == "yes":
            continue
        kind = rule.get("type")
        one, other = rule.get("onescope", "all"), rule.get("otherscope", "all")
        first, second = _class_numbers(one), _class_numbers(other)
        keys = []
        if kind in ("Minimum Drill Size", "Minimum Copper Width"):
            field, param = ("drill_mm", "msDrill") if kind == "Minimum Drill Size" else ("width_mm", "msWidth")
            if first:
                keys = [("class", int(number), field) for number in first if int(number) in classes]
            elif one == "all":
                keys = [("param", param)] if param in wanted else []
        elif kind == "Copper Clearance":
            if first and second:
                keys = [("pair", *sorted((int(a), int(b)))) for a in first for b in second
                        if int(a) in classes and int(b) in classes]
            elif not first and not second:
                keys = [("param", param) for a, b, param in CLEARANCE.values()
                        if {a, b} == {one, other} or (one == other == "all")
                        if param in wanted]
        for key in keys:
            overrides.setdefault(key, _length(rule.attrib["value"]))
    for (kind, target, *rest), value in overrides.items():
        if kind == "param":
            params[target] = value
        elif kind == "class":
            classes[target][rest[0]] = value
        else:
            pairs[target, rest[0]] = value


def _restrict_obstacles(board, stack):
    plain = board.find("plain")
    packages = {(lib.attrib["name"], pkg.attrib["name"]): pkg for lib in board.findall("./libraries/library")
                for pkg in lib.findall("./packages/package")}
    placements = [(None, plain, None, False)]
    for element in board.findall("./elements/element"):
        package = packages.get((element.get("library"), element.get("package")))
        if package is None:
            continue
        angle, mirror = parse_rot(element.get("rot"))
        origin = float(element.attrib["x"]), float(element.attrib["y"])
        transform = partial(_xf, x=origin[0], y=origin[1], angle=angle, mirror=mirror)
        placements.append((element.attrib["name"], package, transform, mirror))
    for ref, container, transform, mirror in placements:
        if container is None:
            continue
        for node in container:
            if node.get("layer") not in {"41", "42", "43"}:
                continue
            options = {"source_ref": ref, "transform": transform, "mirror": mirror} if ref else {}
            if node.tag == "polygon":
                yield _restrict_polygon(node, stack, **options)
            elif node.tag in {"rectangle", "polygonshape"}:
                yield _restrict_shape(node, stack, **options)
            elif ref is None and node.tag == "circle" and float(node.get("width", "0")) == 0:
                layer = int(node.attrib["layer"])
                data = tuple(float(node.attrib[key]) for key in ("x", "y", "radius"))
                if not all(map(math.isfinite, data)) or data[2] <= 0:
                    raise UnverifiedGeometry("Invalid restrict circle")
                yield Obstacle("circle", None, True, data, _restrict_layers(layer, stack, mask=node.get("restrictlayers")),
                               restrict_layer=layer)
            else:
                raise UnverifiedGeometry(f"Unsupported restrict {node.tag} on layer {node.get('layer')}")


def _restrictions(root, board):
    layers = {int(row.attrib["number"]): row.get("name", "") for row in root.findall("./drawing/layers/layer")}
    rules = {row.attrib["name"]: row.attrib["value"] for row in board.findall("./designrules/param")}
    stack = tuple(BoardDesign("", layers, {}, {}, rules, []).copper_layers)
    shapes = set()
    for obs in _restrict_obstacles(board, stack):
        if obs.kind == "polygon":
            points, half = obs.data[2:]
            points = tuple((round(x, 4), round(y, 4)) for x, y in points)
            start = points.index(min(points))
            points = points[start:] + points[:start]
            geometry = (min(points, points[:1] + points[:0:-1]), round(half, 4))
        else:
            geometry = tuple(round(value, 4) for value in obs.data)
        for layer in obs.layers if obs.restrict_layer != 43 else ("via",):
            shapes.add((obs.source_ref or "", obs.kind, layer, geometry))
    return {"copper_stack": stack,
            "board": sorted((kind, str(layer), geom) for ref, kind, layer, geom in shapes if not ref),
            "packages": sorted((ref, kind, str(layer), geom) for ref, kind, layer, geom in shapes if ref)}


def _result(expected, observed):
    changes = {key: {"expected": expected[key], "observed": observed[key]}
               for key in expected if expected[key] != observed[key]}
    return {"status": "fail" if changes else "pass", "expected": expected, "observed": observed, "changes": changes}


def compare_native_board(candidate_board, native_board, native_rules_xml=None):
    """Compare classic export semantics and optional native V2 rule readback.

    Rule normalization covers declared parameters and supported V2 clearance,
    minimum width, and minimum drill scopes. Restriction equality compares
    placement-transformed contours per copper layer and via restrictions.
    """
    expected, observed = ET.fromstring(candidate_board), ET.fromstring(native_board)
    a, b = (root.find("./drawing/board") for root in (expected, observed))
    if a is None or b is None:
        raise ValueError("Expected candidate and native EAGLE boards")
    try:
        native = ET.fromstring(native_rules_xml) if native_rules_xml is not None else None
        baseline = _rule_values(a, a.find("designrules"))
        rules = _result(baseline, _rule_values(b, native, baseline))
        rules["native_v2_readback"] = native is not None
        rules["source"] = "native V2 rules and EAGLE export" if native is not None else "EAGLE export"
        rules["parameter_counts"] = [len(board.findall("./designrules/param")) for board in (a, b)]
        rules["class_clearance_row_counts"] = [len(board.findall("./classes/class/clearance")) for board in (a, b)]
    except (ValueError, KeyError, ET.ParseError) as error:
        rules = {"status": "unverified", "detail": str(error)}
    try:
        restrictions = _result(_restrictions(expected, a), _restrictions(observed, b))
        restrictions["board_status"] = (
            "fail" if {"copper_stack", "board"} & restrictions["changes"].keys() else "pass"
        )
        restrictions["package_status"] = "fail" if "packages" in restrictions["changes"] else "pass"
    except (ValueError, KeyError) as error:
        restrictions = {"status": "unverified", "detail": str(error)}
    return {"rules": rules, "restrictions": restrictions}
