"""Fusion (EAGLE V2) layer stackup and design rules.

The classic EAGLE XML export drops the V2 stackup, but Fusion keeps it in
three places, all with the same <layerstackup> element:
- .estackup files (exported stackup),
- .edru design-rule files (rules + stackup),
- the cloud-format board working copy Fusion writes while a design is open.

A stackup lists layers top to bottom: silk, mask, finish, then alternating
Signal / Prepreg / Core layers, then the bottom finish, mask and silk.
Dielectrics carry thickness and dielectric_constant_1g (Er at 1 GHz).
Signal layers use Fusion's cloud numbering (1, 2, 303, 304 for 4 layers);
`copper` lists them in stack order so callers can map classic export layer
numbers (1, 2, 15, 16) by position.
"""

from __future__ import annotations

import re
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


def _mm(v: str | None) -> float | None:
    if not v:
        return None
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*([a-z]*)", v)
    if not m:
        return None
    x, unit = float(m.group(1)), m.group(2)
    return {"mm": x, "": x, "mil": x * 0.0254, "mic": x / 1000, "um": x / 1000, "in": x * 25.4}.get(unit, x)


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


@dataclass
class Layer:
    kind: str                 # Signal, Prepreg, Core, Solder Mask, ...
    name: str
    number: int | None
    thickness_mm: float | None
    er: float | None = None
    df: float | None = None
    material: str = ""


@dataclass
class Stackup:
    name: str
    layers: list[Layer] = field(default_factory=list)
    source: str = ""

    @property
    def copper(self) -> list[Layer]:
        return [l for l in self.layers if l.kind == "Signal"]

    def neighbours(self, copper_index: int) -> tuple[Layer | None, Layer | None]:
        """Dielectric directly above and below the copper layer at this stack position."""
        sig = self.copper[copper_index]
        i = self.layers.index(sig)
        up = next((l for l in reversed(self.layers[:i]) if l.kind in ("Prepreg", "Core")), None)
        down = next((l for l in self.layers[i + 1:] if l.kind in ("Prepreg", "Core")), None)
        if up is not None and self.layers.index(up) < self.layers.index(self.copper[0]):
            up = None
        if down is not None and self.layers.index(down) > self.layers.index(self.copper[-1]):
            down = None
        return up, down

    def total_mm(self) -> float:
        return round(sum(l.thickness_mm or 0 for l in self.layers
                         if l.kind in ("Signal", "Prepreg", "Core")), 4)

    def as_dict(self) -> dict:
        return {"name": self.name, "source": self.source, "board_thickness_mm": self.total_mm(),
                "layers": [{"kind": l.kind, "name": l.name, "number": l.number,
                            "thickness_mm": l.thickness_mm, "er": l.er, "df": l.df, "material": l.material}
                           for l in self.layers if l.kind in ("Signal", "Prepreg", "Core", "Solder Mask")]}


def parse_stackup(xml: bytes | str | ET.Element, source: str = "") -> Stackup | None:
    root = xml if isinstance(xml, ET.Element) else ET.fromstring(xml)
    el = root if root.tag == "layerstackup" else root.find(".//layerstackup")
    if el is None:
        return None
    st = Stackup(el.get("name", ""), source=source)
    for ld in el.iterfind("layerdef"):
        m = ld.find("material")
        m = m if m is not None else ET.Element("material")
        kind = ld.get("type", "")
        num = ld.get("layer")
        st.layers.append(Layer(
            kind=kind, name=ld.get("name", ""),
            number=int(num) if (num and kind == "Signal") else None,
            thickness_mm=_mm(m.get("thickness")),
            er=_float(m.get("dielectric_constant_1g") or m.get("dielectric_constant")),
            df=_float(m.get("dissipation_factor_1g") or m.get("dissipation_factor")),
            material=" ".join(x for x in (m.get("oem"), m.get("oem_material"), m.get("description")) if x)))
    return st


def validate_stackup(xml: bytes | str | ET.Element) -> dict:
    """Physical import/read-back profile; this workflow supports through vias only."""
    root = xml if isinstance(xml, ET.Element) else ET.fromstring(xml)
    el = root if root.tag == 'layerstackup' else root.find('.//layerstackup')
    st = parse_stackup(root)
    if st is None or not 2 <= len(st.copper) <= 64 or len(st.copper) % 2:
        raise ValueError('Stackup requires an even count of 2..64 copper layers')
    numbers = [c.number for c in st.copper]
    if any(type(n) is not int or not 1 <= n <= 304 for n in numbers) or numbers != sorted(set(numbers)):
        raise ValueError('Copper layer IDs must be unique and ordered top to bottom')
    defs = list(el.findall('layerdef'))
    first = next(i for i, d in enumerate(defs) if d.get('type') == 'Signal')
    last = max(i for i, d in enumerate(defs) if d.get('type') == 'Signal')
    physical = defs[first:last + 1]
    if len(physical) != 2 * len(numbers) - 1 or any(
            d.get('type') not in (('Signal',) if i % 2 == 0 else ('Prepreg', 'Core'))
            for i, d in enumerate(physical)):
        raise ValueError('Stackup must alternate copper and dielectric from top to bottom')
    layers = []
    for d in physical:
        m = d.find('material')
        if m is None or not m.get('name'):
            raise ValueError('Each physical layer needs a named material')
        raw = m.get('thickness', '')
        if not re.fullmatch(r'\s*\d+(?:\.\d+)?\s*(mm|mil|mic|um|in)\s*', raw):
            raise ValueError('Every thickness needs a finite positive value and explicit length unit')
        thickness = _mm(raw.strip())
        er = _float(m.get('dielectric_constant_1g') or m.get('dielectric_constant'))
        df = _float(m.get('dissipation_factor_1g') or m.get('dissipation_factor'))
        if not math.isfinite(thickness) or thickness <= 0:
            raise ValueError('Layer thickness must be finite and positive')
        if d.get('type') != 'Signal' and (er is None or not math.isfinite(er) or er < 1):
            raise ValueError('Each dielectric needs a finite dielectric constant (Er) of at least 1')
        if any(k in m.attrib for k in ('dissipation_factor_1g', 'dissipation_factor')) and \
                (df is None or not math.isfinite(df) or df < 0):
            raise ValueError('Dissipation factor must be finite and nonnegative')
        layers.append(dict(kind=d.get('type'), number=int(d.get('layer')) if d.get('type') == 'Signal' else None,
                           thickness_mm=thickness, er=er, df=df,
                           material={k: m.get(k, '') for k in ('name', 'oem', 'oem_material')}))
    vias = []
    for v in el.findall('viadef'):
        span = [int(v.get('startlayer', '0')), int(v.get('stoplayer', '0'))]
        if set(v.attrib) - {'name', 'id', 'startlayer', 'stoplayer', 'complement'} or \
                span != [numbers[0], numbers[-1]] or v.get('complement', 'no') != 'no':
            raise ValueError('Stack preparation supports through vias only; blind/buried/micro vias need a separate supported process')
        vias.append(span)
    if not vias:
        raise ValueError('Stackup needs an explicit through-via definition')
    return {'copper_layers': numbers, 'layers': layers, 'via_spans': sorted(vias),
            'board_thickness_mm': round(sum(l['thickness_mm'] for l in layers), 6)}


def validate_layer_roles(roles: list[dict], count: int) -> None:
    """References are one-based physical copper positions, not native/export layer IDs."""
    if not isinstance(roles, list) or len(roles) != count:
        raise ValueError('Provide one layer role for every copper layer in top-to-bottom order')
    for i, role in enumerate(roles, 1):
        if not isinstance(role, dict) or role.get('role') not in ('signal', 'ground', 'power', 'mixed'):
            raise ValueError('Layer role must be signal, ground, power or mixed')
        if set(role) - {'role', 'net', 'references'}:
            raise ValueError('Unknown layer-role field')
        if role['role'] in ('ground', 'power') and not isinstance(role.get('net'), str):
            raise ValueError('Ground/power planes need an explicit net')
        if 'net' in role and (not isinstance(role['net'], str) or not role['net'].strip()):
            raise ValueError('Plane net must be nonempty')
        refs = role.get('references', [])
        if not isinstance(refs, list) or any(type(r) is not int or not 1 <= r <= count or abs(r-i) != 1 for r in refs):
            raise ValueError('Reference layers must be adjacent physical copper positions')
        if any(not isinstance(roles[r-1], dict) or roles[r-1].get('role') not in ('ground', 'power') for r in refs):
            raise ValueError('Reference layers must designate ground/power planes')


@dataclass
class ClearanceRule:
    name: str
    value_mm: float
    one: str
    other: str
    same_signal: bool
    enabled: bool


def parse_clearance_rules(xml: bytes | str | ET.Element) -> tuple[list[ClearanceRule], dict[str, str]]:
    """V2 copper clearance rules and the class-number -> name map, from an
    .edru or a cloud-format board."""
    root = xml if isinstance(xml, ET.Element) else ET.fromstring(xml)
    classes = {c.get("number", ""): c.get("name", "") for c in root.iter("class")}
    rules = []
    for r in root.iter("rule"):
        if r.get("type") != "Copper Clearance":
            continue
        rules.append(ClearanceRule(r.get("name", ""), _mm(r.get("value")) or 0.0, r.get("onescope", "all"),
                                   r.get("otherscope", "all"), r.get("samesignal") == "yes",
                                   r.get("enabled", "yes") == "yes"))
    return rules, classes


def class_clearance(rules: list[ClearanceRule], class_number: str) -> list[ClearanceRule]:
    """Enabled rules whose scope names this net class (onescope='classes=4')."""
    out = []
    for r in rules:
        if not r.enabled:
            continue
        for scope in (r.one, r.other):
            m = re.match(r"classes=([\d,]+)", scope or "")
            if m and class_number in m.group(1).split(","):
                out.append(r)
                break
    return out
