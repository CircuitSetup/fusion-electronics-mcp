"""Fusion Electronics MCP bridge add-in.

A thin, fixed set of primitives served over 127.0.0.1 for the external MCP
server (see docs/protocol.md). All design logic lives in the server; this
add-in only touches Fusion, always on the main thread.

    server --TCP 127.0.0.1 (token)--> socket thread
        --app.fireCustomEvent(job id)--> main-thread handler --> Fusion
        <--threading.Event-- result JSON

EAGLE commands pass an allowlist of
verbs that edit the open design only: no RUN (ULPs), SCRIPT, WRITE, EXPORT,
CAM, OPEN or other file/system commands.
The separate run_bundled_ulp operation runs only packaged, reviewed read-only ULPs.
run_registered_ulp runs explicitly registered, hash-pinned sources, including legacy ULPs.

Connection info (port + per-session token) is written to
%APPDATA%/fusion-electronics-mcp/bridge.json and removed on stop.
Stdlib only; runs on Fusion's embedded Python.
"""

import contextlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import traceback
import uuid

import adsk.core  # type: ignore
import adsk.electron  # type: ignore

ADDIN_VERSION = "0.14.19"
PROTOCOL = 1
EVENT_ID = "fusion_electronics_mcp_bridge"
DEFAULT_TIMEOUT = 60.0
INFO_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "fusion-electronics-mcp")
INFO_PATH = os.path.join(INFO_DIR, "bridge.json")
WORK_DIR = os.path.join(tempfile.gettempdir(), "fusion-electronics-mcp")
# where each design was last found, so open_design by name alone goes straight to its folder
FOLDERS_PATH = os.path.join(INFO_DIR, "design_folders.json")
U_PER_MM = 320000.0

# EAGLE command verbs allowed through `run` / `run_script`.
ALLOWED_VERBS = {
    "ADD", "ATTRIBUTE", "AUTO", "CHANGE", "CIRCLE", "CLASS", "CONNECT", "DELETE", "DESCRIPTION",
    "DISPLAY", "DRC", "EDIT", "ERC", "GATESWAP", "GRID", "GROUP", "HOLE", "INVOKE", "JUNCTION", "LABEL", "LAYER", "MIRROR",
    "MOVE", "NAME", "NET", "PACKAGE", "PAD", "PIN", "PINSWAP", "POLYGON", "PREFIX", "RATSNEST",
    "RECT", "REDO", "REPLACE", "RIPUP", "ROTATE", "ROUTE", "SET", "SIGNAL", "SMASH", "SMD",
    "SPLIT", "TECHNOLOGY", "TEXT", "UNDO", "VALUE", "VIA", "WIRE",
}
# EDIT is only allowed for library objects (.pac/.sym/.dev) and schematic
# sheets (.s<n>), never for files.
_EDIT_OK = re.compile(r"^EDIT\s+('?[^';]+\.(pac|sym|dev)'?|\.s\d+)\s*$", re.I)

_app = None
_ui = None
_event = None
_handler = None
_server = None
_token = ""
_stop = threading.Event()
_jobs = {}
_jobs_lock = threading.Lock()
_management_module = None
_inspection_module = None
_events = None
_pair_import_module = None
_pair_import_records = {}
_pair_import_lock = threading.RLock()
_PAIR_PENDING = frozenset(('queued','awaiting_file_selection','verifying','state_unknown'))


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# command validation


def split_commands(text):
    """Split an EAGLE command string on ';' outside single quotes."""
    out, buf, q = [], [], False
    for ch in text:
        if ch == "'":
            q = not q
        if ch == ";" and not q:
            s = "".join(buf).strip()
            if s:
                out.append(s)
            buf = []
        else:
            buf.append(ch)
    s = "".join(buf).strip()
    if s:
        out.append(s)
    return out


_AUTO_FILE = re.compile(r"^\s*AUTO\s+(SAVE|LOAD)\b\s*(.*?)\s*;?\s*$", re.I)


def _auto_file_ok(cmd):
    """AUTO SAVE / LOAD read and write autorouter files: only under the system temp folder."""
    m = _AUTO_FILE.match(cmd)
    if not m:
        return True
    path = m.group(2).strip().strip("'\"")
    if not path:
        return False
    tmp = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(path)
    return real.startswith(tmp + os.sep)


def validate(text):
    cmds = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cmds += split_commands(line)
    for c in cmds:
        verb = c.split(None, 1)[0].upper()
        if verb not in ALLOWED_VERBS:
            raise BridgeError("command_not_allowed", f"EAGLE command {verb!r} is not allowed by the bridge")
        if verb == "EDIT" and not _EDIT_OK.match(c):
            raise BridgeError("command_not_allowed", "EDIT is only allowed for library .pac/.sym/.dev objects")
        if verb == "AUTO" and not _auto_file_ok(c):
            raise BridgeError("command_not_allowed", "AUTO SAVE / LOAD is only allowed for files in the temp folder")
    return cmds


# ---------------------------------------------------------------------------
# helpers (main thread only)


def _kind_of(doc):
    t = doc.objectType if doc else ""
    for k in ("Board", "Schematic", "Library", "EcadDesign"):
        if t.endswith("::" + k + "Document"):
            return {"Board": "board", "Schematic": "schematic", "Library": "library", "EcadDesign": "design"}[k]
    return "other"


_DESIGN_PRODUCTS = {"design": ("EcadDesignDocument", "ecadDesign"),
                    "board": ("BoardDocument", "board"),
                    "schematic": ("SchematicDocument", "schematic")}


def _document_product(doc, kind):
    cls_name, attr = _DESIGN_PRODUCTS[kind]
    product = getattr(doc, attr, None)
    if product is None:
        cls = getattr(adsk.electron, cls_name, None)
        if cls is not None:
            product = getattr(cls.cast(doc), attr, None)
    return product


def _file_id(doc):
    return getattr(getattr(doc, "dataFile", None), "id", None)


def _same_document(left, right):
    if (left is None or right is None
            or not getattr(left, "isValid", True) or not getattr(right, "isValid", True)):
        return False
    if left == right:
        return True
    return _same_file_version(getattr(left, "dataFile", None), getattr(right, "dataFile", None))


def _same_file_version(left_file, right_file):
    left_id, right_id = getattr(left_file, "versionId", None), getattr(right_file, "versionId", None)
    if left_id and right_id:
        return left_id == right_id
    version = getattr(left_file, "versionNumber", None)
    return bool(version is not None and version == getattr(right_file, "versionNumber", None)
                and (file_id := getattr(left_file, "id", None)) and file_id == getattr(right_file, "id", None))


def _product_document(product, kind, docs):
    if product is not None:
        return next((doc for doc in docs if _kind_of(doc) == kind
                     and _document_product(doc, kind) == product), None)
    return None


def _family_peer(doc, kind, docs):
    own = _kind_of(doc)
    if own not in _DESIGN_PRODUCTS or kind == own:
        return None
    product = _document_product(doc, own)
    prop = kind if own == "design" else {"board": "linkedBoard", "schematic": "linkedSchematic"}.get(kind)
    return _product_document(getattr(product, prop, None), kind, docs) if prop else None


def _family_design(doc):
    kind = _kind_of(doc)
    if kind not in _DESIGN_PRODUCTS:
        return None
    product = _document_product(doc, kind)
    return product if kind == "design" else getattr(product, "parentDesign", None)


_created_families = []


def _reference_files(doc, field, evidence=None):
    """Read direct reference metadata without opening files or listing projects."""
    try:
        owner = doc if field == "documentReferences" else getattr(doc, "dataFile", None)
        refs = getattr(owner, field, None)
        count = refs.count if refs is not None else 0
        if evidence is not None:
            evidence[field] = {"count": count, "files": []}
        if count > 32:
            raise ValueError("reference count exceeds safety limit")
        for i in range(count):
            ref = refs.item(i)
            peer = getattr(ref, "referencedDocument", None) if field == "documentReferences" else None
            file = (getattr(ref, "dataFile", None) if field == "documentReferences" else ref)
            if file is None and peer is not None:
                file = getattr(peer, "dataFile", None)
            if evidence is not None:
                evidence[field]["files"].append({"name": getattr(file, "name", None) or getattr(peer, "name", None),
                                                  "id": getattr(file, "id", None),
                                                  "version_id": getattr(file, "versionId", None)})
            yield peer, file
    except Exception as exc:
        if evidence is not None:
            evidence[field] = {**evidence.get(field, {}), "error": str(exc)}


def _family_keys(docs):
    _created_families[:] = [family for family in _created_families if getattr(family[0], "isValid", True)]
    rows = [(doc, _family_design(doc)) for doc in docs]
    parents = [(doc, design) for doc, design in rows if _kind_of(doc) == "design"]
    children = [doc for doc, _ in rows if _kind_of(doc) in ("board", "schematic")]
    claimed = []
    def matches_reference(child, refs):
        return any(_same_document(child, peer)
                   or _same_file_version(getattr(child, "dataFile", None), file) for peer, file in refs)

    for parent, design in parents:
        refs = list(_reference_files(parent, "documentReferences")) if children else []
        observed = [child for root, *members in _created_families if _same_document(root, parent) for child in members]
        members = [child for child in children if matches_reference(child, refs)
                   or any(_same_document(child, peer) for peer in observed)]
        matched_kinds = {_kind_of(child) for child in members}
        remaining = [child for child in children if _kind_of(child) not in matched_kinds]
        if remaining:
            saved = list(_reference_files(parent, "childReferences"))
            members.extend(child for child in remaining if matches_reference(child, saved))
        claimed.append(((parent, design), members))
    for doc, design in rows:
        matches = [key for key, members in claimed if any(_same_document(doc, member) for member in members)]
        if len(matches) > 1:
            raise BridgeError("ambiguous", f"more than one Electronics design references {doc.name!r}")
        root = _product_document(design, "design", docs)
        yield doc, matches[0] if matches else (root if root is not None else doc, design)


def _same_family(left, right):
    left_doc, left_design = left
    right_doc, right_design = right
    return (_same_document(left_doc, right_doc) or
            (left_design is not None and right_design is not None and left_design == right_design))


def _docs():
    return [_app.documents.item(i) for i in range(_app.documents.count)]


_document_ids = []


def _document_id(d):
    # Retain the API wrapper so an unsaved document cannot acquire a reused object ID.
    for doc, token in _document_ids:
        if doc == d: return token
    token = "session:" + uuid.uuid4().hex
    _document_ids.append((d, token))
    return token


def _doc_info(d):
    info = {"name": d.name, "kind": _kind_of(d), "modified": d.isModified, "saved": d.isSaved,
            "active": d.isActive, "document_id": _document_id(d), "project_id": None, "data_file_id": None}
    with contextlib.suppress(Exception):
        if d.dataFile:
            info["version"] = d.dataFile.versionNumber
            info["data_file_id"] = d.dataFile.id
            info["project_id"] = d.dataFile.parentProject.id
    return info


def _product_kind():
    p = _app.activeProduct
    t = p.objectType if p else ""
    return {"adsk::electron::Board": "board", "adsk::electron::Schematic": "schematic",
            "adsk::electron::Library": "library", "adsk::electron::EcadDesign": "design"}.get(t, "other")


def _settle(n=10):
    for _ in range(n):
        adsk.doEvents()
        time.sleep(0.05)


def op_ping(args):
    return {"addin_version": ADDIN_VERSION, "protocol": PROTOCOL, "fusion_version": _app.version,
            "pid": os.getpid()}


def op_context(args):
    doc = _app.activeDocument
    ws = None
    with contextlib.suppress(Exception):
        ws = _ui.activeWorkspace.id
    return {"active_document": _doc_info(doc) if doc else None, "active_editor": _product_kind(),
            "workspace": ws, "documents": [_doc_info(d) for d in _docs() if _kind_of(d) != "other"]}


def op_activate(args):
    kind = args.get("kind")
    if kind not in ("board", "schematic", "library"):
        raise BridgeError("bad_args", "kind must be board, schematic or library")
    if _product_kind() == kind and not args.get("name"):
        return op_context({})
    name = args.get("name")
    cand = [d for d in _docs() if _kind_of(d) == kind and (name is None or d.name == name)]
    if name is None and _app.activeDocument is not None:
        active = _app.activeDocument
        if _kind_of(active) in _DESIGN_PRODUCTS and kind in _DESIGN_PRODUCTS:
            families = list(_family_keys(_docs()))
            active_key = next((key for doc, key in families if _same_document(doc, active)), (active, _family_design(active)))
            peer = _family_peer(active, kind, cand)
            cand = [doc for doc, key in families if _kind_of(doc) == kind
                    and (_same_document(doc, peer) or _same_family(key, active_key))]
            if len(cand) > 1:
                raise BridgeError("ambiguous", f"more than one {kind} revision matches the active Electronics design")
    if not cand:
        raise BridgeError("not_open", f"no open {kind} document named {name!r}")
    cand[0].activate()
    _settle(20)
    if _product_kind() != kind or not _same_document(_app.activeDocument, cand[0]):
        raise BridgeError("activate_failed", f"Fusion did not activate {kind} document {cand[0].name!r}")
    return op_context({})


def _active_product(kind):
    if _product_kind() != kind:
        raise BridgeError("wrong_editor", f"the active editor is {_product_kind()}, need {kind} (call activate)")
    p = _app.activeProduct
    return {"board": adsk.electron.Board, "schematic": adsk.electron.Schematic,
            "library": adsk.electron.Library}[kind].cast(p)


def _linked(kind):
    """The board or schematic product, reachable from whichever Electronics
    editor is active (board, schematic, or the design overview)."""
    cur = _product_kind()
    p = _app.activeProduct
    if cur == kind:
        return _active_product(kind)
    if cur == "design":
        d = adsk.electron.EcadDesign.cast(p)
        return d.board if kind == "board" else d.schematic
    if cur == "schematic" and kind == "board":
        return _active_product("schematic").linkedBoard
    if cur == "board" and kind == "schematic":
        return _active_product("board").linkedSchematic
    # The active document is not an Electronics editor (e.g. a 3D view): use
    # the open board/schematic of the same design; never guess between designs
    # (this once exported the wrong design's board).
    cand = [d for d in _docs() if _kind_of(d) == kind]
    act = _app.activeDocument.name if _app.activeDocument is not None else None
    same = [d for d in cand if d.name == act]
    if same or len(cand) == 1:
        return _document_product((same or cand)[0], kind)
    if not cand:
        raise BridgeError("not_open", f"no {kind} is open")
    raise BridgeError("ambiguous", f"the active document is not an Electronics editor and {len(cand)} {kind}s "
                      f"are open ({', '.join(sorted({d.name for d in cand}))}); activate the one to use")


def op_export(args):
    kind = args.get("kind")
    if kind == "board":
        prod, fn, ext = _linked("board"), "createEagleBrdExportOptions", ".brd"
    elif kind == "schematic":
        prod, fn, ext = _linked("schematic"), "createEagleSchExportOptions", ".sch"
    elif kind == "library":
        prod, fn, ext = _active_product("library"), "createEagleLbrExportOptions", ".lbr"
    else:
        raise BridgeError("bad_args", "kind must be board, schematic or library")
    if prod is None:
        raise BridgeError("not_linked", f"no {kind} is linked to the active document")
    return _export_product(prod, kind)


def _export_product(prod, kind):
    fn, ext = {'board': ('createEagleBrdExportOptions','.brd'),
               'schematic': ('createEagleSchExportOptions','.sch'),
               'library': ('createEagleLbrExportOptions','.lbr')}[kind]
    os.makedirs(WORK_DIR, exist_ok=True)
    path = os.path.join(WORK_DIR, f"export-{uuid.uuid4().hex}{ext}")
    t0 = time.perf_counter()
    em = prod.exportManager
    if not em.execute(getattr(em, fn)(path)) or not os.path.exists(path):
        raise BridgeError("export_failed", f"Fusion did not write the {kind} export")
    return {"path": path, "bytes": os.path.getsize(path), "ms": round((time.perf_counter() - t0) * 1000, 1),
            "name": prod.name}


def _grouped(editor_kind, fn):
    """Run fn() inside one design change so a tool call is ONE undo step.

    Verified on Fusion 2705.1.15: without this, each EAGLE command in a call is its own
    undo step, so a single UNDO could not revert a multi-command write. On an
    exception the change is cancelled (cancelDesignChange rolls back cleanly).
    Exports cannot run inside an open design change ("resource deadlock"), so
    the server verifies after this returns and UNDOes once on mismatch.
    """
    prod = _active_product(editor_kind) if editor_kind in ("board", "schematic", "library") else None
    if prod is None:
        return fn()
    prod.beginDesignChange("fusion-electronics-mcp")
    try:
        out = fn()
    except Exception:
        with contextlib.suppress(Exception):
            prod.cancelDesignChange()
        raise
    prod.endDesignChange()
    return out


def _is_history(cmds):
    return all(c.split(None, 1)[0].upper() in ("UNDO", "REDO") for c in cmds)


# Commands that manage their own undo history or edit nothing. Wrapping AUTO
# in begin/endDesignChange raised the undo.cpp IsRecording() assertion on
# 2705.1.15, so these are never wrapped.
NO_GROUP = {"AUTO", "DRC", "ERC", "RATSNEST", "SET", "UNDO", "REDO"}


def _groupable(cmds):
    return not any(c.split(None, 1)[0].upper() in NO_GROUP for c in cmds)


def op_run(args):
    if args.get("expected_document_id"):
        active = _app.activeDocument
        if active is None or _document_id(active) != args["expected_document_id"]:
            raise BridgeError("wrong_document", "The reviewed target is no longer active; nothing was written")
    cmds = validate(args.get("commands") or "")
    expect = args.get("editor")
    if expect and _product_kind() != expect:
        raise BridgeError("wrong_editor", f"the active editor is {_product_kind()}, need {expect}")
    if not cmds:
        raise BridgeError("bad_args", "no commands")
    if not _is_history(cmds) and any(c.split(None, 1)[0].upper() in ("UNDO", "REDO") for c in cmds):
        raise BridgeError("bad_args", "UNDO/REDO must be sent on their own")
    line = "Electron.run " + " ".join(c + ";" for c in cmds)
    t0 = time.perf_counter()
    if _groupable(cmds):
        raw = _grouped(_product_kind(), lambda: _app.executeTextCommand(line))
    else:
        raw = _app.executeTextCommand(line)
    return {"raw": raw, "ms": round((time.perf_counter() - t0) * 1000, 1), "editor": _product_kind()}


def op_run_script(args):
    text = args.get("script") or ""
    cmds = validate(text)
    expect = args.get("editor")
    if expect and _product_kind() != expect:
        raise BridgeError("wrong_editor", f"the active editor is {_product_kind()}, need {expect}")
    if any(c.split(None, 1)[0].upper() in ("UNDO", "REDO") for c in cmds):
        raise BridgeError("bad_args", "scripts may not contain UNDO/REDO")
    os.makedirs(WORK_DIR, exist_ok=True)
    path = os.path.join(WORK_DIR, f"script-{uuid.uuid4().hex}.scr")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    t0 = time.perf_counter()
    try:
        raw = _grouped(_product_kind(), lambda: _app.executeTextCommand(f'Electron.runScript "{path}"'))
    finally:
        with contextlib.suppress(Exception):
            os.remove(path)
    return {"raw": raw, "ms": round((time.perf_counter() - t0) * 1000, 1), "editor": _product_kind()}


BUNDLED_ULPS = {"probe": "probe.ulp"}


def op_run_bundled_ulp(args):
    """Run only a reviewed, bundled read-only ULP; never accept source, paths or ULP arguments."""
    name = args.get("name", "probe")
    if set(args) - {"name", "design"} or not isinstance(name, str) or name not in BUNDLED_ULPS:
        raise BridgeError("bad_args", "only the bundled 'probe' ULP is supported; no paths or extra arguments")
    doc, editor = _app.activeDocument, _product_kind()
    if doc is None or editor not in ("board", "schematic"):
        raise BridgeError("wrong_editor", "the probe needs an active board or schematic")
    design = args.get("design")
    if design is not None and (not isinstance(design, str) or design != doc.name):
        raise BridgeError("wrong_document", "the requested design is not active")
    source = os.path.join(os.path.dirname(__file__), "ulps", BUNDLED_ULPS[name])
    if not os.path.isfile(source):
        raise BridgeError("missing_ulp", "the bundled ULP is missing; reinstall the add-in/server")
    os.makedirs(WORK_DIR, exist_ok=True)
    before = doc.isModified
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ulp-", dir=WORK_DIR) as folder:
        output = os.path.join(folder, "result.json")
        paths = [p.replace("\\", "/") for p in (source, output)]
        if any(any(c in p for c in "'\";\r\n") for p in paths):
            raise BridgeError("bad_path", "ULP paths contain unsupported command characters")
        # RUN is deliberately not added to validate(): only these packaged programs may execute.
        _app.executeTextCommand(f"Electron.run RUN '{paths[0]}' '{paths[1]}';")
        if _app.activeDocument != doc or _product_kind() != editor or doc.isModified != before:
            raise BridgeError("context_changed", "the document or its modified state changed during the probe")
        try:
            with open(output, "rb") as f:
                raw = f.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("probe output exceeds 2 MiB")
            data = json.loads(raw)
            if (not isinstance(data, dict) or data.get("schema") != 1 or data.get("probe") != name
                    or data.get("editor") != editor or data.get("complete") is not True):
                raise ValueError("incomplete or mismatched probe output")
        except (OSError, ValueError) as ex:
            raise BridgeError("ulp_failed", f"no valid result from the bundled ULP: {ex}") from None
    return {"name": name, "document": doc.name, "editor": editor, "data": data,
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


def op_run_registered_ulp(args):
    """Only registry-pinned source; never accept executable source or paths over the bridge."""
    spec = importlib.util.spec_from_file_location('femcp_ulp_support', os.path.join(os.path.dirname(__file__), 'ulp_support.py'))
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    try:
        if set(args) - {'name', 'arguments', 'registration_sha256', 'expected_document_id', 'legacy_source_sha256'}:
            raise ValueError('Unknown ULP arguments')
        entry, source = support.load(support.registry_dir(), args.get('name'))
        if support.digest(entry) != args.get('registration_sha256'): raise ValueError('Registration changed')
        values = support.arguments(entry, args.get('arguments'))
        legacy = entry['mode'] == 'legacy'
        if legacy and args.get('legacy_source_sha256') != entry['sha256']:
            raise ValueError('Legacy execution requires an explicitly reviewed source hash')
        if not legacy and 'legacy_source_sha256' in args: raise ValueError('Wrong ULP workflow')
        doc, editor = _app.activeDocument, _product_kind()
        if doc is None or _document_id(doc) != args.get('expected_document_id'):
            raise ValueError('The requested document is not active')
        if editor not in entry['editors']: raise ValueError('Wrong editor for this ULP')
        before = doc.isModified
        os.makedirs(WORK_DIR, exist_ok=True)
        # Retain legacy-generated files; their paths may be based on argv[0].
        folder = tempfile.mkdtemp(prefix='legacy-ulp-' if legacy else 'registered-ulp-', dir=WORK_DIR)
        try:
            path = os.path.join(folder, os.path.basename(entry['source_path']))
            output = os.path.join(folder, 'result.json')
            with open(path, 'wb') as f:
                f.write(source if legacy else support.render_source(source, output).encode('utf-8'))
            path = path.replace('\\', '/')
            if any(c in path for c in "'\";\r\n"): raise ValueError('Unsupported source path characters')
            command = 'Electron.run RUN ' + ' '.join("'" + value + "'" for value in [path] + values) + ';'
            raw = _app.executeTextCommand(command)
            if legacy: return {'legacy': True, 'raw': raw, 'artifact_directory': folder}
            if _app.activeDocument != doc or _product_kind() != editor or doc.isModified != before:
                raise ValueError('Document or modified state changed during read/preview; inspect Fusion')
            data = support.read_json(output)
            fields = {'schema', 'mode', 'editor', 'complete'} | ({'data'} if entry['mode'] == 'read' else {'commands', 'changes'})
            if (not isinstance(data, dict) or set(data) != fields or data.get('schema') != 1
                    or data.get('mode') != entry['mode'] or data.get('editor') != editor or data.get('complete') is not True):
                raise ValueError('Incomplete or mismatched ULP output')
            return {'name': entry['name'], 'sha256': entry['sha256'], 'data': data}
        finally:
            if not legacy: shutil.rmtree(folder)
    except (OSError, ValueError, TypeError) as ex:
        raise BridgeError('ulp_failed', str(ex)) from None


def _management():
    global _management_module
    if _management_module is None:
        spec = importlib.util.spec_from_file_location('femcp_management', os.path.join(os.path.dirname(__file__), 'management.py'))
        _management_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_management_module)
    return _management_module


def op_list_scripts(args):
    return _management().list_scripts(_app, args.get('query', ''), args.get('limit', 25), args.get('offset', 0))


def _inspection():
    global _inspection_module
    if _inspection_module is None:
        spec = importlib.util.spec_from_file_location('femcp_inspection', os.path.join(os.path.dirname(__file__), 'inspection.py'))
        _inspection_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_inspection_module)
    return _inspection_module


def op_selection(args):
    result = _inspection().selection(_ui, args.get('limit', 25), args.get('offset', 0))
    result['document'] = _doc_info(_app.activeDocument) if _app.activeDocument else None
    return result


def op_commands(args):
    return _inspection().commands(_ui, args.get('query', ''), args.get('command_id'),
                                  args.get('limit', 25), args.get('offset', 0))


def _inspection_design():
    import adsk.fusion  # type: ignore
    design = adsk.fusion.Design.cast(_app.activeProduct)
    doc = _app.activeDocument
    if design is None:
        _, doc = _pcb3d_link()
        design = _pcb3d_product(doc) if doc is not None else None
    if design is None or doc is None:
        raise BridgeError('not_found', 'Activate a 3D Fusion design or open the verified 3D PCB linked to the active electronics design; no model is generated automatically')
    return design, doc


def op_geometry_bodies(args):
    helper = _inspection(); limit = args.get('limit', 25); offset = args.get('offset', 0)
    helper.page(limit, offset)
    design, doc = _inspection_design()
    rows, native, unsupported = helper.bodies(design)
    return {'document': _doc_info(doc), 'bodies': rows[offset:offset+limit], 'total': len(rows),
            'solid_count': len(native), 'unsupported_geometry': unsupported,
            'next_offset': offset+limit if offset+limit < len(rows) else None,
            'coverage': 'Current root-context BRep bodies, including hidden bodies. Body IDs bind positions to an entity/occurrence inventory fingerprint; changed inventories or rotated tokens require a fresh inventory. These are not persistent edit handles. Missing or outdated models are not detected.'}


def op_geometry_clearance(args):
    design, doc = _inspection_design()
    if not args.get('expected_document_id') or args['expected_document_id'] != _document_id(doc):
        raise BridgeError('stale_document', 'Use expected_document_id from the current get_3d_bodies document')
    modified = doc.isModified
    result = _inspection().clearance(_app, design, adsk.core, args.get('minimum_clearance_mm', 0.2), args.get('body_ids'))
    if doc.isModified != modified:
        raise BridgeError('document_changed', 'Document modification state changed during geometry inspection; result cannot be verified')
    result['document'] = _doc_info(doc)
    return result


def op_inspect_script(args):
    return _management().inspect_script(_app, args.get('folder'))


def op_control_script(args):
    result = _management().control_script(_app, os.path.dirname(__file__), args.get('action'), args.get('folder'),
                                          args.get('expected_id'), args.get('expected_source_sha256'))
    if _events: _events.append('script_control', **result)
    return result


def op_events(args):
    if _events is None: raise BridgeError('needs_addin', 'Event log requires the running add-in')
    return _events.read(args.get('session_id'), args.get('after_seq', 0), args.get('limit', 100))


def op_reload_addin(args):
    result = _management().queue_reload(_app, os.path.dirname(__file__), INFO_PATH, args.get('request_id'))
    if _events: _events.append('reload_requested', request_id=result['request_id'])
    return result


def op_design_rules(args):
    """The V2 <designrules> element (net classes, clearance rules, layer
    stackup with dielectric constants) of the open board. The classic EAGLE
    export drops it, but Fusion's cloud-format working copy of the open
    board keeps it: %TEMP%/Neutron/ElectronFileOutput/<pid>/brd-*/<name>.brd.
    The working copy may lag unsaved in-session edits; stackups rarely change
    mid-session, and the path and timestamp are returned so callers can tell."""
    board = _linked("board")
    base = os.path.join(tempfile.gettempdir(), "Neutron", "ElectronFileOutput", str(os.getpid()))
    cands = []
    if os.path.isdir(base):
        for d in os.listdir(base):
            if d.startswith("brd-"):
                f = os.path.join(base, d, board.name + ".brd")
                if os.path.exists(f):
                    cands.append(f)
    if not cands:
        raise BridgeError("not_found", f"no Fusion working copy found for board {board.name!r}")
    path = max(cands, key=os.path.getmtime)
    with open(path, "rb") as f:
        data = f.read()
    a = data.find(b"<designrules")
    b = data.find(b"</designrules>", a)
    if a < 0 or b < 0:
        raise BridgeError("not_found", "the working copy has no <designrules> element")
    return {"board": board.name, "path": path, "modified": time.strftime("%Y-%m-%dT%H:%M:%S",
            time.localtime(os.path.getmtime(path))), "xml": data[a:b + len(b"</designrules>")].decode("utf-8", "replace")}


def op_pours(args):
    """Live pour settings from the API (the cloud working copy lags behind
    edits, so it is not used here)."""
    board = _linked("board")
    out = []
    for i in range(board.signals.count):
        sig = board.signals.item(i)
        for j in range(sig.polyPours.count):
            p = sig.polyPours.item(j)
            pts = []
            with contextlib.suppress(Exception):
                for k in range(p.wires.count):
                    w = p.wires.item(k)
                    pts.append([round(w.x1 / U_PER_MM, 4), round(w.y1 / U_PER_MM, 4)])
            out.append({"net": sig.name, "layer": p.layer, "thermals": p.thermals,
                        "thermal_width_mm": round(p.thermalWidth / U_PER_MM, 4),
                        "isolate_mm": round(p.isolate / U_PER_MM, 4), "width_mm": round(p.width / U_PER_MM, 4),
                        "rank": p.rank, "orphans": p.orphans, "hatched": p.hatched, "outline": pts})
    return {"pours": out}


def op_layers(args):
    """Board layers with visibility, so pick-based edits can show only the
    layer they target and restore the user's exact view afterwards."""
    board = _linked("board")
    return {"layers": [{"number": l.number, "name": l.name, "visible": l.visible, "used": l.used}
                       for l in (board.layers.item(i) for i in range(board.layers.count))]}


_MODES = {"ignore": ("Ignore Violators", "IgnoreViolatorsCommand"),
          "walkaround": ("Walkaround Violators", "WalkaroundViolatorsCommand"),
          "push": ("Push Violators", "PushViolatorsCommand")}


def op_violation_mode(args):
    """Get (and optionally set) the board's violation mode: how Fusion treats
    objects in the way of an edit. In 'push' mode a MOVE is shoved away from
    other parts, even across board sides (a bottom switch under a top LED)."""
    lc = adsk.core.ListControlDefinition.cast(_ui.commandDefinitions.itemById("ViolationModeCommand").controlDefinition)

    def current():
        for i in range(lc.listItems.count):
            if lc.listItems.item(i).isSelected:
                name = lc.listItems.item(i).name
                return next((k for k, v in _MODES.items() if v[0] == name), name)
        return None
    before = current()
    want = args.get("set")
    if want:
        if want not in _MODES:
            raise BridgeError("bad_args", "mode must be ignore, walkaround or push")
        _ui.commandDefinitions.itemById(_MODES[want][1]).execute()
        _settle(8)
    return {"before": before, "mode": current()}


def op_errors(args):
    kind = args.get("kind") or _product_kind()
    if kind not in ("board", "schematic"):
        raise BridgeError("bad_args", "kind must be board or schematic")
    prod = _linked(kind)
    out = []
    for i in range(prod.errors.count):
        e = prod.errors.item(i)
        row = {}
        for k in ("code", "description", "layer", "state", "sheet", "signature"):
            with contextlib.suppress(Exception):
                row[k] = getattr(e, k)
        with contextlib.suppress(Exception):
            row["x_mm"] = round(e.x / U_PER_MM, 4)
            row["y_mm"] = round(e.y / U_PER_MM, 4)
        out.append(row)
    return {"kind": kind, "count": len(out), "errors": out}


def op_save(args):
    doc = _app.activeDocument
    if doc is None:
        raise BridgeError("no_document", "no active document")
    desc = str(args.get("description") or "saved by fusion-electronics-mcp")[:200]
    before = None
    with contextlib.suppress(Exception):
        before = doc.dataFile.versionNumber
    if doc.isSaved and not doc.isModified:
        return {"ok": True, "document": doc.name, "version_before": before, "version_after": before,
                "modified": False}
    ok = doc.save(desc)
    after = before
    for _ in range(int(float(args.get("wait_s", 30)) / 0.25)):
        adsk.doEvents()
        time.sleep(0.25)
        with contextlib.suppress(Exception):
            after = doc.dataFile.versionNumber
        if before is None or (after and after > before):
            break
    return {"ok": bool(ok), "document": doc.name, "version_before": before, "version_after": after,
            "modified": doc.isModified}


def op_close(args):
    """Close an open library document (needed before ADDing parts from it)."""
    name = args.get("name")
    cand = [d for d in _docs() if _kind_of(d) == "library" and d.name == name]
    if not cand:
        raise BridgeError("not_open", f"no open library named {name!r}")
    if cand[0].isModified and not args.get("discard"):
        raise BridgeError("unsaved", f"library {name!r} has unsaved changes; save first")
    cand[0].close(False)
    _settle(10)
    return op_context({})


def _workspace_commands():
    """Definitions behind visible controls in the current Fusion workspace."""
    def commands(controls):
        for i in range(controls.count):
            control = controls.item(i)
            if not control.isVisible:
                continue
            definition = getattr(control, "commandDefinition", None)
            if definition is not None:
                yield definition
            children = getattr(control, "controls", None)
            if children is not None:
                yield from commands(children)

    panels = _ui.activeWorkspace.toolbarPanels
    for i in range(panels.count):
        panel = panels.item(i)
        if panel.isVisible:
            yield from commands(panel.controls)


def _workspace_command(label, command_id=None):
    matches = {}
    for cd in _workspace_commands():
        if not cd.controlDefinition.isEnabled:
            continue
        if cd.id == command_id:
            return cd
        if cd.name == label:
            matches[cd.id] = cd
    if len(matches) != 1:
        raise BridgeError("unsupported", f"no unique enabled {label!r} control in the active Fusion workspace")
    return next(iter(matches.values()))


def op_command_capabilities(args):
    """Read-only inventory of relevant visible workspace command IDs."""
    commands = {cd.id: {"id": cd.id, "name": cd.name, "enabled": cd.controlDefinition.isEnabled}
                for cd in _workspace_commands()
                if any(term in cd.name.casefold() for term in ("schematic", "pcb", "3d"))}
    def info(doc):
        return {"name": doc.name, "data_file_id": _file_id(doc)} if doc is not None else None

    docs = _docs()
    family = []
    for doc in docs:
        kind = _kind_of(doc)
        if kind not in _DESIGN_PRODUCTS:
            continue
        row = {"name": doc.name, "kind": kind, "references": {}}
        for field in ("documentReferences", "childReferences"):
            list(_reference_files(doc, field, evidence=row["references"]))
        try:
            design = _family_design(doc)
            parent = _product_document(design, "design", docs)
            row.update(document=info(doc), parent=info(parent) if parent is not None else
                       {"name": design.name, "data_file_id": None} if design is not None else None,
                       board=info(_family_peer(doc, "board", docs)),
                       schematic=info(_family_peer(doc, "schematic", docs)))
        except Exception as exc:
            row["error"] = str(exc)
        family.append(row)
    pull = _ui.commandDefinitions.itemById("PullPCB3DCmd")
    pull_capability = {"id": "PullPCB3DCmd", "present": pull is not None}
    if pull is not None:
        pull_capability.update(name=pull.name, enabled=pull.controlDefinition.isEnabled)
    return {"editor": _product_kind(), "commands": sorted(commands.values(), key=lambda cd: cd["id"]),
            "electronics_documents": family, "pull_command": pull_capability}


def op_new_design(args):
    """Create the schematic and PCB from the new Electronics design's own toolbar."""
    command = _ui.commandDefinitions.itemById("NewElectronDesignDocumentCommand")
    if command is None:
        raise BridgeError("unsupported", "this Fusion build has no New Electronics Design command")
    before = _docs()
    command.execute()
    _settle(40)
    doc = _app.activeDocument
    if doc is None or doc in before or _kind_of(doc) != "design":
        raise BridgeError("unsupported", "Fusion did not activate a new Electronics design")
    family = [doc]
    _created_families.append(family)
    design = _document_product(doc, "design")
    if design is None:
        raise BridgeError("unsupported", "Fusion did not expose the new Electronics design; it is still open and unsaved")

    def new_children(kind, existing):
        return [child for child in _docs() if _kind_of(child) == kind
                and not any(_same_document(child, old) for old in existing)]

    observed = []
    for kind, label in (("schematic", "New Schematic"), ("board", "New PCB")):
        children = new_children(kind, before)
        if not children:
            if not _same_document(_app.activeDocument, doc):
                doc.activate()
                _settle(20)
            if not _same_document(_app.activeDocument, doc):
                raise BridgeError("unsupported", "cannot activate the new Electronics design")
            try:
                child_command = _workspace_command(label)
            except BridgeError as ex:
                raise BridgeError(ex.code, f"{ex}. The new Electronics design is still open and unsaved.") from None
            existing = _docs()
            child_command.execute()
            _settle(40)
            children = new_children(kind, existing)
        if len(children) != 1:
            raise BridgeError("unsupported", f"Fusion did not open exactly one new {kind}; the new design is still open and unsaved")
        observed.append(children[0])
    sch, board = design.schematic, design.board
    if (sch is None or board is None or sch.parentDesign is None or board.parentDesign is None
            or sch.linkedBoard is None or board.linkedSchematic is None):
        raise BridgeError("unsupported", "Fusion did not verify a linked schematic and PCB; the new design is still open and unsaved")
    family.extend(observed)
    if args.get("name"):
        if not doc.saveAs(args["name"], _data_folder(args.get("folder")),
                          "created by fusion-electronics-mcp", ""):
            raise BridgeError("save_failed", "Fusion did not save the new Electronics design")
        _settle(40)
    return op_context({})


def _pair_support():
    global _pair_import_module
    if _pair_import_module is None:
        spec = importlib.util.spec_from_file_location('femcp_eagle_import', os.path.join(os.path.dirname(__file__), 'eagle_import.py'))
        _pair_import_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_pair_import_module)
    return _pair_import_module


def _import_document_state():
    return [{k:v for k,v in _doc_info(doc).items() if k != 'active'} for doc in _docs()]


def _import_preserved(record):
    before_ids = {row['document_id'] for row in record['before']}
    if [row for row in _import_document_state() if row['document_id'] in before_ids] != record['before']:
        raise ValueError('Protected original documents changed; import save is not verified')
    return before_ids


def _import_source(record):
    support = _pair_support()
    support.read_pair(record['args'])
    support.read_pair(dict(record['args'], schematic_path=record['public']['staged']['schematic'],
                           board_path=record['public']['staged']['board']))


def _import_update(record, **values):
    with _pair_import_lock:
        record['public'].update(values)


def _pair_import_pending():
    with _pair_import_lock:
        return any(r['public']['status'] in _PAIR_PENDING for r in _pair_import_records.values())


def op_eagle_pair_import_status(args):
    rid = _pair_support().request_id(args.get('request_id'))
    with _pair_import_lock:
        if rid not in _pair_import_records:
            raise ValueError('Unknown import request in this add-in session; inspect documents before any retry')
        return json.loads(json.dumps(_pair_import_records[rid]['public']))


def _import_destination(args):
    project = _app.data.activeProject
    if project is None or not args.get('project_id') or project.id != args['project_id']:
        raise ValueError('The explicit destination project must be the active existing project')
    folder = project.rootFolder
    path = args.get('folder','')
    if not isinstance(path,str) or '\\' in path or (path and any(p in ('','.', '..') for p in path.split('/'))):
        raise ValueError('Use an existing project-relative folder path without traversal')
    for segment in path.split('/') if path else []:
        folder = folder.dataFolders.itemByName(segment)
        if folder is None: raise ValueError('Destination folder does not exist; no folders are created')
    name = args.get('name')
    if (not isinstance(name,str) or not name.strip() or len(name)>128
            or any(c in name for c in '/\\') or any(ord(c)<32 for c in name)):
        raise ValueError('Use an explicit new product name without separators or control characters')
    if folder.dataFiles.itemByName(name) or folder.dataFiles.itemByName(name+'.fprj'):
        raise ValueError('Destination product name already exists; choose a new target')
    return folder


def op_start_eagle_pair_import(args):
    support = _pair_support(); rid = support.request_id(args.get('request_id'))
    if rid in _pair_import_records:
        if _pair_import_records[rid]['args'] != args: raise ValueError('Import request ID cannot be rebound')
        return op_eagle_pair_import_status({'request_id':rid})
    if _pair_import_pending():
        raise ValueError('An import is pending; inspect its status and finish it before starting another')
    before = _import_document_state()
    if any(not row['saved'] or row['modified'] for row in before):
        raise ValueError('All open documents must be saved and unmodified before import')
    folder = _import_destination(args)
    command = _ui.commandDefinitions.itemById('ImportSCHAndBRDCmd')
    if command is None or not command.controlDefinition.isEnabled:
        raise ValueError('Native paired-EAGLE importer is unavailable in this context')
    raw, expected = support.read_pair(args)
    os.makedirs(WORK_DIR, exist_ok=True)
    staged = tempfile.mkdtemp(prefix='eagle-pair-', dir=WORK_DIR)
    paths = {kind:os.path.join(staged, os.path.basename(args[key]))
             for kind,key in (('schematic','schematic_path'),('board','board_path'))}
    for path,data in zip(paths.values(),raw):
        with open(path,'xb') as stream: stream.write(data)
    record = {'args':dict(args), 'before':before, 'expected':expected, 'folder':folder,
              'public':{'request_id':rid, 'session_id':_events.session_id, 'status':'queued',
                        'staged':paths, 'expected':support.summary(expected),
                        'destination':{'project_id':args['project_id'], 'folder_id':folder.id,
                                       'folder':args.get('folder',''), 'name':args['name']},
                        'next_action':'Select the staged schematic, then board, in Fusion native import dialogs; then call finish_eagle_pair_import. Command start is not import completion.'}}
    with _pair_import_lock: _pair_import_records[rid] = record
    # Return the ticket before the importer can block Fusion's main thread in a file dialog.
    def schedule():
        try:
            if _app.fireCustomEvent(EVENT_ID, 'eagle-pair:'+rid) is False:
                raise ValueError('Fusion did not accept the deferred import event')
        except Exception as ex:
            _import_update(record, status='not_started', error=str(ex))
    timer = threading.Timer(0.5, schedule)
    timer.daemon = True; timer.start()
    return op_eagle_pair_import_status({'request_id':rid})


def _launch_eagle_pair_import(rid):
    record = _pair_import_records.get(rid)
    if record is None or record['public']['status'] != 'queued': return
    attempted = False
    try:
        if _import_document_state() != record['before']:
            raise ValueError('Protected document state changed before import; command was not started')
        _import_destination(record['args'])
        _import_source(record)
        _import_update(record, status='awaiting_file_selection')
        command = _ui.commandDefinitions.itemById('ImportSCHAndBRDCmd')
        attempted = True
        if command is None or command.execute() is False:
            raise ValueError('Fusion refused to start the native paired importer')
    except Exception as ex:
        _import_update(record, status='state_unknown' if attempted else 'not_started', error=str(ex))


def op_finish_eagle_pair_import(args):
    rid = _pair_support().request_id(args.get('request_id'))
    status = op_eagle_pair_import_status(args)
    if status['status'] in ('saved_linked','not_started'): return status
    record = _pair_import_records[rid]
    if status['status'] == 'queued': return status
    try:
        _import_update(record, status='verifying')
        _import_source(record)
        before_ids = _import_preserved(record)
        created = [d for d in _docs() if _doc_info(d)['document_id'] not in before_ids]
        designs = [d for d in created if _kind_of(d)=='design']
        if len(designs)!=1 or len([d for d in created if _kind_of(d) in _DESIGN_PRODUCTS])!=3:
            raise ValueError('Expected exactly one new Electronics Design, schematic and board; import is not verified')
        doc = designs[0]; design = _document_product(doc,'design')
        sch,brd = design.schematic,design.board
        sch_doc = _product_document(sch,'schematic',created); brd_doc = _product_document(brd,'board',created)
        if (sch is None or brd is None or sch_doc is None or brd_doc is None
                or sch.parentDesign != design or brd.parentDesign != design
                or sch.linkedBoard != brd or brd.linkedSchematic != sch):
            raise ValueError('New schematic/board reciprocal family links are not verified')
        exports = {kind:_export_product(prod,kind) for kind,prod in (('schematic',sch),('board',brd))}
        observed = _pair_support().readback(*(exports[k]['path'] for k in ('schematic','board')))
        if observed != record['expected']: raise ValueError('Native part/sheet/contact read-back differs from the pinned source pair')
        destination = _import_destination(record['args']) if not doc.isSaved else record['folder']
        if destination.id != record['folder'].id: raise ValueError('Destination folder identity changed')
        if not doc.isSaved:
            if record.get('save_requested'):
                raise ValueError('A save was already requested; reconcile its native/cloud identity before continuing. SaveAs will not be submitted twice.')
            record['save_requested'] = True
            _import_update(record, save_requested=True)
            if not doc.saveAs(record['args']['name'], destination, 'Imported pinned EAGLE schematic/board pair', ''):
                raise ValueError('Fusion did not confirm save; inspect this request before retrying')
            _settle(40)
        family = (doc,sch_doc,brd_doc)
        if any(not d.isSaved or d.isModified or d.dataFile is None for d in family):
            raise ValueError('New family save is not yet verified; finish this same request again after saving settles')
        if any(d.dataFile.parentProject.id != record['args']['project_id'] for d in family):
            raise ValueError('Saved family project identity differs')
        if doc.dataFile.parentFolder.id != record['folder'].id or doc.dataFile.name != record['args']['name']:
            raise ValueError('Saved Electronics Design destination/name differs')
        _import_preserved(record)
        _import_update(record, status='saved_linked', documents=[_doc_info(d) for d in family],
                       exports=exports, verification='Reciprocal native family links, part names/counts, sheets and complete physical net/contact mapping. Footprint geometry and manufacturing acceptance require independent review.',
                       next_action='Independently inspect the exports and reopen the exact saved family IDs before product acceptance.')
    except Exception as ex:
        _import_update(record, status='state_unknown', error=str(ex), next_action='Inspect native dialogs and documents; do not start a second import. Retry finish only for this same request after resolving its state.')
    return op_eagle_pair_import_status({'request_id':rid})


def op_new_library(args):
    name, folder = args.get("name"), args.get("folder")
    if name is not None and (not isinstance(name, str) or not name.strip()
                             or any(c in name for c in '/\\') or any(ord(c) < 32 for c in name)):
        raise BridgeError("bad_args", "name must be a nonempty library name without path separators or control characters")
    if folder is not None and name is None:
        raise BridgeError("bad_args", "folder requires a name so the new library can be saved")
    command = _ui.commandDefinitions.itemById("NewElectronLbrDocumentCommand")
    if command is None or not command.controlDefinition.isEnabled:
        raise BridgeError("unsupported", "this Fusion context has no enabled New Electronics Library command")
    project = _app.data.activeProject if name else None
    target = _data_folder(_norm_folder(folder)) if name else None
    before = _docs()
    command.execute()
    _settle(40)
    doc = _app.activeDocument
    if doc is None or any(_same_document(doc, old) for old in before) or _kind_of(doc) != "library":
        raise BridgeError("state_unknown", "Fusion did not activate a verifiable new library; inspect get_context before retrying")
    if name:
        try:
            saved = doc.saveAs(name, target, "created by fusion-electronics-mcp", "")
        except Exception as exc:
            raise BridgeError("state_unknown", f"new library save raised {exc}; inspect get_context before retrying") from None
        if not saved:
            raise BridgeError("save_failed", "Fusion did not save the new library; it remains open, inspect get_context before retrying")
        _settle(40)
        if not doc.isSaved or not _file_matches(doc.dataFile, project, _norm_folder(folder), name, "flbr"):
            raise BridgeError("state_unknown", "new library save identity was not verified; it remains open, inspect get_context before retrying")
        _remember_folder(project.name, name, "flbr", _norm_folder(folder), doc.dataFile.id, project.id)
    return op_context({})


def _project_folder(folder=None, project=None):
    """Resolve one folder path without enumerating siblings or walking the project tree."""
    proj = _app.data.activeProject
    if project and project != proj.name:
        raise BridgeError("unsupported", "only the active project can be listed: walking another project's "
                          "folders froze Fusion (2705.1.15); open that project in Fusion first")
    node = proj.rootFolder
    path = _norm_folder(folder)
    for part in path.split("/") if path else []:
        node = node.dataFolders.itemByName(part)
        if node is None:
            raise BridgeError("not_found", f"no folder {folder!r} in project {proj.name!r}")
    return proj, node, path


def _project_designs(folder=None, project=None, budget_s=8.0):
    """Complete name lookup for open_design; partial searches must never select an ambiguous file."""
    started = time.monotonic()
    proj, node, path = _project_folder(folder, project)
    out = []
    for f in node.dataFiles.asArray():
        if f.fileExtension in ("fprj", "flbr"):
            out.append((path, f))
        if time.monotonic() - started > budget_s:
            raise BridgeError("timeout", f"listing project {proj.name!r} took too long; use list_designs pages, "
                              "then open with the returned data_file_id")
    return proj, out


def op_list_designs(args):
    limit, offset = args.get("limit", 25), args.get("offset", 0)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise BridgeError("bad_args", "limit must be an integer in 1..100 and offset a nonnegative integer")
    started = time.monotonic()
    proj, node, path = _project_folder(args.get("folder"), args.get("project"))
    # One native snapshot avoids a cloud collection lookup for every file.
    files = node.dataFiles.asArray()
    result = {"project": proj.name, "project_id": proj.id, "folder": path,
              "designs": [], "libraries": [], "total_files": len(files), "offset": offset}
    end = min(offset, len(files))
    for i in range(offset, min(offset + limit, len(files))):
        f = files[i]
        ext = f.fileExtension
        if ext in ("fprj", "flbr"):
            result["designs" if ext == "fprj" else "libraries"].append(
                {"name": f.name, "folder": path, "version": f.versionNumber, "data_file_id": f.id})
        end = i + 1
        # Native calls cannot be interrupted; always return at least one file's progress.
        if time.monotonic() - started > 8.0:
            break
    result.update(complete=end == len(files), next_offset=end if end < len(files) else None)
    return result


def op_find_documents(args):
    """Project names in the active hub (one call). Listing the folders of a project that is
    not the active one froze Fusion twice on 2705.1.15 (2026-10-03: the cloud fetch never
    completes while a script holds the main thread), so this never walks projects; open
    libraries from other projects in Fusion by hand."""
    hub = _app.data.activeHub
    return {"hub": hub.name, "projects": [hub.dataProjects.item(i).name for i in range(hub.dataProjects.count)]}


def _norm_folder(path):
    return "/".join(p for p in str(path or "").replace("\\", "/").split("/") if p)


def _folders():
    try:
        with open(FOLDERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_folder(project, name, ext, folder, file_id=None, project_id=None):
    """Note the folder a design was found in (best effort: never fails the call)."""
    if folder is None or not project or not name:
        return
    data = _folders()
    key = f"{project_id or project}|{ext}|{name}"
    previous = data.get(key)
    if not file_id and isinstance(previous, dict) and previous.get("folder") == folder:
        return
    value = {"folder": folder, "file_id": file_id, "project": project} if file_id or project_id else folder
    if previous == value:
        return
    data[key] = value
    with contextlib.suppress(OSError):
        os.makedirs(INFO_DIR, exist_ok=True)
        tmp = FOLDERS_PATH + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, FOLDERS_PATH)


def _record_folder(value):
    return value.get("folder") if isinstance(value, dict) else value


def _known_folders(project):
    return sorted({folder for k, v in _folders().items() if k.startswith(f"{project}|")
                   and isinstance(folder := _record_folder(v), str)})


def _find_project_file(file_id, project, folder, name, ext):
    if file_id and project:
        with contextlib.suppress(Exception):
            f = _app.data.findFileById(file_id)
            if _file_matches(f, project, folder, name, ext):
                return _file_folder(f), f
    return None


def _file_matches(f, project, folder, name, ext):
    return (f and project and f.name == name and f.fileExtension == ext
            and f.parentProject.id == project.id
            and _file_folder(f) is not None
            and (folder is None or _norm_folder(folder) == _file_folder(f)))


def _file_folder(data_file):
    try:
        node = data_file.parentFolder
        parts = []
        while node is not None and not getattr(node, "isRoot", False):
            parts.append(node.name)
            node = node.parentFolder
        return "/".join(reversed(parts))
    except Exception:
        return None


def op_open_design(args):
    """Bring a design (or library) forward, opening it from the project only when none of its
    documents is open. Fusion froze on 2026-10-07 during open_design while another design had
    unsaved changes; re-opening a file that is already open (only its board or schematic tab,
    without the design overview) can raise a modal prompt that blocks Fusion's main thread.
    So: any open document of the design counts as open and is activated, and opening from the
    project is refused while an open Electronics document has unsaved changes, unless
    allow_unsaved is set."""
    name = args.get("name")
    folder = args.get("folder")
    ext = "flbr" if args.get("kind") == "library" else "fprj"
    kinds = ("library",) if ext == "flbr" else ("design", "board", "schematic")
    active_project = getattr(getattr(_app, "data", None), "activeProject", None)
    project = active_project.name if active_project else None
    project_id = getattr(active_project, "id", None)
    if args.get("project") and args["project"] != project:
        raise BridgeError("unsupported", "make the requested project active in Fusion before opening its files")
    file_id = args.get("data_file_id")
    docs = [doc for doc in _docs() if _kind_of(doc) in kinds]
    families = list(_family_keys(docs))
    roots = []
    unknown_folder = False
    for doc, key in families:
        root, design = key
        if (any(_same_family(key, r) for r in roots)
                or name not in (doc.name, root.name, getattr(design, "name", None))):
            continue
        f = getattr(root, "dataFile", None)
        if file_id:
            if getattr(f, "id", None) != file_id or not _file_matches(f, active_project, folder, name, ext):
                continue
        elif f and project_id and f.parentProject.id != project_id:
            continue
        where = _file_folder(f)
        if folder is not None and where != _norm_folder(folder):
            unknown_folder |= where is None
            continue
        roots.append(key)
    if len(roots) > 1 or (not roots and unknown_folder):
        raise BridgeError("ambiguous", f"cannot identify one open Electronics document named {name!r}; "
                          "pass folder= or data_file_id=, or switch to it in Fusion")
    if roots:
        root = roots[0][0]
        f = getattr(root, "dataFile", None)
        owner = getattr(f, "parentProject", None)
        _remember_folder(getattr(owner, "name", project), root.name, ext, _file_folder(f),
                         getattr(f, "id", None), getattr(owner, "id", project_id))
        doc = min((d for d, key in families if _same_family(key, roots[0])),
                  key=lambda d: kinds.index(_kind_of(d)))
        if not doc.isActive:
            doc.activate()
            _settle(20)
            if not doc.isActive and getattr(_app, "activeDocument", None) != doc:
                raise BridgeError("activate_failed", f"Fusion did not activate {doc.name!r}")
        res = op_context({})
        res["already_open"] = True
        return res
    unsaved = sorted({d.name for d in _docs() if _kind_of(d) != "other" and d.isModified})
    if unsaved and not args.get("allow_unsaved"):
        raise BridgeError("unsaved", f"{', '.join(repr(u) for u in unsaved)} has unsaved changes. Opening another "
                          "design from the project while one has unsaved changes froze Fusion once; save it "
                          "first (save_design), open the design in Fusion by hand, or pass allow_unsaved=true.")
    folders = _folders()
    record = folders.get(f"{project_id}|{ext}|{name}", folders.get(f"{project}|{ext}|{name}"))
    direct = None
    if file_id:
        direct = _find_project_file(file_id, active_project, folder, name, ext)
        if direct is None:
            raise BridgeError("not_found", f"no file with the supplied ID matches {name!r} in the active project and folder")
    elif isinstance(record, dict):
        last = record.get("folder")
        if isinstance(last, str) and (folder is None or _norm_folder(folder) == last):
            direct = _find_project_file(record.get("file_id"), active_project, last, name, ext)
    cand = [direct] if direct else []
    if not cand and folder is None:                         # name only: try the folder it was found in last time
        if record is None:
            elsewhere = sorted({
                value.get("project") or key.split("|", 1)[0] if isinstance(value, dict) else key.split("|", 1)[0]
                for key, value in folders.items() if key.endswith(f"|{ext}|{name}")
            } - {project})
            if elsewhere:
                raise BridgeError("not_found", f"{name!r} was last seen in project {', '.join(map(repr, elsewhere))}, "
                                  f"not the active project {project!r}. Make that project active in Fusion first.")
        last = _record_folder(record)
        if last is not None:
            with contextlib.suppress(BridgeError):
                _, found = _project_designs(folder=last, project=args.get("project"))
                cand = [(p, f) for p, f in found if f.name == name and f.fileExtension == ext]
    if not cand:
        try:
            proj, found = _project_designs(folder=folder, project=args.get("project"))
        except BridgeError as ex:
            known = sorted(set(_known_folders(project_id) + _known_folders(project)))
            if folder is None and known and ex.code in ("timeout", "not_found"):
                raise BridgeError(ex.code, f"{ex}. Pass folder=; designs have been found in "
                                  + ", ".join(repr(k or "top folder") for k in known)) from None
            raise
        cand = [(p, f) for p, f in found if f.name == name and f.fileExtension == ext]
    if not cand:
        known = sorted(set(_known_folders(project_id) + _known_folders(project)) - {_norm_folder(folder)})
        raise BridgeError("not_found", f"no {'library' if ext == 'flbr' else 'design'} {name!r} in "
                          f"{folder or 'the top folder'} of the active project {project!r}; pass folder= "
                          "(it is not searched recursively), or open it in Fusion by hand"
                          + (f". Designs have been found in {', '.join(repr(k or 'top folder') for k in known)}"
                             if known else ""))
    if len(cand) > 1:
        raise BridgeError("ambiguous", f"more than one file matches {name!r}; use list_designs and pass data_file_id")
    path, file = cand[0]
    _remember_folder(project, name, ext, path, getattr(file, "id", None), project_id)
    _app.documents.open(file)
    _settle(40)
    return op_context({})


def op_close_design(args):
    """Close the actual linked Electronics document family."""
    name = args.get("name")
    open_docs = [d for d in _docs() if _kind_of(d) in _DESIGN_PRODUCTS]
    families = list(_family_keys(open_docs))
    roots = []
    for doc, key in families:
        root, design = key
        if name in (doc.name, root.name, getattr(design, "name", None)) and not any(_same_family(key, r) for r in roots):
            roots.append(key)
    if not roots:
        raise BridgeError("not_open", f"design {name!r} is not open")
    if len(roots) != 1:
        raise BridgeError("ambiguous", f"more than one open Electronics design matches {name!r}")
    docs = [doc for doc, key in families if _same_family(key, roots[0])]
    design = roots[0][1]
    kinds = {_kind_of(doc) for doc in docs}
    if len(kinds) != len(docs):
        raise BridgeError("ambiguous", f"more than one revision belongs to {name!r}; no documents were closed")
    if design is not None:
        created = next((members for parent, *members in _created_families
                        if _same_document(parent, roots[0][0])), None)
        for kind in ("board", "schematic"):
            if kind in kinds:
                continue
            try:
                linked = getattr(design, kind, None)
            except RuntimeError as exc:
                # A missing child link can throw even on an otherwise valid empty design.
                missing_link = str(exc) in (f"InternalValidationError: {kind}Link",
                                            "InternalValidationError: pcbLink" if kind == "board" else "")
                if created is not None and missing_link and not any(_kind_of(child) == kind for child in created):
                    continue
                raise
            if linked is not None:
                raise BridgeError("unsupported", f"cannot verify every linked child of {name!r}; no documents were closed")
    if any(d.isModified for d in docs) and not args.get("discard_changes"):
        raise BridgeError("unsaved", f"{name!r} has unsaved changes; save first or pass discard_changes")
    order = ("board", "schematic", "design")
    for doc in sorted(docs, key=lambda d: order.index(_kind_of(d))):
        current = next((d for d in _docs() if _same_document(d, doc)), None)
        if current is not None:
            if current.isModified and not args.get("discard_changes"):
                raise BridgeError("unsaved", f"{current.name!r} has unsaved changes. The family close stopped "
                                  "and earlier tabs may already be closed.")
            if not current.close(False):
                raise BridgeError("close_failed", f"Fusion could not close {doc.name!r}")
            _settle(5)
    return op_context({})


def _data_folder(path):
    """DataFolder in the active project for a 'A/B' path, created if missing."""
    folder = _app.data.activeProject.rootFolder
    for part in [x for x in (path or "").split("/") if x]:
        nxt = folder.dataFolders.itemByName(part)
        folder = nxt or folder.dataFolders.add(part)
    return folder


def _bbox_mm(b):
    return [round(b.minPoint.x * 10, 3), round(b.minPoint.y * 10, 3), round(b.minPoint.z * 10, 3),
            round(b.maxPoint.x * 10, 3), round(b.maxPoint.y * 10, 3), round(b.maxPoint.z * 10, 3)]


def op_create_package3d(args):
    """Give a library package a 3D model from a STEP file, the way Fusion's UI
    does it (verified on 2705.1.15): Electron.Create3DPackage <footprint xml>
    opens a Package3D document with the footprint as sketches; the STEP is
    imported into it and placed; the document is saved through the API (so
    Finish does not stop on 'Unsaved changes' and a Save form); then Finish
    (Package3DStop) links it to the package in the open library."""
    import math
    import xml.etree.ElementTree as ET
    import adsk.fusion  # type: ignore
    pkg_name = args["package"]
    step = args["step_path"]
    if not os.path.exists(step):
        raise BridgeError("not_found", f"STEP file not found: {step}")
    lib = _active_product("library")
    os.makedirs(WORK_DIR, exist_ok=True)
    lbr = os.path.join(WORK_DIR, f"lib-{uuid.uuid4().hex}.lbr")
    em = lib.exportManager
    em.execute(em.createEagleLbrExportOptions(lbr))
    root = ET.parse(lbr).getroot()
    pk = next((x for x in root.iter("package") if x.get("name") == pkg_name), None)
    if pk is None:
        raise BridgeError("not_found", f"package {pkg_name!r} is not in the open library")
    fx = os.path.join(WORK_DIR, f"fp-{uuid.uuid4().hex}.xml")
    ET.ElementTree(pk).write(fx, encoding="utf-8", xml_declaration=True)
    lib_doc = _app.activeDocument
    _app.executeTextCommand(f"Electron.Create3DPackage {fx}")
    _settle(30)
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise BridgeError("package3d_failed", "Fusion did not open a 3D package document")
    doc3d = _app.activeDocument
    rc = design.rootComponent
    im = _app.importManager
    n_before = rc.occurrences.count
    if not im.importToTarget(im.createSTEPImportOptions(step), rc) or rc.occurrences.count <= n_before:
        raise BridgeError("import_failed", f"STEP import failed: {step}")
    occ = rc.occurrences.item(rc.occurrences.count - 1)
    m = adsk.core.Matrix3D.create()
    origin = adsk.core.Point3D.create(0, 0, 0)
    for axis, deg in zip(((1, 0, 0), (0, 1, 0), (0, 0, 1)), args.get("rotation_deg") or (0, 0, 0)):
        if deg:
            r = adsk.core.Matrix3D.create()
            r.setToRotation(math.radians(deg), adsk.core.Vector3D.create(*axis), origin)
            m.transformBy(r)
    off = args.get("offset_mm") or (0, 0, 0)
    t = adsk.core.Matrix3D.create()
    t.translation = adsk.core.Vector3D.create(off[0] / 10, off[1] / 10, off[2] / 10)
    m.transformBy(t)
    occ.transform = m
    with contextlib.suppress(Exception):
        if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            design.snapshots.add()
    model_bb = _bbox_mm(occ.boundingBox)
    pad_bb = None
    with contextlib.suppress(Exception):
        pad_bb = _bbox_mm(rc.sketches.itemByName("Pad").boundingBox)
    # more of the model below the board than above it = upside down (a flipped through-hole part
    # still pokes its pins up through the board, so "top above zero" is not enough)
    if args.get("require_up", True) and model_bb and (model_bb[5] <= 0.2 or -model_bb[2] > model_bb[5]):
        # the body would hang below the board (a model in another up-axis convention, or a rotation
        # with the wrong sign): never save it; once a design has used a 3D package it is very hard
        # to replace (seen with vendor connector models)
        with contextlib.suppress(Exception):
            doc3d.close(False)
        with contextlib.suppress(Exception):
            lib_doc.activate()
        raise BridgeError("model_below_board",
                          f"the model spans z {model_bb[2]} to {model_bb[5]} mm: mostly below the board, upside down; fix rotation_deg "
                          f"(KiCad's 3D rotation signs are the opposite of Fusion's) and try again. Model box {model_bb}")
    folder = _data_folder(args.get("folder") or "3D Packages")
    # a new name when replacing a model: a design matches 3D packages by name, so a corrected
    # model saved under the old name never reaches boards that already use that name
    doc3d.saveAs(args.get("doc_name") or pkg_name, folder, "3D package created by fusion-electronics-mcp", "")
    for _ in range(120):
        adsk.doEvents()
        time.sleep(0.25)
        with contextlib.suppress(Exception):
            if doc3d.isSaved and not doc3d.isModified:
                break
    _ui.commandDefinitions.itemById("Package3DStop").execute()
    _settle(40)
    with contextlib.suppress(Exception):
        lib_doc.activate()
        _settle(10)
    lib = _active_product("library")
    linked = []
    for i in range(lib.deviceSets.count):
        ds = lib.deviceSets.item(i)
        for j in range(ds.devices.count):
            dv = ds.devices.item(j)
            with contextlib.suppress(Exception):
                if dv.package and dv.package.name == pkg_name:
                    linked.append({"device": ds.name + dv.name,
                                   "packages3d": [dv.packages3d.item(k).name for k in range(dv.packages3d.count)]})
    for f in (lbr, fx):
        with contextlib.suppress(Exception):
            os.remove(f)
    return {"package": pkg_name, "model_bbox_mm": model_bb, "pad_bbox_mm": pad_bb,
            "saved_as": f"{args.get('folder') or '3D Packages'}/{args.get('doc_name') or pkg_name}", "devices": linked}


def op_elements3d(args):
    """Read-only: the 3D package each board element uses (None = no 3D model), optionally
    only `names`. locally_modified 3D packages are skipped by Fusion's library update."""
    board = _linked("board")
    names = set(args.get("names") or [])
    out = []
    els = board.elements
    for i in range(els.count):
        e = els.item(i)
        if names and e.name not in names:
            continue
        p3 = None
        with contextlib.suppress(Exception):
            p3 = e.package3d
        out.append({"name": e.name, "package3d": None if p3 is None else _safe(lambda: p3.name),
                    "id": None if p3 is None else _safe(lambda: p3.id),
                    "locally_modified": None if p3 is None else _safe(lambda: p3.locallyModified)})
    return {"elements": out}


def op_lib_device3d(args):
    """Read-only: in the open library, the 3D packages each device links (devices whose package
    name contains `match`, or all)."""
    lib = _active_product("library")
    match = (args.get("match") or "").upper()
    out = []
    for i in range(lib.deviceSets.count):
        ds = lib.deviceSets.item(i)
        for j in range(ds.devices.count):
            d = ds.devices.item(j)
            with contextlib.suppress(Exception):
                if d.package and (not match or match in d.package.name.upper()):
                    out.append({"device": ds.name + d.name, "package": d.package.name,
                                "packages3d": [d.packages3d.item(k).name for k in range(d.packages3d.count)]})
    return {"devices": out}


_pcb3d_links = {}


def _pcb3d_product(doc):
    import adsk.fusion  # type: ignore
    for i in range(doc.products.count):
        design = adsk.fusion.Design.cast(doc.products.item(i))
        if design and design.rootComponent.allOccurrences.count:
            return design
    return None


def _pcb3d_link():
    active = _app.activeDocument
    parent = _family_design(active)
    docs = _docs()
    for board, pcb in _pcb3d_links.values():
        if board not in docs or pcb not in docs:
            continue
        if (_same_document(active, board) or _same_document(active, pcb)
                or (parent is not None and parent == _family_design(board))):
            return board, pcb
    mechanical = [doc for doc in docs if _kind_of(doc) == "other" and getattr(doc, "dataFile", None) is not None]
    matches = []
    if mechanical:
        for board in (doc for doc in docs if _kind_of(doc) == "board"):
            board_active = _same_document(active, board) or (parent is not None and parent == _family_design(board))
            candidates = mechanical if board_active else [doc for doc in mechanical if _same_document(active, doc)]
            if not candidates:
                continue
            refs = list(_reference_files(board, "childReferences"))
            matches.extend((board, doc) for doc in candidates
                           if any(_same_file_version(doc.dataFile, file) for _, file in refs)
                           and _pcb3d_product(doc) is not None)
    if len(matches) > 1:
        raise BridgeError("ambiguous", "more than one saved 3D PCB association matches the active document")
    return matches[0] if matches else (None, None)


def op_pcb3d_bodies(args):
    """Read-only: in the open 3D PCB, each board part's height range (mm, z) next to the board's,
    so a model hanging on the wrong side shows up. Parts are found by reference designator at
    any depth (Fusion nests them under 'Packages' in a new 3D PCB, at the top in older ones)."""
    board_doc, doc = _pcb3d_link()
    des = _pcb3d_product(doc) if doc is not None else None
    if des is None:
        raise BridgeError("not_found", "no verified 3D PCB is open for the active design")
    refs = set()
    with contextlib.suppress(Exception):
        els = _document_product(board_doc, "board").elements
        refs = {els.item(i).name for i in range(els.count)}
    root = des.rootComponent
    board = None
    occs = root.allOccurrences
    for i in range(occs.count):                       # a 'Board' occurrence (new 3D PCBs)
        o = occs.item(i)
        if o.name.split(":")[0].lower() == "board":
            bb = o.boundingBox
            board = (round(bb.minPoint.z * 10, 3), round(bb.maxPoint.z * 10, 3))
            break
    if board is None:                                 # else the largest body at the top level
        best = None
        for i in range(root.bRepBodies.count):
            bb = root.bRepBodies.item(i).boundingBox
            area = (bb.maxPoint.x - bb.minPoint.x) * (bb.maxPoint.y - bb.minPoint.y)
            if best is None or area > best[0]:
                best = (area, round(bb.minPoint.z * 10, 3), round(bb.maxPoint.z * 10, 3))
        board = best[1:] if best else None
    parts = []
    for i in range(occs.count):
        o = occs.item(i)
        ref = o.name.rsplit(":", 1)[-1]
        name = o.name.split(":")[0]
        hit = ref in refs or name in refs or any(name.endswith(" " + r) or name.endswith("_" + r) for r in refs)
        if not hit:
            continue
        with contextlib.suppress(Exception):
            bb = o.boundingBox
            parts.append({"occurrence": o.name, "z_mm": [round(bb.minPoint.z * 10, 3), round(bb.maxPoint.z * 10, 3)]})
    return {"document": doc.name, "board_z_mm": None if board is None else list(board), "parts": parts}


def op_push_3d(args):
    """Push a board to 3D or pull changes into its verified 3D PCB."""
    board, doc = _pcb3d_link()
    if doc is None:
        if _product_kind() != "board":
            if _product_kind() not in ("schematic", "design"):
                raise BridgeError("wrong_editor", "activate the linked 2D PCB before pushing to 3D")
            op_activate({"kind": "board"})
        board = _app.activeDocument
        before = _docs()
        _workspace_command("Push to 3D PCB", "Switch3dPcbDocCmd").execute()
        deadline = time.time() + float(args.get("wait_s", 180))
        while time.time() < deadline:
            _settle(20)
            active = _app.activeDocument
            if active is not None and active not in before and _kind_of(active) == "other":
                if _pcb3d_product(active) is not None:
                    doc = active
                    break
        if doc is None:
            raise BridgeError("not_found", "Push did not activate a new 3D PCB and no open 3D PCB matched "
                              "the board's saved references")
        _pcb3d_links[_document_id(board)] = (board, doc)
        _settle(200)
        return op_pcb3d_bodies({})
    doc.activate()
    _settle(10)
    if _app.activeDocument != doc:
        raise BridgeError("activate_failed", "cannot activate the verified 3D PCB")
    cd = next((command for command in _workspace_commands() if command.id == "PullPCB3DCmd"
               and command.controlDefinition.isEnabled), None)
    if cd is None:
        cd = _ui.commandDefinitions.itemById("PullPCB3DCmd")
    if cd is None or not cd.controlDefinition.isEnabled:
        raise BridgeError("unsupported", "no enabled PullPCB3DCmd in the verified 3D PCB workspace")
    cd.execute()
    _settle(int(args.get("settle", 400)))
    return op_pcb3d_bodies({})


def _safe(fn):
    try:
        v = fn()
        return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
    except Exception as ex:
        return f"<{type(ex).__name__}>"


def op_update_libraries(args):
    """Update the active design from all its libraries (Fusion's 'Update all',
    command Electron::UpdateDesignFromAllLibraries; no dialogs seen on
    2705.1.15). Brings library changes such as attributes and 3D packages in."""
    if _product_kind() not in ("board", "schematic", "design"):
        raise BridgeError("wrong_editor", f"the active editor is {_product_kind()}; activate the board or schematic")
    cd = _app.userInterface.commandDefinitions.itemById("Electron::UpdateDesignFromAllLibraries")
    if cd is None:
        raise BridgeError("unsupported", "this Fusion build has no Electron::UpdateDesignFromAllLibraries command")
    cd.execute()
    _settle(int(args.get("settle", 60)))
    return op_context({})


OPS = {"ping": op_ping, "context": op_context, "activate": op_activate, "export": op_export,
       "start_eagle_pair_import": op_start_eagle_pair_import,
       "eagle_pair_import_status": op_eagle_pair_import_status, "finish_eagle_pair_import": op_finish_eagle_pair_import,
       "list_scripts": op_list_scripts, "inspect_script": op_inspect_script, "control_script": op_control_script,
       "events": op_events, "reload_addin": op_reload_addin,
       "selection": op_selection, "commands": op_commands,
       "geometry_bodies": op_geometry_bodies, "geometry_clearance": op_geometry_clearance,
       "run": op_run, "run_script": op_run_script, "run_bundled_ulp": op_run_bundled_ulp,
       "run_registered_ulp": op_run_registered_ulp,
       "errors": op_errors, "save": op_save,
       "close_library": op_close, "list_designs": op_list_designs, "open_design": op_open_design,
       "find_documents": op_find_documents, "new_library": op_new_library,
       "close_design": op_close_design, "design_rules": op_design_rules, "new_design": op_new_design,
       "pours": op_pours, "create_package3d": op_create_package3d,
       "layers": op_layers, "violation_mode": op_violation_mode, "update_libraries": op_update_libraries,
       "elements3d": op_elements3d, "lib_device3d": op_lib_device3d, "pcb3d_bodies": op_pcb3d_bodies,
       "push_3d": op_push_3d, "command_capabilities": op_command_capabilities}


# ---------------------------------------------------------------------------
# transport


class _Handler(adsk.core.CustomEventHandler):
    def notify(self, args):
        jid = args.additionalInfo
        if jid.startswith('eagle-pair:'):
            _launch_eagle_pair_import(jid[len('eagle-pair:'):])
            return
        with _jobs_lock:
            job = _jobs.get(jid)
            if job is None or job["state"] != "queued":
                return
            job["state"] = "running"
        try:
            if (_pair_import_pending()
                    and job['op'] not in ('ping','events','start_eagle_pair_import','finish_eagle_pair_import')):
                raise BridgeError('import_pending','A paired import is pending; inspect its dialogs/status and finish it before CAD edits or reloads')
            fn = OPS[job["op"]]
            job["result"] = {"ok": True, "result": fn(job["args"])}
        except BridgeError as ex:
            job["result"] = {"ok": False, "error": {"code": ex.code, "message": str(ex)}}
        except Exception as ex:
            job["result"] = {"ok": False, "error": {"code": "fusion_error", "message": str(ex)[:500],
                                                    "trace": traceback.format_exc()[-1500:]}}
        job["state"] = "done"
        job["done"].set()


def _reply(conn, obj):
    conn.sendall((json.dumps(obj, default=str) + "\n").encode("utf-8"))


def _serve_conn(conn):
    try:
        conn.settimeout(15)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 4_000_000:
                _reply(conn, {"v": PROTOCOL, "ok": False, "error": {"code": "too_large", "message": "request too large"}})
                return
        req = json.loads(buf.decode("utf-8"))
        rid = req.get("id")
        if not hmac.compare_digest(str(req.get("token", "")), _token):
            _reply(conn, {"v": PROTOCOL, "id": rid, "ok": False, "error": {"code": "unauthorized", "message": "bad token"}})
            return
        op = req.get("op")
        if op not in OPS:
            _reply(conn, {"v": PROTOCOL, "id": rid, "ok": False, "error": {"code": "unknown_op", "message": f"unknown op {op!r}"}})
            return
        if op == 'start_eagle_pair_import' and (req.get('args') or {}).get('request_id') in _pair_import_records:
            # A retry must stay available while the original launch is blocked in a file picker.
            values = req['args']; record = _pair_import_records[values['request_id']]
            if record['args'] != values:
                _reply(conn, {'v':PROTOCOL, 'id':rid, 'ok':False, 'error':{'code':'bad_args','message':'Import request ID cannot be rebound'}})
            else:
                _reply(conn, {'v':PROTOCOL, 'id':rid, 'ok':True, 'result':op_eagle_pair_import_status(values)})
            return
        if _pair_import_pending() and op not in ('events','eagle_pair_import_status','finish_eagle_pair_import','ping'):
            _reply(conn, {'v':PROTOCOL, 'id':rid, 'ok':False, 'error':{'code':'import_pending',
                         'message':'Inspect the pending paired-import status/dialogs and finish the same request before other CAD calls or reloads'}})
            return
        if op in ('events','eagle_pair_import_status'):
            # Cached data only: event inspection must remain available while Fusion's UI is busy.
            try:
                _reply(conn, {'v': PROTOCOL, 'id': rid, 'ok': True, 'result': OPS[op](req.get('args') or {})})
            except (ValueError, BridgeError) as ex:
                _reply(conn, {'v': PROTOCOL, 'id': rid, 'ok': False, 'error': {'code': 'bad_args', 'message': str(ex)}})
            return
        timeout = min(float(req.get("timeout") or DEFAULT_TIMEOUT), 600.0)
        jid = uuid.uuid4().hex
        job = {"op": op, "args": req.get("args") or {}, "done": threading.Event(), "state": "queued", "result": None}
        with _jobs_lock:
            _jobs[jid] = job
        _app.fireCustomEvent(EVENT_ID, jid)
        if job["done"].wait(timeout):
            res = job["result"]
        else:
            with _jobs_lock:
                state = job["state"]
                if state == "queued":
                    job["state"] = "cancelled"
            res = {"ok": False, "error": {"code": "timeout", "message": (
                f"timed out after {timeout}s before Fusion started it (not run)" if state == "queued" else
                f"timed out after {timeout}s while running in Fusion; it may still complete, "
                "check state before retrying (a modal dialog may be open)")}}
        with _jobs_lock:
            _jobs.pop(jid, None)
        res.update({"v": PROTOCOL, "id": rid})
        _reply(conn, res)
    except Exception:
        with contextlib.suppress(Exception):
            _reply(conn, {"v": PROTOCOL, "ok": False, "error": {"code": "bridge_error", "message": traceback.format_exc()[-800:]}})
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _accept(sock):
    sock.settimeout(0.5)
    while not _stop.is_set():
        try:
            conn, _ = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_serve_conn, args=(conn,), daemon=True).start()


# Two copies of the add-in (say the installed one and one added from a source folder) share
# EVENT_ID and bridge.json: the second unregisters the first one's event while its server is
# still listening, and stopping either deletes the other's bridge.json. One copy runs at a time.
_OWNER_ATTR = "_fusion_electronics_mcp_owner"


def _other_copy():
    owner = getattr(adsk.core, _OWNER_ATTR, None)
    return owner if owner and os.path.normcase(owner) != os.path.normcase(os.path.abspath(__file__)) else None


def run(context):
    global _app, _ui, _event, _handler, _server, _token, _events
    reload_ticket = _management().claim_reload(adsk.core, os.path.dirname(__file__))
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    other = _other_copy()
    if other:
        _ui.messageBox("Another copy of the Fusion Electronics MCP add-in is already running:\n" + other
                       + "\n\nStop that one in Scripts and Add-Ins first (and untick its Run on Startup); "
                       "this copy was not started.", "Fusion Electronics MCP")
        return
    try:
        if _events: _events.close()
        _events = _management().EventLog()
        _events.start(_app, adsk.core)
        _events.append('bridge_started', addin_version=ADDIN_VERSION)
        with contextlib.suppress(Exception):
            _app.unregisterCustomEvent(EVENT_ID)
        _event = _app.registerCustomEvent(EVENT_ID)
        _handler = _Handler()
        _event.add(_handler)
        _token = secrets.token_hex(24)
        _stop.clear()
        _server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _server.bind(("127.0.0.1", 0))
        _server.listen(8)
        os.makedirs(INFO_DIR, exist_ok=True)
        info = {"port": _server.getsockname()[1], "token": _token, "pid": os.getpid(),
                       "protocol": PROTOCOL, "addin_version": ADDIN_VERSION,
                       "event_session_id": _events.session_id,
                       "reload_request_id": reload_ticket['request_id'] if reload_ticket else None,
                       "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _management().U.atomic_json(INFO_PATH, info)
        threading.Thread(target=_accept, args=(_server,), daemon=True).start()
        setattr(adsk.core, _OWNER_ATTR, os.path.abspath(__file__))
        if reload_ticket is not None: reload_ticket['bridge_session'] = info
    except Exception:
        if _events: _events.close()
        _ui.messageBox(traceback.format_exc(), "Fusion Electronics MCP failed to start")


def stop(context):
    if _other_copy() or _server is None:      # not the running copy: leave the other one alone
        return
    _stop.set()
    if _events:
        _events.append('bridge_stopping')
        _events.close()
    with contextlib.suppress(Exception):
        _server.close()
    with contextlib.suppress(Exception):
        if _event and _handler:
            _event.remove(_handler)
        _app.unregisterCustomEvent(EVENT_ID)
    with contextlib.suppress(Exception):
        with open(INFO_PATH, encoding="utf-8") as f:
            mine = json.load(f).get("token") == _token
        if mine:
            os.remove(INFO_PATH)
    with contextlib.suppress(Exception):
        delattr(adsk.core, _OWNER_ATTR)
    # Individual operations clean temporary files; retain legacy ULP output across reloads.
