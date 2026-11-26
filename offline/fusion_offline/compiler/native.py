"""Replace the EAGLE payload in a Fusion Electronics document archive."""

from contextlib import contextmanager
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from .paths import normalize_artifact_path


NATIVE_FORMATS = {"schematic": ".fsch", "board": ".fbrd"}


@contextmanager
def _document_archive(data: bytes, kind: str):
    extension = "." + NATIVE_FORMATS[kind][2:]
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            for entry in entries:
                name = entry.filename.removesuffix("/")
                if normalize_artifact_path(name) != name:
                    raise ValueError(f"invalid Fusion archive member name: {entry.filename!r}")
            names = {entry.filename.casefold() for entry in entries}
            docs = [entry for entry in entries
                    if "/electron.BlobParts/" in entry.filename and entry.filename.lower().endswith(extension)]
            if len(names) != len(entries) or "manifest.dat" not in names or len(docs) != 1:
                raise ValueError(f"expected one {extension} payload and a manifest in the Fusion {kind} archive")
            if sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
                raise ValueError(f"expanded Fusion {kind} archive exceeds 64 MiB")
            yield archive, docs[0]
    except (BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ValueError(f"invalid Fusion {kind} archive: {exc}") from exc


def read_fusion_xml(data: bytes, kind: str) -> bytes:
    with _document_archive(data, kind) as (archive, document):
        return archive.read(document)


def pack_fusion_document(template: bytes, xml: bytes, kind: str) -> bytes:
    """Keep the original document identity and sidecars while replacing its XML."""
    output = BytesIO()
    with _document_archive(template, kind) as (archive, document), ZipFile(output, "w") as updated:
        updated.comment = archive.comment
        for entry in archive.infolist():
            original = archive.read(entry)
            updated.writestr(entry, xml if entry is document else original)
    return output.getvalue()
