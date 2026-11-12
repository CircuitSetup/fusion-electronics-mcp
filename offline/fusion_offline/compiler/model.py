"""Strict version-1 design shape validation before source resolution."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
import re
from sys import float_info

SPEC_VERSION = 1
ROUTING_BUDGET_DEFAULTS = {
    "max_nodes_per_attempt": 200_000,
    "max_nodes_per_connection": 300_000,
    "max_attempts_per_connection": 2,
    "max_seconds_per_attempt": 15,
    "max_seconds_per_job": 600,
    "max_vias_per_connection": 2,
    "ripup_rounds": 2,
}
ROUTING_BUDGET_MAXIMUMS = {
    "max_nodes_per_attempt": 1_000_000,
    "max_nodes_per_connection": 2_000_000,
    "max_attempts_per_connection": 8,
    "max_seconds_per_attempt": 60,
    "max_seconds_per_job": 1800,
    "max_vias_per_connection": 8,
    "ripup_rounds": 8,
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.$@+!~-]+")


def _object(value, field, required="", optional=""):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    needed = set(required.split())
    unknown = value.keys() - needed - set(optional.split())
    if unknown:
        raise ValueError(f"{field} has unknown fields {sorted(unknown, key=str)}")
    if missing := needed - value.keys():
        raise ValueError(f"{field} is missing fields {sorted(missing)}")


def _list(value, field, minimum=0):
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError(f"{field} must be an array with at least {minimum} entries")
    return value


def _identifier(value, field, *, empty=False):
    if not isinstance(value, str) or not (_IDENTIFIER.fullmatch(value) or (empty and value == "")):
        raise ValueError(f"{field} must be a supported EAGLE identifier")


def _text(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")


def _number(value, field, *, integer=False):
    if (type(value) not in (int, float) or (integer and type(value) is not int)
            or not -float_info.max <= value <= float_info.max):
        raise ValueError(f"{field} must be a finite {'integer' if integer else 'number'}")


def _boolean(value, field):
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")


def _unique(key, seen, field):
    if key in seen:
        raise ValueError(f"duplicate {field} {key!r}")
    seen.add(key)


def _asset(asset, field, *, library=False):
    _object(asset, field, "name path sha256" if library else "path sha256")
    path = asset["path"]
    if (not isinstance(path, str) or "\x00" in path
            or not (PureWindowsPath(path).is_absolute() or PurePosixPath(path).is_absolute())):
        raise ValueError(f"{field}.path must be absolute")
    sha = asset["sha256"]
    if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", sha):
        raise ValueError(f"{field}.sha256 must be a 64-hex SHA-256")


def _xy(row):
    for axis in ("x", "y"):
        _number(row[axis], axis)


def _layer(value):
    _number(value, "layer", integer=True)
    if not 1 <= value <= 304:
        raise ValueError("layer must be a copper export layer ID (1 through 304)")


def _layers(value):
    seen = set()
    for layer in _list(value, "layers", 1):
        _layer(layer)
        _unique(layer, seen, "layer")


def _vertices(value, field, minimum):
    vertices = _list(value, field, minimum)
    if vertices and len(vertices) < 3:
        raise ValueError(f"{field} requires at least three vertices")
    for point in vertices:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError(f"{field} vertices must be [x, y]")
        _number(point[0], f"{field}.x")
        _number(point[1], f"{field}.y")


def _placement(row, *, separate=False):
    _object(row, "placement", "x y rotation side fixed" + (" ref" if separate else ""), "allow_outside")
    _xy(row)
    _number(row["rotation"], "board rotation")
    if not 0 <= row["rotation"] < 360:
        raise ValueError("board rotation must be in [0, 360)")
    if row["side"] not in ("top", "bottom"):
        raise ValueError("placement side must be top or bottom")
    _boolean(row["fixed"], "fixed")
    if "allow_outside" in row:
        _boolean(row["allow_outside"], "allow_outside")


def _sheets(schematic):
    _object(schematic, "schematic", "sheets")
    sheets = set()
    for row in _list(schematic["sheets"], "sheets"):
        _object(row, "sheet", "number", "description")
        number = row["number"]
        _number(number, "sheet number", integer=True)
        if number < 1:
            raise ValueError("sheet number must be positive")
        if "description" in row:
            _text(row["description"], "sheet description")
        _unique(number, sheets, "sheet")
    return sheets


def _parts(rows, libraries, sheets):
    parts, references = {}, set()
    for part in _list(rows, "parts"):
        _object(part, "part", "ref library deviceset device technology value attributes units board")
        for field in ("ref", "library", "deviceset", "device", "technology"):
            _identifier(part[field], field, empty=field in ("device", "technology"))
        ref = part["ref"]
        _unique(ref.casefold(), references, "ref")
        if part["library"] not in libraries:
            raise ValueError(f"unknown library {part['library']!r} for part {ref}")
        _text(part["value"], "value")
        attributes = part["attributes"]
        if not isinstance(attributes, dict):
            raise ValueError("attributes must be an object")
        for name, value in attributes.items():
            _identifier(name, "attribute name")
            _text(value, "attribute value")
        gates, gate_names = {}, set()
        for unit in _list(part["units"], "units", 1):
            _object(unit, "unit", "gate sheet x y rotation")
            _identifier(unit["gate"], "gate")
            _unique(unit["gate"].casefold(), gate_names, "gate")
            _number(unit["sheet"], "sheet", integer=True)
            if unit["sheet"] not in sheets:
                raise ValueError(f"unit refers to undeclared sheet {unit['sheet']}")
            _xy(unit)
            _number(unit["rotation"], "schematic rotation")
            if unit["rotation"] not in (0, 90, 180, 270):
                raise ValueError("schematic rotation must be 0, 90, 180, or 270")
            gates[unit["gate"]] = unit["sheet"]
        parts[ref] = gates
        if part["board"] is not None:
            _placement(part["board"])
    return parts


def _board(board, parts, physical):
    _object(board, "board", "outline holes keepouts placements net_classes pours", "copper_layers")
    if "copper_layers" in board:
        layers = board["copper_layers"]
        _layers(layers)
        if len(layers) < 2 or layers != sorted(layers):
            raise ValueError("copper_layers must have at least two IDs in stack order")
    _vertices(board["outline"], "outline", 0)
    for hole in _list(board["holes"], "holes"):
        _object(hole, "hole", "x y drill")
        _xy(hole)
        _number(hole["drill"], "hole drill")
        if hole["drill"] <= 0:
            raise ValueError("hole drill must be positive")
    for keepout in _list(board["keepouts"], "keepouts"):
        _object(keepout, "keepout", "vertices layers")
        _vertices(keepout["vertices"], "keepout vertices", 3)
        _layers(keepout["layers"])
    for row in _list(board["placements"], "placements"):
        _placement(row, separate=True)
        _identifier(row["ref"], "placement ref")
        if row["ref"] not in parts:
            raise ValueError(f"placement refers to undeclared part {row['ref']}")
        _unique(row["ref"], physical, "placement")
    class_names, class_numbers = set(), set()
    for row in _list(board["net_classes"], "net_classes"):
        _object(row, "net class", "number name width drill", "clearances")
        _identifier(row["name"], "net class name")
        _number(row["number"], "net class number", integer=True)
        _unique(row["name"].casefold(), class_names, "net class name")
        _unique(row["number"], class_numbers, "net class number")
        if not 0 <= row["number"] <= 15:
            raise ValueError("net class number must be 0 through 15")
        for field in ("width", "drill"):
            _number(row[field], f"net class {field}")
            if row[field] < 0:
                raise ValueError(f"net class {field} must be nonnegative")
        clearances = row.get("clearances", {})
        if not isinstance(clearances, dict):
            raise ValueError("net class clearances must be an object")
        for name, value in clearances.items():
            _identifier(name, "clearance class name")
            _number(value, "class clearance")
            if value < 0:
                raise ValueError("class clearance must be nonnegative")


def _net_geometry(net):
    for wire in _list(net.get("wires", []), "wires"):
        _object(wire, "wire", "x1 y1 x2 y2 layer width")
        _layer(wire["layer"])
        for field in ("x1", "y1", "x2", "y2", "width"):
            _number(wire[field], f"wire {field}")
        if wire["width"] <= 0:
            raise ValueError("wire width must be positive")
    for via in _list(net.get("vias", []), "vias"):
        _object(via, "via", "x y drill extent", "diameter")
        _xy(via)
        extent = via["extent"]
        match = re.fullmatch(r"([1-9][0-9]{0,2})-([1-9][0-9]{0,2})", extent) if isinstance(extent, str) else None
        if match is None or not 1 <= int(match[1]) < int(match[2]) <= 304:
            raise ValueError("via extent must be two increasing copper export layer IDs")
        _number(via["drill"], "via drill")
        _number(via.get("diameter", 0), "via diameter")
        if via["drill"] <= 0 or via.get("diameter", 0) < 0:
            raise ValueError("via drill must be positive and diameter must be nonnegative")


def _nets(rows, parts, physical):
    nets, names, assigned_pins = {}, set(), set()
    for net in _list(rows, "nets"):
        _object(net, "net", "name class pins schematic_style route", "port wires vias")
        name = net["name"]
        _identifier(name, "net name")
        _unique(name.casefold(), names, "net name")
        _identifier(net["class"], "net class")
        _boolean(net["route"], "route")
        if net["schematic_style"] not in ("wired", "labels"):
            raise ValueError("schematic_style must be wired or labels")
        if "port" in net and net["port"] not in ("inter-block", "inter-sheet"):
            raise ValueError("port must be inter-block or inter-sheet")
        has_physical_pin = False
        pin_sheets = set()
        for pin in _list(net["pins"], "pins", 1):
            _object(pin, "pin", "part gate pin")
            for field in ("part", "gate", "pin"):
                _identifier(pin[field], field)
            ref, gate = pin["part"], pin["gate"]
            if ref not in parts:
                raise ValueError(f"pin refers to undeclared part {ref}")
            if gate not in parts[ref]:
                raise ValueError(f"pin refers to unplaced gate {ref}.{gate}")
            _unique((ref, gate, pin["pin"].casefold()), assigned_pins, "pin")
            has_physical_pin |= ref in physical
            pin_sheets.add(parts[ref][gate])
        if net["route"] and not has_physical_pin:
            raise ValueError(f"routed net {name} needs physical contacts")
        if net["schematic_style"] == "labels" and (
            "port" not in net or (net["port"] == "inter-sheet" and len(pin_sheets) < 2)
        ):
            raise ValueError("labels require an explicit inter-block or inter-sheet port")
        _net_geometry(net)
        nets[name] = net
    return nets


def _pours(rows, nets):
    for pour in _list(rows, "pours"):
        _object(pour, "pour", "net layer vertices width", "isolate rank thermals orphans")
        _identifier(pour["net"], "pour net")
        if pour["net"] not in nets:
            raise ValueError(f"pour refers to undeclared net {pour['net']}")
        _layer(pour["layer"])
        _vertices(pour["vertices"], "pour vertices", 3)
        _number(pour["width"], "pour width")
        _number(pour.get("isolate", 0), "pour isolate")
        if pour["width"] <= 0 or pour.get("isolate", 0) < 0:
            raise ValueError("pour width must be positive and isolate must be nonnegative")
        if "rank" in pour:
            _number(pour["rank"], "pour rank", integer=True)
            if not 1 <= pour["rank"] <= 6:
                raise ValueError("pour rank must be 1 through 6")
        for field in ("thermals", "orphans"):
            if field in pour:
                _boolean(pour[field], field)


def _routing(routing, nets):
    _object(routing, "routing", "selected_nets protected_nets policies budget", "require_fully_routed")
    if "require_fully_routed" in routing:
        _boolean(routing["require_fully_routed"], "require_fully_routed")
    listed = set()
    for field in ("selected_nets", "protected_nets"):
        for name in _list(routing[field], field):
            _identifier(name, field)
            if name not in nets:
                raise ValueError(f"{field} refers to undeclared net {name}")
            _unique(name, listed, "selected or protected net")
            if field == "selected_nets" and not nets[name]["route"]:
                raise ValueError(f"selected net {name} must allow routing")
    policies = routing["policies"]
    if not isinstance(policies, dict):
        raise ValueError("policies must be an object")
    for name, policy in policies.items():
        _identifier(name, "policy net")
        if name not in nets:
            raise ValueError(f"policy refers to undeclared net {name}")
        _object(policy, "policy", "", "allowed_layers max_vias_per_connection")
        if "allowed_layers" in policy:
            _layers(policy["allowed_layers"])
        if "max_vias_per_connection" in policy:
            _budget_value("max_vias_per_connection", policy["max_vias_per_connection"])
    budget = routing["budget"]
    _object(budget, "budget", "", " ".join(ROUTING_BUDGET_MAXIMUMS))
    for field, value in budget.items():
        _budget_value(field, value)


def _budget_value(field, value):
    maximum = ROUTING_BUDGET_MAXIMUMS[field]
    _number(value, field, integer=not field.startswith("max_seconds_"))
    if not 0 < value <= maximum:
        raise ValueError(f"{field} must be positive and at most {maximum}")


def parse_design_spec(data: object) -> dict:
    """Check the JSON shape. Pinned asset and exact physical-pad checks follow."""
    _object(data, "design", "version name units templates libraries parts schematic board nets routing")
    if type(data["version"]) is not int or data["version"] != SPEC_VERSION:
        raise ValueError("version must be 1")
    if data["units"] != "mm":
        raise ValueError("units must be mm")
    _identifier(data["name"], "design name")
    _object(data["templates"], "templates", "schematic board")
    for kind, asset in data["templates"].items():
        _asset(asset, f"templates.{kind}")
    libraries, library_names = set(), set()
    for library in _list(data["libraries"], "libraries"):
        _asset(library, "library", library=True)
        _identifier(library["name"], "library name")
        _unique(library["name"].casefold(), library_names, "library name")
        libraries.add(library["name"])
    sheets = _sheets(data["schematic"])
    parts = _parts(data["parts"], libraries, sheets)
    physical = {part["ref"] for part in data["parts"] if part["board"] is not None}
    _board(data["board"], parts, physical)
    nets = _nets(data["nets"], parts, physical)
    _pours(data["board"]["pours"], nets)
    _routing(data["routing"], nets)
    return data
