"""Component library: JSON parts (library/parts/*.json, schema in
library/schema.json) and their Fusion build scripts.

Scripts use the same command dialect as Groundplane's fabhub/scrgen.py, which
the Phase 1 spike ran successfully through Electron.runScript: EDIT .pac/.sym/
.dev, SMD, PIN, CONNECT, TECHNOLOGY '', ATTRIBUTE, and the layer-114 JLC
marker for JLC-native footprints. The EAGLE `ATTRIBUTE SET` grammar is broken
in Fusion and is never emitted.
"""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse
import os
from dataclasses import dataclass

from .commands import n, q
from fusion_offline.kicad_import import validate_pad_names
from fusion_offline.eagle import Pad

from . import data_dir

# the component library lives in the per-user data folder (FUSION_MCP_LIBRARY overrides);
# the parts come from the separate library repository
DEFAULT_DIR = data_dir("library", "parts")
MARKER_LAYER = 114
MILLING_LAYER = 46
# >NAME / >VALUE in new packages: proportional font, ratio 8 %, bottom-left aligned, >NAME stacked
# above >VALUE above the top-left corner of the footprint (Groundplane library style)
TEXT_STYLE = {"size": 0.8128, "ratio": 8, "font": "PROPORTIONAL", "align": "BOTTOM LEFT", "gap": 0.25}


@dataclass
class Part:
    data: dict

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def device_name(self) -> str:
        """The ADD name: deviceset + device (EAGLE concatenates them)."""
        return self.data["deviceset"] + (self.data.get("device") or "")

    def summary(self) -> dict:
        d = self.data
        return {"id": d["id"], "deviceset": d["deviceset"], "device": d.get("device", ""),
                "description": d.get("description", ""), "category": d.get("category", ""),
                "package": d["package"]["name"],
                "pads": len(d["package"].get("smds", [])) + len(d["package"].get("pads", [])),
                "attributes": d.get("attributes", {}), "metadata": d.get("metadata", {})}


class Library:
    def __init__(self, directory: str | None = None):
        self.directory = os.path.abspath(directory or os.environ.get("FUSION_MCP_LIBRARY") or DEFAULT_DIR)
        self.parts: dict[str, Part] = {}
        if os.path.isdir(self.directory):
            for fn in sorted(os.listdir(self.directory)):
                if fn.endswith(".json"):
                    with open(os.path.join(self.directory, fn), encoding="utf-8") as f:
                        p = Part(json.load(f))
                    self.parts[p.id] = p

    def add(self, data: dict, overwrite: bool = False) -> str:
        """Save a part definition as <id>.json in the library folder; returns the path."""
        os.makedirs(self.directory, exist_ok=True)
        path = os.path.join(self.directory, f"{data['id']}.json")
        if os.path.exists(path) and not overwrite:
            raise FileExistsError(f"part {data['id']!r} already exists ({path}); pass overwrite=true to replace it")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.parts[data["id"]] = Part(data)
        return path

    def search(self, query: str, limit: int = 20) -> list[dict]:
        terms = [t for t in query.lower().split() if t]
        hits = []
        for p in self.parts.values():
            hay = json.dumps(p.summary()).lower()
            if all(t in hay for t in terms):
                hits.append(p.summary())
        return hits[:limit]

    def get(self, part_id: str) -> Part:
        if part_id not in self.parts:
            raise KeyError(f"no library part {part_id!r}")
        return self.parts[part_id]


def same_package(el, pkg: dict, tol: float = 0.02) -> bool:
    """Compare declared pad parameters for reuse, including SLOT shape, length and axis.
    Matching XML fields do not certify native SLOT extents or manufactured cuts."""
    def key(name, x, y, a, b):
        return (str(name).upper(), round(float(x) / tol), round(float(y) / tol), round(float(a) / tol), round(float(b) / tol))
    def pad_key(p):
        period = {"round": 1, "square": 90, "octagon": 45, "slot": 180, "long": 180}.get(p.shape, 360)
        return key(p.name, p.x, p.y, p.drill, p.diameter) + (p.shape, round((p.rot % period) / tol),
                                                         round(p.slot_length / tol) if p.slot_length is not None else None)
    got_pads = [Pad.from_xml(p) for p in el.findall("pad")]
    want_pads = [Pad(p["name"], p["x"], p["y"], p["drill"], p.get("diameter", 0),
                     p.get("shape", "round"), p.get("rot", 0), p.get("slot_length")) for p in pkg.get("pads", [])]
    if any(p.shape == "slot" and p.slot_length is None for p in got_pads + want_pads):
        return False
    got = sorted(key(s.get("name"), s.get("x"), s.get("y"), s.get("dx"), s.get("dy")) for s in el.findall("smd"))
    want = sorted(key(s["name"], s["x"], s["y"], s["dx"], s["dy"]) for s in pkg.get("smds", []))
    return got == want and sorted(map(pad_key, got_pads)) == sorted(map(pad_key, want_pads))


def configure_part(part: dict, pin_groups=None, pin_units=None, pin_sides=None, datasheet=None) -> None:
    """Apply explicit datasheet layout choices; never infer electrical pin functions."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", part["id"]):
        raise ValueError("part_id must contain lowercase letters, digits and hyphens")
    pins = part["symbol"]["pins"]
    names = {p["name"] for p in pins}
    if len({(p.get("unit", "G$1").upper(), p["name"].upper()) for p in pins}) != len(pins):
        raise ValueError("symbol pin names must be unique within each gate, ignoring case")
    if len(names) != len(pins) and any(x is not None for x in (pin_groups,pin_units,pin_sides)):
        raise ValueError("Duplicate pin names require explicit unit/group/side fields, not name-based maps")
    for pin in pins:
        if not re.fullmatch(r"[A-Za-z0-9_$-]+", pin.get("unit", "G$1")): raise ValueError("Invalid unit name")
        q(pin["name"])
        if pin.get("direction", "pas") not in {"nc", "in", "out", "io", "oc", "pwr", "pas", "hiz", "sup"}:
            raise ValueError(f"invalid pin direction for {pin['name']}")
    for field, mapping in (("group", pin_groups), ("unit", pin_units)):
        if mapping is None:
            continue
        if len({label.upper() for label in mapping}) != len(mapping):
            raise ValueError(f"{field} names must be unique, ignoring case")
        assigned = []
        for label, members in mapping.items():
            if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_$-]+", label) or not members:
                raise ValueError(f"{field} names must be identifiers and each must contain pins")
            assigned.extend(members)
        if len(assigned) != len(set(assigned)) or set(assigned) != names:
            raise ValueError(f"{field} must assign every pin exactly once: {sorted(names)}")
        order = {name: i for i, name in enumerate(assigned)}
        if field == "group":
            pins.sort(key=lambda p: order[p["name"]])
        for label, members in mapping.items():
            for pin in pins:
                if pin["name"] in members:
                    pin[field] = label
        part["symbol"]["style"] = False
    for name, side in (pin_sides or {}).items():
        if name not in names or side not in {"left", "right"}:
            raise ValueError("pin_sides must map existing pin names to left or right")
        next(p for p in pins if p["name"] == name)["side"] = side
    if datasheet is not None:
        if set(datasheet) - {"source", "revision", "pages", "sha256"}:
            raise ValueError("datasheet supports source, revision, pages and optional sha256")
        source, revision, pages = (datasheet.get(k) for k in ("source", "revision", "pages"))
        if not isinstance(source, str) or not source.strip() or not isinstance(revision, str) or not revision.strip():
            raise ValueError("datasheet needs a source URL or file path and revision")
        if not isinstance(pages, list) or not pages or any(type(v) is not int or v < 1 for v in pages):
            raise ValueError("datasheet pages must be positive PDF page numbers (1-based)")
        evidence = {"source": source, "revision": revision, "pages": sorted(set(pages))}
        digest = datasheet.get("sha256")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            raise ValueError("datasheet sha256 must be 64 hexadecimal characters")
        if urlparse(source).scheme not in {"https", "http"}:
            path = Path(source).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"datasheet file not found: {path}")
            with path.open("rb") as f:
                actual = hashlib.file_digest(f, "sha256").hexdigest()
            if digest and digest.lower() != actual:
                raise ValueError("datasheet sha256 does not match the local file")
            digest = actual
            evidence["source"] = str(path)
        if digest:
            evidence["sha256"] = digest.lower()
        part["metadata"]["datasheet"] = evidence


def validate_pad_coverage(package: dict, connects: dict[str, str]) -> None:
    """Check the complete footprint against the library mapping, independent of design nets."""
    pads = {p["name"] for p in package.get("pads", []) + package.get("smds", [])}
    mechanical = set(package.get("mechanical_pads", []))
    if unknown := mechanical - pads:
        raise ValueError(f"Mechanical pads {sorted(unknown)} are not in package {package['name']}")
    owners = {}
    for pin, names in connects.items():
        pin_pads = names.split()
        if not pin_pads:
            raise ValueError(f"Symbol pin {pin} needs a package pad")
        if len(pin_pads) != len(set(pin_pads)):
            raise ValueError(f"Symbol pin {pin} repeats a package pad")
        for pad in pin_pads:
            if pad in owners:
                raise ValueError(f"Pad {pad} is mapped to multiple symbol pins: {owners[pad]}, {pin}")
            owners[pad] = pin
    mapped = set(owners)
    if unknown := mapped - pads:
        raise ValueError(f"pads {sorted(unknown)} are not in package {package['name']}")
    if package_only := mapped & mechanical:
        raise ValueError(f"Mechanical pads {sorted(package_only)} must stay package-only")
    if missing := pads - mechanical - mapped:
        raise ValueError(f"Unmapped electrical pads in {package['name']}: {sorted(missing)}")


def validate_part_connections(part: dict) -> None:
    validate_pad_names(part["package"])
    connects = {}
    for gate, symbol in symbol_units(part):
        for pin in symbol["pins"]:
            name, pads = f"{gate}.{pin['name']}", str(pin["pad"])
            if pin.get("direction") == "nc" and len(pads.split()) > 1:
                raise ValueError(f"{name}: NC pads require separate symbol pins")
            if name.upper() in connects:
                raise ValueError(f"Duplicate symbol pin {name}")
            connects[name.upper()] = pads
    validate_pad_coverage(part["package"], connects)


def symbol_units(part: dict) -> list[tuple[str, dict]]:
    """Keep the legacy flat pin list, with optional unit names on each pin."""
    sym = part["symbol"]
    units = list(dict.fromkeys(p.get("unit", "G$1") for p in sym["pins"]))
    if units == ["G$1"]:
        return [("G$1", sym)]
    return [(unit, {**sym, "name": sym["name"] + "_" + unit, "style": False,
                    "pins": [p for p in sym["pins"] if p.get("unit", "G$1") == unit]}) for unit in units]


def build_script(part: Part, reuse_package: bool = False) -> str:
    """reuse_package: the library already has this package (same pads), so only the symbol and
    device are drawn and the device uses the existing package."""
    d = part.data
    pkg = d["package"]
    validate_part_connections(d)
    L = ["GRID MM;"]
    if not reuse_package:
        L += _package_commands(pkg)
    units = symbol_units(d)
    for gate, unit in units:
        _symbol_commands(L, d, unit)
    return _device_part(L, d, units, pkg, q)


def _package_commands(pkg: dict) -> list:
    L = [f"EDIT {q(pkg['name'] + '.pac')};", "LAYER 1;"]
    for s in pkg.get("smds", []):
        L.append(f"SMD {n(s['dx'])} {n(s['dy'])} -{int(s.get('roundness', 0))} R{n(s.get('rot', 0))} "
                 f"{q(s['name'])} ({n(s['x'])} {n(s['y'])});")
    for p in pkg.get("pads", []):
        L.append(f"CHANGE DRILL {n(p['drill'])};")
        shape = {"square": "SQUARE", "long": "LONG", "octagon": "OCTAGON", "slot": "SLOT"}.get(p.get("shape"), "ROUND")
        L.append(f"PAD {n(p['diameter'])} {shape} R{n(p.get('rot', 0))} "
                 f"{q(p['name'])} ({n(p['x'])} {n(p['y'])});")
        if shape == "SLOT":
            # a new SLOT pad is 1.27 mm long; CHANGE SLOTLENGTH at the pad sets the drill slot's
            # length (verified on 2706.1.14; `CHANGE LENGTH` is symbol-editor only)
            L.append(f"CHANGE SLOTLENGTH {n(p['slot_length'])} ({n(p['x'])} {n(p['y'])});")
    for h in pkg.get("holes", []):
        L.append(f"HOLE {n(h['drill'])} ({n(h['x'])} {n(h['y'])});")
    if pkg.get("slots"):
        # unplated slots (a connector's locating slot): a closed outline on the Milling layer,
        # the way Groundplane's library draws them (fabs route layer 46 as a non-plated cut)
        L.append(f"LAYER {MILLING_LAYER};")
        for s in pkg["slots"]:
            L += [f"WIRE 0 {a} {b};" for a, b in _stadium(s)]
    if pkg.get("silk"):
        L.append("LAYER 21;")
        for x1, y1, x2, y2 in pkg["silk"]:
            L.append(f"WIRE 0.12 ({n(x1)} {n(y1)}) ({n(x2)} {n(y2)});")
    if pkg.get("silk_polys") or pkg.get("silk_circles"):
        L.append("LAYER 21;")
        # filled silk (KiCad's pin-1 triangle) becomes a filled dot at its centre: Fusion's
        # wire bend style squares off non-45-degree edges of both WIREs and POLYGONs
        # (a pin-1 triangle came out as a T on 2705.1.15); a CIRCLE cannot be bent
        for pts in pkg.get("silk_polys", []):
            cx = sum(x for x, _ in pts) / len(pts)
            cy = sum(y for _, y in pts) / len(pts)
            L.append(f"CIRCLE 0 ({n(cx)} {n(cy)}) ({n(cx + 0.2)} {n(cy)});")
        for c in pkg.get("silk_circles", []):
            w = 0 if c.get("filled") else 0.12      # CIRCLE width 0 draws a filled disc
            L.append(f"CIRCLE {n(w)} ({n(c['x'])} {n(c['y'])}) ({n(c['x'] + c['r'])} {n(c['y'])});")
    x0, top = _top_left(pkg)
    t = TEXT_STYLE
    L += [f"CHANGE FONT {t['font']};", f"CHANGE RATIO {t['ratio']};", f"CHANGE SIZE {n(t['size'])};",
          f"CHANGE ALIGN {t['align']};",
          "LAYER 25;", f"TEXT '>NAME' R0 ({n(x0)} {n(top + 2 * t['gap'] + t['size'])});",
          "LAYER 27;", f"TEXT '>VALUE' R0 ({n(x0)} {n(top + t['gap'])});"]
    if pkg.get("jlc_native"):
        L += [f"LAYER {MARKER_LAYER} JLC_FOOTPRINT;", "CHANGE SIZE 0.6;", "TEXT 'JLC' R0 (0 0);"]
    return L


def _top_left(pkg: dict) -> tuple[float, float]:
    """Leftmost x and topmost y of the package's copper, holes and silk."""
    xs, ys = [], []
    for s in pkg.get("smds", []):
        h = max(float(s["dx"]), float(s["dy"])) / 2
        xs.append(float(s["x"]) - h)
        ys.append(float(s["y"]) + h)
    for p in pkg.get("pads", []):
        h = (float(p["diameter"]) + float(p.get("slot_length", 0) or 0)) / 2
        xs.append(float(p["x"]) - h)
        ys.append(float(p["y"]) + h)
    for h in pkg.get("holes", []):
        xs.append(float(h["x"]) - float(h["drill"]) / 2)
        ys.append(float(h["y"]) + float(h["drill"]) / 2)
    for s in pkg.get("slots", []):
        xs.append(float(s["x"]) - float(s["length"]) / 2)
        ys.append(float(s["y"]) + float(s["length"]) / 2)
    for x1, y1, x2, y2 in pkg.get("silk", []):
        xs += [float(x1), float(x2)]
        ys += [float(y1), float(y2)]
    return (min(xs) if xs else 0.0), (max(ys) if ys else 0.0)


def _stadium(slot: dict) -> list[tuple[str, str]]:
    """The outline of a width x length slot (rounded ends) as four WIRE segments, each a pair of
    EAGLE points; the ends are 180-degree arcs (`-180`, clockwise)."""
    import math
    a = math.radians(float(slot.get("rot", 0)))
    ux, uy = math.cos(a), math.sin(a)                 # along the slot
    nx, ny = -uy, ux                                  # across it
    r = float(slot["width"]) / 2
    h = max(float(slot["length"]) / 2 - r, 0.0)
    cx, cy = float(slot["x"]), float(slot["y"])
    pt = lambda s, t: f"({n(cx + s * ux + t * nx)} {n(cy + s * uy + t * ny)})"
    p1, p2, p3, p4 = pt(h, r), pt(h, -r), pt(-h, -r), pt(-h, r)
    return [(p4, p1), (p1, f"-180 {p2}"), (p2, p3), (p3, f"-180 {p4}")]


def variant_script(part: Part, deviceset: str, variant: str, connects: dict[str, str],
                   technologies: dict[str, dict[str, str]], reuse_package: bool = False) -> str:
    """Add the part's package to an EXISTING device set as a new package variant: no symbol is
    drawn, the device set's own gate pins are connected (connects: 'G$1.PIN' -> 'pad pad ...'),
    and each technology gets exactly the given attributes. Verified on 2706.1.14: Fusion gives a
    variant made by PACKAGE an empty PACKAGE attribute in every technology; it is deleted unless
    asked for (ATTRIBUTE <name> DELETE)."""
    pkg = part.data["package"]
    validate_pad_names(pkg)
    validate_pad_coverage(pkg, connects)
    L = ["GRID MM;"]
    if not reuse_package:
        L += _package_commands(pkg)
    L += [f"EDIT {q(deviceset + '.dev')};", f"PACKAGE {q(pkg['name'])} {q(variant)};"]
    # ONE CONNECT per pin, all its pads in one string (a second CONNECT replaces the first)
    L += [f"CONNECT {q(pin)} {q(' '.join(str(pads).split()))};" for pin, pads in connects.items()]
    clean = lambda v: str(v).replace("'", "").replace(";", ",")
    for tech, attrs in technologies.items():
        L.append(f"TECHNOLOGY {q(tech) if tech else chr(39) * 2};")
        for k, v in attrs.items():
            L.append(f"ATTRIBUTE {k} {q(clean(v)) if v else chr(39) * 2};")
        if "PACKAGE" not in attrs:
            L.append("ATTRIBUTE PACKAGE DELETE;")
    return "\n".join(L) + "\n"


def deviceset_info(root, name: str) -> dict | None:
    """A device set from an exported library: gates and their symbol pins, and every package
    variant with its package, pin-pad connects and technologies (with attributes)."""
    up = lambda v: (v or "").upper()
    ds = next((d for d in root.iter("deviceset") if up(d.get("name")) == up(name)), None)
    if ds is None:
        return None
    syms = {s.get("name"): [p.get("name") for p in s.iter("pin")] for s in root.iter("symbol")}
    gates = [{"name": g.get("name"), "symbol": g.get("symbol"), "pins": syms.get(g.get("symbol"), [])}
             for g in ds.iter("gate")]
    variants = []
    for d in ds.iter("device"):
        variants.append({
            "variant": d.get("name") or "", "package": d.get("package"),
            "connects": {f"{c.get('gate')}.{c.get('pin')}": c.get("pad") for c in d.iter("connect")},
            "technologies": {t.get("name") or "": {a.get("name"): a.get("value") or "" for a in t.iter("attribute")}
                             for t in d.iter("technology")}})
    return {"deviceset": ds.get("name"), "prefix": ds.get("prefix"), "gates": gates, "variants": variants}


def _symbol_commands(L: list, d: dict, sym: dict) -> None:
    from . import std_symbols as SS
    # two-pin passives always get the library's standard symbol (std_symbols), whatever the
    # source drew; "style": false in the part keeps the part's own symbol
    style = sym.get("style")
    if style is None:
        style = SS.infer_style(d)
    if style:
        # a standard two-pin symbol: 7.62 mm, the anode / + pin on the left
        sym = {**sym, "style": style}
        lp, rp = SS.pin_order(sym["style"], sym["pins"])
        plen = SS.styles()[sym["style"]]["pin_length"]
        L += [f"EDIT {q(sym['name'] + '.sym')};",
              f"PIN {q(lp['name'])} {lp.get('direction', 'pas')} {plen} R0 (0 0);",
              f"PIN {q(rp['name'])} {rp.get('direction', 'pas')} {plen} R180 (7.62 0);",
              "CHANGE VISIBLE OFF (0 0);", "CHANGE VISIBLE OFF (7.62 0);"] + SS.art_commands(sym["style"])
        return
    from fusion_offline.symbols import box_pins
    hh, placed = box_pins(sym)
    L += [f"EDIT {q(sym['name'] + '.sym')};", "LAYER 94;",
          f"WIRE 0.254 (-5.08 {n(-hh)}) (5.08 {n(-hh)}) (5.08 {n(hh)}) (-5.08 {n(hh)}) (-5.08 {n(-hh)});"]
    for p, x, y, rot in placed:
        L.append(f"PIN {q(p['name'])} {p.get('direction', 'pas')} short R{rot} ({n(x)} {n(y)});")
    L += ["CHANGE FONT PROPORTIONAL;", "CHANGE SIZE 1.778;", "LAYER 95;", f"TEXT '>NAME' R0 (-5.08 {n(hh + 0.5)});",
          "LAYER 96;", f"TEXT '>VALUE' R0 (-5.08 {n(-hh - 2.3)});"]


def _device_part(L: list, d: dict, units: list, pkg: dict, q) -> str:
    L += [f"EDIT {q(d['deviceset'] + '.dev')};", f"PREFIX {q(d['prefix'])};",
          f"VALUE {'ON' if d.get('user_value') else 'OFF'};"]
    L += [f"ADD {q(sym['name'])} {q(gate)} NEXT 0 ({i * 25.4:g} 0);" for i, (gate, sym) in enumerate(units)]
    L.append(f"PACKAGE {q(pkg['name'])} '{d.get('device', '')}';")
    # ONE CONNECT per pin, all its pads in one space-separated string. Verified
    # on 2705.1.15: a second CONNECT for the same pin REPLACES the first, so
    # per-pad lines silently leave only the last pad connected.
    L += [f"CONNECT {q(gate + '.' + p['name'])} {q(' '.join(str(p['pad']).split()))};"
          for gate, sym in units for p in sym["pins"]]
    L.append("TECHNOLOGY '';")
    clean = lambda v: str(v).replace("'", "").replace(";", ",")
    for k, v in d.get("attributes", {}).items():
        L.append(f"ATTRIBUTE {k} {q(clean(v)) if v else chr(39) * 2};")
    meta = d.get("metadata", {})
    for k in ("source", "maintainer", "link"):
        if meta.get(k):
            L.append(f"ATTRIBUTE LIB_{k.upper()} {q(clean(meta[k]))};")
    for key, value in meta.get("datasheet", {}).items():
        text = ",".join(map(str, value)) if isinstance(value, list) else str(value)
        L.append(f"ATTRIBUTE DATASHEET_{key.upper()} {q(clean(text))};")
    return "\n".join(L) + "\n"
