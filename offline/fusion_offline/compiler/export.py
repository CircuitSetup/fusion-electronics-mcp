"""Publish both files of an offline Fusion Electronics edit together."""

from hashlib import sha256
import json
import xml.etree.ElementTree as ET

from fusion_mcp.addin.FusionElectronicsMCP.eagle_import import signature

from .canonical import canonical_json_bytes
from .model import _validate_source_xml
from .native import NATIVE_FORMATS, pack_fusion_document, read_fusion_xml
from .paths import _local_path, _read_local_file, atomic_directory, atomic_write
from .roundtrip import DRC_BASELINE_FILENAME, board_rule_snapshot, changed_board_rules


_DOCUMENT_LIMIT = 32 * 1024 * 1024


def _source_baseline(raw, xml, path):
    _validate_source_xml(xml, str(path))
    return {"source_sha256": sha256(raw).hexdigest(), "source_rules": board_rule_snapshot(ET.fromstring(xml))}


def capture_drc_baseline(source_board, output_directory):
    """Record the declared rules and exact file hash of a saved board before editing."""
    source = _local_path(source_board)
    suffix = source.suffix.lower()
    if suffix not in (".fbrd", ".brd"):
        raise ValueError("use a saved .fbrd or .brd board")
    raw = _read_local_file(source, _DOCUMENT_LIMIT)
    xml = read_fusion_xml(raw, "board") if suffix == ".fbrd" else raw
    baseline = _source_baseline(raw, xml, source)
    target = _local_path(output_directory)
    with atomic_directory(target) as staging:
        atomic_write(staging / DRC_BASELINE_FILENAME, canonical_json_bytes(baseline))
    return target / DRC_BASELINE_FILENAME


def export_edited_pair(source_schematic, source_board, output_directory, *,
                       edited_schematic=None, edited_board=None, drc_baseline=None, allow_rule_changes=False):
    """Validate the saved or edited XML, then export a same-basename `.fsch`/`.fbrd` pair."""
    sources = {"schematic": _local_path(source_schematic), "board": _local_path(source_board)}
    basename = sources["schematic"].stem
    if (sources["board"].stem.casefold() != basename.casefold()
            or any(sources[kind].suffix.lower() != ext for kind, ext in NATIVE_FORMATS.items())):
        raise ValueError("use matching, same-basename Fusion .fsch and .fbrd source files")

    templates = {kind: _read_local_file(path, _DOCUMENT_LIMIT) for kind, path in sources.items()}
    original_board = read_fusion_xml(templates["board"], "board")
    source = _source_baseline(templates["board"], original_board, sources["board"])
    if drc_baseline is not None:
        captured = json.loads(_read_local_file(_local_path(drc_baseline), _DOCUMENT_LIMIT))
        if captured != source:
            raise ValueError("DRC baseline does not match the source board revision")
    payloads, roots = {}, {}
    for kind, edited in (("schematic", edited_schematic), ("board", edited_board)):
        path = _local_path(edited) if edited is not None else sources[kind]
        if edited is not None:
            xml = _read_local_file(path, _DOCUMENT_LIMIT)
        else:
            xml = original_board if kind == "board" else read_fusion_xml(templates[kind], kind)
        _validate_source_xml(xml, str(path))
        payloads[kind] = xml
        roots[kind] = ET.fromstring(xml)
    try:
        signature(roots["schematic"], roots["board"], verify_part_details=True)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"schematic and board are out of sync: {exc}") from exc
    output_rules = board_rule_snapshot(roots["board"])
    changes = changed_board_rules(source["source_rules"], output_rules)
    if changes and not allow_rule_changes:
        raise ValueError("DRC baseline changed: " + ", ".join(changes) + ". Use --allow-rule-changes for intentional edits.")
    baseline = {**source, "output_rules": output_rules, "changes": changes}

    target = _local_path(output_directory)
    with atomic_directory(target) as staging:
        for kind, extension in NATIVE_FORMATS.items():
            path = staging / f"{basename}{extension}"
            atomic_write(path, pack_fusion_document(templates[kind], payloads[kind], kind))
            if read_fusion_xml(_read_local_file(path, _DOCUMENT_LIMIT), kind) != payloads[kind]:
                raise ValueError(f"Fusion {kind} payload differs from the validated XML")
        atomic_write(staging / DRC_BASELINE_FILENAME, canonical_json_bytes(baseline))
    return {**{kind: target / f"{basename}{extension}" for kind, extension in NATIVE_FORMATS.items()},
            "drc_baseline": target / DRC_BASELINE_FILENAME}
