"""Read Fusion's Library Manager index without opening or modifying CAD documents."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
import html
import os
from pathlib import Path
import re
import sqlite3
import sys


def database_path(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Fusion library index does not exist: {path}")
        return path
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        raise ValueError("Pass database_path to Fusion's libraries.db on this platform.")
    root = root / "Autodesk/Autodesk Fusion 360/Electron"
    paths = sorted({p.resolve() for pattern in ("lbr/libraries.db", "*/lbr/libraries.db")
                    for p in root.glob(pattern) if p.is_file()})
    if not paths:
        raise ValueError("No Fusion library index found. Open Fusion's Library Manager first, "
                         "or pass database_path to its libraries.db.")
    if len(paths) > 1:
        raise ValueError("Multiple Fusion library indexes found; select the intended account with "
                         "database_path: " + ", ".join(map(str, paths)))
    return paths[0]


def _text(value: str | None) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value or "")).split())


def search(query: str, limit: int = 25, offset: int = 0,
           library_name: str | None = None, index_path: str | None = None) -> dict:
    query = query.strip()
    if not query or len(query) > 256:
        raise ValueError("query must contain 1..256 characters")
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("limit must be 1..100 and offset must be non-negative")
    path = database_path(index_path)
    try:
        # shortcut: Fusion's private index schema may change; adapt on a reported schema error.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")  # Keep counts, attributes and devices in the same WAL snapshot.
            return _search(conn, path, query, limit, offset, library_name)
    except sqlite3.Error as ex:
        raise ValueError(f"Cannot read Fusion library index {path}: {ex}. "
                         "Open/refresh Library Manager and retry; if its schema changed, "
                         "this MCP's library search needs updating.") from None


def _search(conn, path, query, limit, offset, library_name):
    terms = query.casefold().split()
    words = [re.compile(r"(?<![^\W_])" + re.escape(term) + r"(?![^\W_])") for term in terms]
    matches = lambda text: all(term in text for term in terms)
    libraries = {}
    for row in conn.execute("SELECT id, name, description, libtype, path, urn, version, isused, downloaded "
                            "FROM Libraries"):
        libraries[row["id"]] = {
            "library_id": row["id"], "library_name": row["name"], "library_path": row["path"],
            "library_urn": row["urn"], "library_version": row["version"], "library_type": row["libtype"],
            "active": bool(row["isused"]), "downloaded": bool(row["downloaded"]),
            "library_description": _text(row["description"]),
        }
    names = Counter(lib["library_name"].casefold() for lib in libraries.values())
    for lib in libraries.values():
        lib["ambiguous_library_name"] = names[lib["library_name"].casefold()] > 1
    if library_name is not None:
        libraries = {key: lib for key, lib in libraries.items()
                     if lib["library_name"].casefold() == library_name.casefold()}
    indexed = {row[0] for row in conn.execute(
        "SELECT DISTINCT s.library FROM DeviceSets s JOIN Devices d ON d.deviceset=s.id")}
    unindexed = [lib for key, lib in libraries.items() if key not in indexed]
    missing_matches = [lib for lib in unindexed if matches(
        (lib["library_name"] + " " + lib["library_description"]).casefold())]
    missing_matches.sort(key=lambda lib: (lib["library_name"].casefold(), lib["library_id"]))

    attrs = defaultdict(dict)
    for row in conn.execute("SELECT technology, name, value FROM DeviceAttributes ORDER BY id"):
        attrs[row["technology"]][row["name"]] = row["value"]
    parts = []
    for row in conn.execute("""
        SELECT d.id, s.library, s.name AS deviceset, s.description,
               d.variantname AS variant, p.name AS package, p.description AS package_description,
               t.id AS technology_id, t.technology, t.fullname
        FROM Devices d JOIN DeviceSets s ON s.id=d.deviceset
        LEFT JOIN Packages p ON p.id=d.package
        LEFT JOIN Technologies t ON t.device=d.id
    """):
        lib = libraries.get(row["library"])
        if lib is None:
            continue
        technology = row["technology"] or ""
        device = row["fullname"] or (
            row["deviceset"].replace("*", technology) + (row["variant"] or ""))
        attributes = attrs.get(row["technology_id"], {})
        description = _text(row["description"])
        part_text = " ".join([device, row["deviceset"], row["variant"] or "", technology,
                              row["package"] or "", _text(row["package_description"]), description,
                              *[f"{name} {value}" for name, value in attributes.items()]]).casefold()
        text = " ".join([lib["library_name"].casefold(), lib["library_description"].casefold(), part_text])
        if matches(text):
            part = {**lib, "device": device, "deviceset": row["deviceset"],
                    "variant": row["variant"] or "", "technology": technology,
                    "package": row["package"], "description": description, "attributes": attributes}
            rank = (device.casefold() != query.casefold(), not device.casefold().startswith(query.casefold()),
                    -sum(bool(word.search(part_text)) for word in words), not lib["active"],
                    lib["library_name"].casefold(), device.casefold(),
                    lib["library_id"], row["id"], row["technology_id"] or 0)
            parts.append((rank, part))
    parts.sort(key=lambda item: item[0])
    warnings = ["Search covers this local Library Manager index, not a live cloud catalog. "
                "Refresh/download missing libraries in Fusion and search again. "
                "Inactive libraries must be activated before placement."]
    if unindexed:
        warnings.append(f"{len(unindexed)} libraries have no indexed devices; their components could not "
                        "be searched. Matching library names/descriptions are listed separately.")
    if any(part["ambiguous_library_name"] for _, part in parts[offset:offset + limit]):
        warnings.append("Multiple libraries share a result's name. Resolve the intended library in "
                        "Library Manager using its path/URN before calling add_part.")
    return {
        "database_path": str(path), "query": query, "total": len(parts), "offset": offset,
        "next_offset": offset + limit if offset + limit < len(parts) else None,
        "parts": [part for _, part in parts[offset:offset + limit]],
        "coverage": {"libraries": len(libraries), "libraries_with_devices": len(libraries) - len(unindexed),
                     "libraries_without_devices": len(unindexed)},
        "unindexed_libraries": missing_matches[offset:offset + limit],
        "unindexed_total": len(missing_matches),
        "unindexed_next_offset": offset + limit if offset + limit < len(missing_matches) else None,
        "warnings": warnings,
    }
