"""Build a flat EAGLE schematic from pinned assets and explicit placements."""
from copy import deepcopy
import xml.etree.ElementTree as ET

from .. import sch_wiring
from ..design import parse_schematic_design
from .canonical import eagle_xml_bytes
from .libraries import ResolvedLibraries
from .model import DesignSpec


def apply_net_classes(document: ET.Element, rows) -> dict[str, str]:
    """Overlay spec classes by number and return exact name-to-number mappings."""
    classes = document.find("classes")
    if classes is None:
        raise ValueError("EAGLE template must contain classes")
    nodes = {}
    for node in classes.findall("class"):
        number = int(node.get("number", "-1"))
        if number in nodes or not 0 <= number <= 15:
            raise ValueError(f"invalid or duplicate template net class number {number}")
        nodes[number] = node
    for row in rows:
        nodes[row["number"]] = ET.Element("class", number=str(row["number"]),
                                           name=row["name"], width=str(row["width"]),
                                           drill=str(row["drill"]))

    numbers = {}
    folded = set()
    for number, node in sorted(nodes.items()):
        name = node.get("name", "")
        if not name or name.casefold() in folded:
            raise ValueError(f"invalid or duplicate net class name {name!r}")
        folded.add(name.casefold())
        numbers[name] = str(number)
    for row in rows:
        for name, value in row.get("clearances", {}).items():
            if name not in numbers:
                raise ValueError(f"unknown clearance net class {name!r}")
            ET.SubElement(nodes[row["number"]], "clearance", **{"class": numbers[name], "value": str(value)})
    for node in classes.findall("class"):
        classes.remove(node)
    classes.extend(node for _, node in sorted(nodes.items()))
    return numbers


def _prepare_sheets(document, declarations):
    if [sheet["number"] for sheet in declarations] != list(range(1, len(declarations) + 1)):
        raise ValueError("schematic sheet numbers must be contiguous starting at 1")
    container = document.find("sheets")
    if container is None:
        container = ET.SubElement(document, "sheets")
    templates = container.findall("sheet")
    for sheet in templates[len(declarations):]:
        if any(len(child) or (child.text or "").strip() for child in sheet):
            raise ValueError("empty schematic template has content on an undeclared sheet")
        container.remove(sheet)

    sheets = {}
    for row in declarations:
        number = row["number"]
        sheet = templates[number - 1] if number <= len(templates) else ET.SubElement(container, "sheet")
        description = sheet.find("description")
        if description is not None:
            sheet.remove(description)
        if "description" in row:
            description = ET.Element("description")
            description.text = row["description"]
            sheet.insert(0, description)
        for index, tag in enumerate(("plain", "instances", "busses", "nets"), start="description" in row):
            if sheet.find(tag) is None:
                sheet.insert(index, ET.Element(tag))
        sheets[number] = sheet
    return sheets


def build_schematic(spec: DesignSpec, template_xml: bytes, libraries: ResolvedLibraries) -> bytes:
    """Emit the declared units and gate-qualified nets as standalone EAGLE XML."""
    root = ET.fromstring(template_xml)
    schematics = root.findall("./drawing/schematic")
    if root.tag != "eagle" or len(schematics) != 1:
        raise ValueError("expected one EAGLE schematic template")
    document = schematics[0]
    if document.find("modules/*") is not None or document.find(".//moduleinst") is not None:
        raise ValueError("schematic template must be flat")
    for modules in document.findall("modules"):
        document.remove(modules)
    if any(document.find(path) is not None for path in (
        "./parts/*", "./sheets/sheet/instances/*",
        "./sheets/sheet/busses/*", "./sheets/sheet/nets/*",
    )):
        raise ValueError("schematic template must be empty")

    embedded = document.find("libraries")
    parts = document.find("parts")
    if embedded is None or parts is None:
        raise ValueError("EAGLE schematic template must contain libraries and parts")
    for library in embedded.findall("library"):
        embedded.remove(library)
    embedded.extend(deepcopy(library) for library in libraries.libraries.values())
    classes = apply_net_classes(document, spec.board["net_classes"])
    sheets = _prepare_sheets(document, spec.schematic["sheets"])
    for row in spec.parts:
        ref = row["ref"]
        resolved = libraries.parts[ref]
        part = ET.SubElement(parts, "part", name=ref, library=resolved.library,
                             deviceset=resolved.deviceset, device=resolved.device,
                             technology=resolved.technology, value=row["value"])
        for name, value in row["attributes"].items():
            ET.SubElement(part, "attribute", name=name, value=value)
        for unit in row["units"]:
            ET.SubElement(sheets[unit["sheet"]].find("instances"), "instance",
                          part=ref, gate=unit["gate"], x=str(unit["x"]), y=str(unit["y"]),
                          rot=f'R{unit["rotation"]:g}')

    pins = {(pin.part, pin.gate, pin.pin): pin for pin in parse_schematic_design(root).pins}
    # Place requested label stubs first so wired routes see their geometry.
    for net in sorted(spec.nets, key=lambda row: row["schematic_style"]):
        name, class_name = net["name"], net["class"]
        if class_name not in classes:
            raise ValueError(f"{name}: unknown net class {class_name!r}")
        targets = [pins[pin["part"], pin["gate"], pin["pin"]] for pin in net["pins"]]
        if net["schematic_style"] == "labels":
            for target in targets:
                wiring = sch_wiring.plan(root, name, [target], style="labels")
                sch_wiring.simulate(root, name, [target], wiring)
        else:
            wiring = sch_wiring.plan(root, name, targets, labels=False)
            sch_wiring.simulate(root, name, targets, wiring)
        for sheet in sorted({pin.sheet for pin in targets}):
            node = sheets[sheet].find(f"./nets/net[@name='{name}']")
            node.set("class", classes[class_name])
    return eagle_xml_bytes(root, template_xml)
