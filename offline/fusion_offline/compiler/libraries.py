"""Resolve exact EAGLE device definitions and retain only their dependencies."""
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
import xml.etree.ElementTree as ET

from ..design import read_xml
from ..eagle import Pad, UnverifiedGeometry
from ..edit_plan import geometry_hash
from .model import DesignSpec


@dataclass(frozen=True)
class ResolvedGate:
    symbol: ET.Element
    pins: Mapping[str, ET.Element]


@dataclass(frozen=True)
class ResolvedPart:
    library: str
    deviceset: str
    device: str
    technology: str
    package: str | None
    gates: Mapping[str, ResolvedGate]
    pads: Mapping[tuple[str, str], tuple[str, ...]]
    attributes: Mapping[str, str]
    physical: bool


@dataclass(frozen=True)
class ResolvedLibraries:
    """Shared schematic/board mappings. Copy XML nodes before modifying them."""
    libraries: Mapping[str, ET.Element]
    parts: Mapping[str, ResolvedPart]
    source_hashes: Mapping[str, str]
    package_hashes: Mapping[tuple[str, str], str]
    findings: tuple[Mapping[str, str], ...]


def _named(nodes, field, context, *, unnamed=False):
    names = {}
    folded = set()
    for node in nodes:
        name = node.get("name", "")
        if not name and not unnamed:
            raise ValueError(f"{context}: missing {field} name")
        if name.casefold() in folded:
            raise ValueError(f"{context}: duplicate {field} name {name!r}")
        folded.add(name.casefold())
        names[name] = node
    return names


def _exact(names, name, field, context):
    if name not in names:
        raise ValueError(f"{context}: no exact {field} {name!r}")
    return names[name]


def _resolve_device(deviceset, device, symbols, packages, context):
    gates = {}
    for gate, node in _named(deviceset.findall("./gates/gate"), "gate", context).items():
        symbol = _exact(symbols, node.get("symbol"), "symbol", f"{context}.{gate}")
        pins = _named(symbol.findall("pin"), "pin", f"{context}.{gate}")
        gates[gate] = ResolvedGate(symbol, MappingProxyType(pins))

    package = None
    pad_names = {}
    if package_name := device.get("package"):
        package = _exact(packages, package_name, "package", context)
        pad_names = _named((node for node in package if node.tag in ("pad", "smd")),
                           "pad", f"{context}/{package_name}")

    pads = {}
    for connect in device.findall("./connects/connect"):
        gate, pin = connect.get("gate"), connect.get("pin")
        resolved_gate = _exact(gates, gate, "connect gate", context)
        _exact(resolved_gate.pins, pin, "connect pin", f"{context}.{gate}")
        if (gate, pin) in pads:
            raise ValueError(f"{context}: duplicate pad mapping for {gate}.{pin}")
        names = tuple(connect.get("pad", "").split())
        if not names or len(names) != len(set(names)):
            raise ValueError(f"{context}: missing or duplicate pad in mapping for {gate}.{pin}")
        for name in names:
            _exact(pad_names, name, "pad", f"{context}.{gate}.{pin}")
        pads[gate, pin] = names
    return package, MappingProxyType(gates), MappingProxyType(pads)


def _check_net_contacts(spec, parts):
    pad_nets = {}
    for net in spec.nets:
        contacts = set()
        for row in net["pins"]:
            ref, gate, pin = row["part"], row["gate"], row["pin"]
            part = parts[ref]
            _exact(part.gates[gate].pins, pin, "pin", f"{ref}.{gate}")
            if not part.physical:
                continue
            pads = _exact(part.pads, (gate, pin), "pad mapping", ref)
            contacts.update((ref, pad) for pad in pads)
        for contact in sorted(contacts):
            if pad_nets.setdefault(contact, net["name"]) != net["name"]:
                raise ValueError(f"physical pad {contact[0]}.{contact[1]} belongs to multiple nets")
        if net["route"] and len(contacts) < 2:
            raise ValueError(f"routed net {net['name']} needs at least two distinct physical pads")


def _minimize(parent, group, tag, selected):
    # Leave ordered geometry and other native XML children as they appeared in the source.
    container = parent.find(group)
    if container is not None:
        children = container.findall(tag)
        for child in children:
            container.remove(child)
        container.extend(sorted((node for node in children if node in selected),
                                key=lambda node: node.get("name", "")))


def resolve_libraries(spec: DesignSpec) -> ResolvedLibraries:
    """Use only definitions supplied by the spec's hash-pinned library sources."""
    parts, libraries, package_hashes, findings = {}, {}, {}, []
    physical = {row["ref"] for row in spec.board["placements"]}
    source_hashes = {asset.name: asset.sha256 for asset in spec.libraries}
    for asset in spec.libraries:
        root = read_xml(asset.data)
        nodes = root.findall("./drawing/library")
        if len(nodes) != 1 or root.tag != "eagle":
            raise ValueError(f"expected one EAGLE library in {asset.path}")
        library = nodes[0]
        if library.get("name") not in (None, "", asset.name):
            raise ValueError(f"{asset.path}: no exact library name {asset.name!r}")
        library.set("name", asset.name)

        rows = [part for part in spec.parts if part["library"] == asset.name]
        if not rows:
            continue
        devicesets = _named(library.findall("./devicesets/deviceset"), "deviceset", asset.name)
        packages = _named(library.findall("./packages/package"), "package", asset.name)
        symbols = _named(library.findall("./symbols/symbol"), "symbol", asset.name)
        selected_packages, selected_symbols, selected_devices = set(), set(), {}
        resolved_devices = {}
        for row in rows:
            ref = row["ref"]
            deviceset = _exact(devicesets, row["deviceset"], "deviceset", ref)
            devices = _named(deviceset.findall("./devices/device"), "device", ref, unnamed=True)
            device = _exact(devices, row["device"], "device", ref)
            technologies = _named(device.findall("./technologies/technology"), "technology", ref, unnamed=True)
            technology = _exact(technologies, row["technology"], "technology", ref)
            selected_devices.setdefault(deviceset, {}).setdefault(device, set()).add(technology)

            is_physical = row["board"] is not None or ref in physical
            has_package = bool(device.get("package"))
            if is_physical and not has_package:
                raise ValueError(f"{ref}: a physical device needs an exact package")
            if has_package and not is_physical:
                raise ValueError(f"{ref}: a packaged device needs a board placement for paired import")
            if device not in resolved_devices:
                resolved_devices[device] = _resolve_device(deviceset, device, symbols, packages, ref)
            package, gates, pads = resolved_devices[device]
            for unit in row["units"]:
                _exact(gates, unit["gate"], "gate", ref)

            selected_symbols.update(gate.symbol for gate in gates.values())
            if package is not None:
                selected_packages.add(package)
            attributes = _named(technology.findall("attribute"), "technology attribute", ref)
            parts[ref] = ResolvedPart(
                asset.name, row["deviceset"], row["device"], row["technology"],
                package.get("name") if package is not None else None, gates, pads,
                MappingProxyType({name: node.get("value", "") for name, node in attributes.items()}),
                is_physical,
            )

        for deviceset, devices in selected_devices.items():
            for device, technologies in devices.items():
                _minimize(device, "technologies", "technology", technologies)
            _minimize(deviceset, "devices", "device", devices)
        _minimize(library, "devicesets", "deviceset", selected_devices)
        _minimize(library, "symbols", "symbol", selected_symbols)
        _minimize(library, "packages", "package", selected_packages)
        libraries[asset.name] = library
        for package in library.findall("./packages/package"):
            name = package.get("name")
            package_hashes[asset.name, name] = geometry_hash(package)
            for pad in package.findall("pad"):
                try:
                    Pad.from_xml(pad).require_supported_geometry(f"{asset.name}/{name}")
                except UnverifiedGeometry as exc:
                    findings.append(MappingProxyType({
                        "rule": "slot_geometry", "status": "unverified", "library": asset.name,
                        "package": name, "pad": pad.get("name"), "detail": str(exc),
                    }))

    _check_net_contacts(spec, parts)
    return ResolvedLibraries(
        MappingProxyType(libraries), MappingProxyType(parts), MappingProxyType(source_hashes),
        MappingProxyType(package_hashes), tuple(findings),
    )
