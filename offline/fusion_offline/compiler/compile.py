"""Compile and publish deterministic offline EAGLE candidates."""
from collections import Counter
import json
import xml.etree.ElementTree as ET

from .board import build_board
from .board_svg import render_board_svg
from .canonical import canonical_json_bytes
from .libraries import resolve_libraries
from .manifest import CandidateManifest, _identity, publish_candidate
from .model import ROUTING_BUDGET_DEFAULTS, load_design_spec
from .render import review_artifacts
from .schematic import build_schematic
from .validate import validate_candidate


def _routing_intent(spec):
    overrides = {row["ref"]: row for row in spec.board["placements"]}
    allow_outside = [part["ref"] for part in spec.parts
                     if (overrides.get(part["ref"], part["board"]) or {}).get("allow_outside")]
    return {
        "allow_outside": sorted(allow_outside),
        "nets": {net["name"]: net["route"] for net in spec.nets},
        "routing": {
            "require_fully_routed": False,
            **spec.routing,
            "budget": {**ROUTING_BUDGET_DEFAULTS, **spec.routing["budget"]},
        },
    }


def _publish(output_directory, inputs, schematic, board, routing_intent, routing_job=None, *,
             provenance=None, board_svg=None, routing_artifacts=None):
    intent = json.loads(routing_intent)
    report = validate_candidate(schematic, board, intent, provenance=provenance)
    artifacts = {
        "schematic": schematic, "board": board, "routing_intent": routing_intent,
        "report": canonical_json_bytes(report), "routing_diagnostics": b"{}",
    }
    if provenance is not None:
        artifacts["routing_provenance"] = canonical_json_bytes(provenance)
    artifacts.update(routing_artifacts or {})
    artifacts["board_svg"] = (render_board_svg(board, intent["routing"], {}, {})
                              if board_svg is None else board_svg)
    artifacts.update(review_artifacts(_identity(inputs, routing_job), inputs, schematic, board, intent,
                                      report, artifacts))
    return publish_candidate(output_directory, inputs=inputs, report=report,
                             routing_job=routing_job, artifacts=artifacts)


def compile_candidate(spec_path, expected_spec_sha256, output_directory) -> CandidateManifest:
    """Compile pinned local assets and publish a validated offline candidate."""
    spec = load_design_spec(spec_path, expected_spec_sha256)
    libraries = resolve_libraries(spec)
    schematic = build_schematic(spec, spec.templates["schematic"].data, libraries)
    board = build_board(spec, spec.templates["board"].data, libraries, schematic)
    inputs = {
        "spec_version": spec.version, "spec_sha256": expected_spec_sha256.lower(),
        "template_schematic_sha256": spec.templates["schematic"].sha256,
        "template_board_sha256": spec.templates["board"].sha256,
        "libraries": [{"name": asset.name, "sha256": asset.sha256} for asset in spec.libraries],
    }
    return _publish(output_directory, inputs, schematic, board, canonical_json_bytes(_routing_intent(spec)))


def _structure(node):
    return (node.tag, tuple(sorted(node.attrib.items())), node.text if (node.text or "").strip() else "",
            tuple(_structure(child) for child in node))


def _compare_routed_board(original, routed, selected_nets):
    roots = ET.fromstring(original), ET.fromstring(routed)
    copper = []
    for root in roots:
        signal_copper = {}
        for signal in root.findall("./drawing/board/signals/signal"):
            name = signal.get("name")
            if name not in selected_nets:
                continue
            geometry = [node for node in signal if node.tag in ("wire", "via") and node.get("layer") != "19"]
            signal_copper[name] = Counter(_structure(node) for node in geometry)
            for node in geometry:
                signal.remove(node)
        copper.append(signal_copper)
    if _structure(roots[0]) != _structure(roots[1]):
        raise ValueError("routing changed protected copper or non-routing board geometry")
    if any(before - copper[1][name] for name, before in copper[0].items()):
        raise ValueError("routing removed or changed pre-existing copper")
