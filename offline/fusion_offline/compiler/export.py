"""Publish both files of an offline Fusion Electronics edit together."""

import xml.etree.ElementTree as ET

from fusion_mcp.addin.FusionElectronicsMCP.eagle_import import signature

from .model import _validate_source_xml
from .native import NATIVE_FORMATS, pack_fusion_document, read_fusion_xml
from .paths import _local_path, _read_local_file, atomic_directory, atomic_write


def export_edited_pair(source_schematic, source_board, output_directory, *,
                       edited_schematic=None, edited_board=None):
    """Validate the saved or edited XML, then export a same-basename `.fsch`/`.fbrd` pair."""
    sources = {"schematic": _local_path(source_schematic), "board": _local_path(source_board)}
    basename = sources["schematic"].stem
    if (sources["board"].stem.casefold() != basename.casefold()
            or any(sources[kind].suffix.lower() != ext for kind, ext in NATIVE_FORMATS.items())):
        raise ValueError("use matching, same-basename Fusion .fsch and .fbrd source files")

    limit = 32 * 1024 * 1024
    templates = {kind: _read_local_file(path, limit) for kind, path in sources.items()}
    payloads, roots = {}, {}
    for kind, edited in (("schematic", edited_schematic), ("board", edited_board)):
        path = _local_path(edited) if edited is not None else sources[kind]
        xml = _read_local_file(path, limit) if edited is not None else read_fusion_xml(templates[kind], kind)
        _validate_source_xml(xml, str(path))
        payloads[kind] = xml
        roots[kind] = ET.fromstring(xml)
    try:
        signature(roots["schematic"], roots["board"], verify_part_details=True)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"schematic and board are out of sync: {exc}") from exc

    target = _local_path(output_directory)
    with atomic_directory(target) as staging:
        for kind, extension in NATIVE_FORMATS.items():
            path = staging / f"{basename}{extension}"
            atomic_write(path, pack_fusion_document(templates[kind], payloads[kind], kind))
            if read_fusion_xml(_read_local_file(path, limit), kind) != payloads[kind]:
                raise ValueError(f"Fusion {kind} payload differs from the validated XML")
    return {kind: target / f"{basename}{extension}" for kind, extension in NATIVE_FORMATS.items()}
