"""Deterministic review files for a saved compiler candidate."""
from collections import defaultdict
from html import escape
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

from ..design import parse_board_design, parse_schematic_design, transform
from ..sch_layout import Drawing
from ..sch_render import render_svg
from ..symbols import Geo, symbol_pins, symbol_prims
from .canonical import canonical_json_bytes
from .manifest import MANIFEST_FILENAME, _artifact_filename, load_candidate_manifest
from .paths import _check_parents, _local_path


def _schematic_sheets(schematic_xml):
    root = ET.fromstring(schematic_xml)
    schematic = parse_schematic_design(root)
    document = root.find("./drawing/schematic")
    libraries = {lib.attrib["name"]: lib for lib in document.findall("./libraries/library")}
    units = defaultdict(list)
    geometry = {}
    for instance in schematic.instances:
        units[instance.sheet].append(instance)
    for number, sheet in enumerate(document.findall("./sheets/sheet"), 1):
        drawing, symbols, values = Drawing(), {}, {}
        for unit in sorted(units[number], key=lambda row: (row.part, row.gate)):
            part = schematic.parts[unit.part]
            key = (part.library, part.deviceset, unit.gate)
            if key not in geometry:
                lib = libraries[part.library]
                device = next(n for n in lib.findall("./devicesets/deviceset") if n.get("name") == part.deviceset)
                gate = next(n for n in device.findall("./gates/gate") if n.get("name") == unit.gate)
                symbol = next(n for n in lib.findall("./symbols/symbol") if n.get("name") == gate.get("symbol"))
                geometry[key] = Geo(symbol.attrib["name"], symbol_pins(symbol), {}, symbol_prims(symbol))
            symbol = geometry[key]
            ref = f"{unit.part}.{unit.gate}"
            position = unit.x, unit.y, unit.angle, unit.mirror
            drawing.parts[ref] = position
            symbols[ref], values[ref] = symbol, part.value
            x0, y0, x1, y1 = symbol.body()
            xs, ys = zip(*(transform(x, y, *position) for x in (x0, x1) for y in (y0, y1)))
            drawing.boxes.append((min(xs), min(ys), max(xs), max(ys), ref))
        for net in sorted(sheet.findall("./nets/net"), key=lambda node: node.attrib["name"]):
            name = net.attrib["name"]
            for wire in net.iter("wire"):
                drawing.wires.append((name, [tuple(float(wire.attrib[key]) for key in pair)
                                             for pair in (("x1", "y1"), ("x2", "y2"))]))
            for label in net.iter("label"):
                drawing.labels.append((name, float(label.attrib["x"]), float(label.attrib["y"]), 1))
        junctions = sorted({(float(node.attrib["x"]), float(node.attrib["y"]))
                            for node in sheet.findall("./nets/net/segment/junction")})
        description = sheet.findtext("description") or ""
        title = f"Sheet {number}" + (f" · {description}" if description else "")
        svg = render_svg(drawing, symbols, values, {}, title=title, junctions=junctions)
        yield f"schematic_sheet_{number}", title, (svg + "\n").encode()


def _layer_violations(board, policy):
    stack = set(parse_board_design(board).copper_layers)
    violations = []
    for signal in board.findall("./drawing/board/signals/signal"):
        net = signal.attrib["name"]
        allowed = set(policy["policies"].get(net, {}).get("allowed_layers", stack))
        if allowed - stack:
            violations.append({"net": net, "geometry": "policy", "layers": sorted(allowed - stack)})
        for node in signal:
            if node.tag in ("wire", "polygon") and node.attrib["layer"] != "19":
                layers = {int(node.attrib["layer"])}
            elif node.tag == "via":
                first, last = map(int, node.attrib["extent"].split("-"))
                layers = {layer for layer in stack if first <= layer <= last} | {first, last}
            else:
                continue
            disallowed = layers - (allowed & stack)
            if disallowed:
                violations.append({"net": net, "geometry": node.tag, "layers": sorted(disallowed)})
    return sorted(violations, key=lambda row: (row["net"], row["geometry"], row["layers"]))


def _review_html(summary, sheets, routed):
    title = "Compiler candidate review"
    html = [
        '<!doctype html><html lang="en"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{title}</title>",
        "<style>body{margin:2rem auto;padding:0 1rem;max-width:78rem;font:1rem system-ui;color:#172331}"
        "img{display:block;max-width:100%;border:1px solid #c7d1da}code{overflow-wrap:anywhere}"
        "li{margin:.3rem 0}summary{cursor:pointer}</style><body>",
        f"<h1>{title}</h1><p>Candidate <code>{summary['candidate_id']}</code></p>",
        f"<p>Compilation complete. Validation <strong>{summary['validation_status']}</strong>. "
        f"Offline import eligibility <strong>{'eligible' if summary['import_eligible'] else 'blocked'}</strong>.</p>",
        "<p>Native Fusion ERC/DRC has not yet run.</p>",
        "<h2>Routing</h2>",
    ]
    counts = summary["routing_counts"]
    budget = summary["routing_budget_consumed"]
    job_counts = f"{counts['routed']} routed and {counts['failed']} failed in this job." if routed else "No routing job."
    html.extend((
        f"<p>{job_counts} {counts['unrouted']} unjoined contact pairs on the saved board.</p>",
        f"<p>Search consumed {budget['attempts']} attempts, {budget['expanded_nodes']} expanded nodes, "
        f"{budget['ripups']} rip-ups and {budget['vias_used']} vias on completed routes.</p>",
        "<h3>Saved vias by net</h3>",
        "<ul>" + "".join(f"<li>{escape(net)} · {count}</li>" for net, count in summary["vias_by_net"].items())
        + "</ul>" if summary["vias_by_net"] else "<p>No saved vias.</p>",
        "<h3>Allowed-layer violations</h3>",
    ))
    violations = summary["allowed_layer_violations"]
    html.append("<ul>" + "".join(
        f"<li>{escape(row['net'])} · {row['geometry']} · {', '.join(map(str, row['layers']))}</li>"
        for row in violations
    ) + "</ul>" if violations else "<p>None found.</p>")
    required = summary["required_unverified_findings"]
    html.append("<h3>Required unverified findings</h3>")
    html.append("<ul>" + "".join(
        f"<li>{escape(row['check'])} · {escape(row['detail'])}</li>" for row in required
    ) + "</ul>" if required else "<p>None reported.</p>")
    html.append("<h2>Board</h2>")
    html.append(f'<img alt="Board geometry and routing diagnostics" src="{_artifact_filename("board_svg")}">')
    html.append("<h2>Schematic sheets</h2>")
    for key, label, _ in sheets:
        html.append(f'<h3>{escape(label)}</h3><img alt="{escape(label)}" src="{_artifact_filename(key)}">')
    html.append("<h2>Sources and reports</h2><ul>")
    inputs = summary["inputs"]
    for label, pin in (("Specification", inputs["spec_sha256"]),
                       ("Schematic template", inputs["template_schematic_sha256"]),
                       ("Board template", inputs["template_board_sha256"])):
        html.append(f"<li>{label} SHA-256 <code>{pin}</code></li>")
    for lib in inputs["libraries"]:
        html.append(f"<li>{escape(lib['name'])} library SHA-256 <code>{lib['sha256']}</code></li>")
    html.append("</ul><ul>")
    links = ["report", "summary", "routing_diagnostics", "schematic", "board"]
    if routed:
        links += ["routing_request", "routing_routes", "routing_provenance"]
    for filename in [MANIFEST_FILENAME, *map(_artifact_filename, links)]:
        html.append(f'<li><a href="{filename}">{filename}</a></li>')
    html.append("</ul></body></html>")
    return ("\n".join(html) + "\n").encode()


def review_artifacts(candidate_id, inputs, schematic, board, intent, report, artifacts):
    """Build review bytes using the same pinned routing inputs as the candidate."""
    routing = intent["routing"]
    request = json.loads(artifacts.get("routing_request", b"{}"))
    routes = json.loads(artifacts.get("routing_routes", b"{}"))
    diagnostics = json.loads(artifacts["routing_diagnostics"])
    root = ET.fromstring(board)
    signals = sorted(root.findall("./drawing/board/signals/signal"), key=lambda node: node.attrib["name"])
    sheets = list(_schematic_sheets(schematic))
    summary = {
        "candidate_id": candidate_id,
        "inputs": inputs,
        "compile_status": "complete",
        "validation_status": report["status"],
        "import_eligible": report["status"] == "pass",
        "routing_counts": {
            "routed": len(routes.get("routes", {})),
            "failed": len(routes.get("failed", {})),
            "unrouted": report["airwires"],
        },
        "routing_budget_consumed": {
            "attempts": sum(row["attempts"] for row in diagnostics.values()),
            "expanded_nodes": sum(row["expanded_nodes"] for row in diagnostics.values()),
            "ripups": routes.get("rips", 0),
            "vias_used": sum(row["vias_used"] or 0 for row in diagnostics.values()),
        },
        "vias_by_net": {signal.attrib["name"]: len(vias) for signal in signals if (vias := signal.findall("via"))},
        "allowed_layer_violations": _layer_violations(root, routing),
        "required_unverified_findings": [row for row in report["findings"]
                                         if row["required"] and row["status"] == "unverified"],
    }
    return {
        "preview": _review_html(summary, sheets, bool(request)),
        "summary": canonical_json_bytes(summary),
        **{key: data for key, _, data in sheets},
    }


def render_candidate(manifest_path, expected_sha256):
    """Read the published review and verify all artifact pins without rerendering."""
    return load_candidate_manifest(manifest_path, expected_sha256)


def render_candidate_png(manifest_path, expected_sha256, output_path):
    """Write an optional PNG beside other local output, outside the pinned candidate."""
    from ..render import render

    candidate = render_candidate(manifest_path, expected_sha256)
    target = _local_path(output_path)
    if target.suffix.lower() != ".png" or target.is_relative_to(candidate.path.parent):
        raise ValueError("PNG output must be outside the candidate directory and end in .png")
    _check_parents(target)
    if target.exists():
        raise FileExistsError(target)
    with TemporaryDirectory(dir=target.parent) as directory:
        preview = Path(directory) / "board.png"
        render(ET.fromstring(candidate.read_artifact("board")), str(preview), title="Candidate board")
        _check_parents(target)
        os.link(preview, target)
    return target
