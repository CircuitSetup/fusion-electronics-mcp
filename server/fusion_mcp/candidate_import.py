"""Advance one pinned compiler candidate through the guarded paired importer."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from xml.etree.ElementTree import ParseError

from fusion_offline.compiler.canonical import canonical_digest, canonical_json_bytes
from fusion_offline.compiler.manifest import load_candidate_manifest
from fusion_offline.compiler.model import ROUTING_BUDGET_DEFAULTS, _routing
from fusion_offline.compiler.paths import _read_local_file, atomic_write
from fusion_offline.compiler.validate import validate_candidate

from . import data_dir, eagle_pair_picker as picker
from .addin.FusionElectronicsMCP import eagle_import
from .bridge import BridgeOpError, BridgeUnavailable
from .fusion_lock import FusionBusy


_IS_WINDOWS = sys.platform == "win32"
_PICKER_STATUS = "awaiting_file_selection"
_NO_IMPORT = {"not_started", "cancelled_native_rejection"}
_OBSERVATION_ERRORS = (BridgeOpError, BridgeUnavailable, OSError, RuntimeError, ValueError, KeyError, TypeError)
_INSPECT = ("Inspect the existing ticket with get_eagle_pair_import_status and review Fusion's "
            "native dialogs and documents before using the low-level recovery tools.")
_COMMAND = "ImportSCHAndBRDCmd"


def _validate(manifest_path, manifest_sha256, args):
    candidate = load_candidate_manifest(manifest_path, manifest_sha256)
    data = candidate.data
    if data["status"] != "pass":
        raise ValueError("Candidate manifest must have passing required validation")
    for kind in ("schematic", "board"):
        artifact = data["artifacts"][kind]
        args[f"{kind}_path"] = str(candidate.path.parent / artifact["path"])
        args[f"{kind}_sha256"] = artifact["sha256"]
    raw, signature = eagle_import.read_pair(args)
    intent_bytes = candidate.read_artifact("routing_intent")
    intent = json.loads(intent_bytes)
    required = {"allow_outside", "nets", "routing"}
    optional = {"verify_reference_labels", "placement_grid_exceptions"}
    if (not isinstance(intent, dict) or not required <= intent.keys() <= required | optional
            or canonical_json_bytes(intent) != intent_bytes):
        raise ValueError("Candidate routing intent must be a canonical compiler object")
    nets, exemptions = intent["nets"], intent["allow_outside"]
    if (not isinstance(nets, dict) or any(type(enabled) is not bool for enabled in nets.values())
            or not isinstance(exemptions, list) or any(not isinstance(ref, str) for ref in exemptions)
            or exemptions != sorted(set(exemptions)) or not set(exemptions) <= set(signature["elements"])):
        raise ValueError("Candidate routing intent has invalid nets or placement exemptions")
    exceptions = intent.get("placement_grid_exceptions", {})
    if (type(intent.get("verify_reference_labels", False)) is not bool or not isinstance(exceptions, dict)
            or not set(exceptions) <= set(signature["elements"])
            or any(not isinstance(reason, str) or not reason for reason in exceptions.values())):
        raise ValueError("Candidate routing intent has invalid placement grid metadata")
    _routing(intent["routing"], {net: {"route": enabled} for net, enabled in nets.items()},
             board_nets={net for net, _, _ in signature["contacts"]})
    if intent["routing"]["budget"].keys() != ROUTING_BUDGET_DEFAULTS.keys():
        raise ValueError("Candidate routing intent must pin the complete routing budget")
    provenance = (json.loads(candidate.read_artifact("routing_provenance"))
                  if data["routing_job"] else None)
    if data["routing_job"] and not isinstance(provenance, dict):
        raise ValueError("Candidate routing provenance must be an object")
    report = validate_candidate(*raw, intent, provenance=provenance)
    if report["status"] != "pass":
        failed_attributes = next((row["detail"] for row in report["findings"]
                                  if row["check"] == "element_attributes" and row["status"] != "pass"), "")
        detail = f": {failed_attributes}" if failed_attributes else ""
        raise ValueError(f"Pinned candidate pair and routing intent failed offline revalidation{detail}")
    return dict(manifest_path=str(manifest_path), manifest_sha256=manifest_sha256,
                candidate_id=data["candidate_id"], artifacts=data["artifacts"], args=args,
                expected=eagle_import.summary(signature))


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, canonical_json_bytes(data))


def _read(path):
    value = json.loads(_read_local_file(path, 8 * 1024 * 1024))
    if not isinstance(value, dict):
        raise ValueError("Import journal must be a JSON object")
    return value


def _ticket(bridge, request_id):
    return bridge.call("eagle_pair_import_status", {"request_id": request_id}, timeout=10)


def _identity(record, ticket):
    pins = record["pins"]
    args = pins["args"]
    destination = ticket.get("destination", {})
    if (ticket.get("request_id") != args["request_id"]
            or ticket.get("session_id") != record["session"]["session_id"]
            or ticket.get("expected") != pins["expected"] or not isinstance(destination, dict)
            or any(destination.get(key) != args[key] for key in ("project_id", "folder", "name"))
            or not isinstance(destination.get("folder_id"), str) or not destination["folder_id"]):
        raise ValueError("Native ticket differs from the pinned candidate, session or destination")
    staged = ticket.get("staged", {})
    if (not isinstance(staged, dict) or staged.keys() != {"schematic", "board"}
            or not all(isinstance(path, str) and path for path in staged.values())):
        raise ValueError("Native ticket has no staged pair")
    identity = {"destination": destination, "staged": staged}
    if record.get("native", identity) != identity:
        raise ValueError("Native ticket destination or staged pair changed")
    return identity


def _low_level(record):
    rid = record["pins"]["args"]["request_id"]
    start = _read(Path(data_dir("import-requests")) / f"{rid}.json")
    if (start.get("args") != record["pins"]["args"] or start.get("binding") != record["session"]
            or start.get("destination") != record["native"]["destination"]
            or type(start.get("after_seq")) is not int or start["after_seq"] < 0):
        raise ValueError("Native start journal differs from the candidate import binding")
    return start


def _receipt(record):
    args = record["pins"]["args"]
    rid = args["request_id"]
    path = Path(data_dir("import-pickers")) / f"{rid}.json"
    if not path.exists():
        return {}
    receipt = _read(path)
    destination = record["native"]["destination"]
    binding = dict(record["session"], request_id=rid,
                   destination={key: destination[key] for key in ("project_id", "folder_id", "name")},
                   **{f"{kind}_sha256": args[f"{kind}_sha256"] for kind in ("schematic", "board")})
    if receipt.get("binding") != binding:
        raise ValueError("Picker receipt differs from the candidate import binding")
    for kind in ("schematic", "board"):
        if kind in receipt:
            entry = receipt[kind]
            if (not isinstance(entry, dict) or entry.get("status") != "posted"
                    or entry.get("path") != record["native"]["staged"][kind]
                    or not isinstance(entry.get("identity"), list)
                    or (kind == "board" and "schematic" not in receipt)):
                raise ValueError(f"{kind.capitalize()} picker has an unverified submission attempt")
    return receipt


def _terminated(bridge, record):
    cursor = _low_level(record)["after_seq"]
    session_id = record["session"]["session_id"]
    events = bridge.call("events", dict(session_id=session_id, after_seq=cursor, limit=200), timeout=10)
    if (events.get("session_id") != session_id or events.get("reset")
            or events.get("dropped") or events.get("truncated")
            or type(events.get("latest_seq")) is not int or events["latest_seq"] < cursor
            or events.get("next_after_seq") != events["latest_seq"]
            or not {"commandStarting", "commandTerminated"} <= set(events.get("supported_events", ()))):
        raise ValueError("Cannot establish complete native paired-import command history")
    rows = [event for event in events["events"] if event.get("command_id") == _COMMAND]
    sequences = [event["seq"] for event in rows]
    if sequences != sorted(set(sequences)) or any(not cursor < seq <= events["latest_seq"] for seq in sequences):
        raise ValueError("Native paired-import event cursor does not match the command history")
    history = [event["event"] for event in rows]
    if history == ["commandStarting", "commandTerminated"]:
        return True
    if history in ([], ["commandStarting"]):
        return False
    raise ValueError("Native paired-import command history is ambiguous")


def _saved(ticket):
    documents = ticket.get("documents", ())
    return (ticket.get("status") == "saved_linked"
            and ticket.get("link_verification") == "verified_after_save"
            and isinstance(documents, list) and len(documents) == 3
            and all(isinstance(doc, dict) for doc in documents)
            and {doc.get("kind") for doc in documents} == {"design", "schematic", "board"}
            and all(doc.get("saved") is True and doc.get("modified") is False
                    and doc.get("document_id") and doc.get("data_file_id")
                    and doc.get("project_id") == ticket["destination"]["project_id"] for doc in documents)
            and all(len({doc[key] for doc in documents}) == 3
                    for key in ("document_id", "data_file_id")))


def _pending(ticket, step, *, next_action="Resume import_eagle_candidate with identical arguments.", **details):
    return dict(ticket, status="pending", step=step, native_status=ticket["status"],
                next_action=next_action, **details)


def _manual(bridge, record, ticket):
    if _terminated(bridge, record):
        return None
    staged = record["native"]["staged"]
    stage_root = (Path(tempfile.gettempdir()) / "fusion-electronics-mcp").resolve()
    for item in staged.values():
        path = Path(item)
        if (not path.is_absolute() or path.is_symlink() or path.parent.is_symlink()
                or path.parent.resolve().parent != stage_root or not path.parent.name.startswith("eagle-pair-")):
            raise ValueError("Manual selection requires the private staged EAGLE pair")
    args = dict(record["pins"]["args"], schematic_path=staged["schematic"], board_path=staged["board"])
    _, signature = eagle_import.read_pair(args)
    if eagle_import.summary(signature) != record["pins"]["expected"]:
        raise ValueError("Manual staged pair does not match the candidate")
    return _pending(ticket, "awaiting_manual_selection", manual_selection=True,
                    next_action="Select the staged schematic and then the board in Fusion. "
                                "Resume import_eagle_candidate with identical arguments.")


def _select(bridge, record, ticket):
    receipt = _receipt(record)
    if "board" in receipt:
        return None if _terminated(bridge, record) else _pending(ticket, "board_submitted")

    kind = "board" if "schematic" in receipt else "schematic"

    def awaiting_picker():
        if _terminated(bridge, record):
            raise ValueError("Native paired import terminated before both picker submissions were verified")
        return _pending(ticket, f"awaiting_{kind}_picker")

    native = picker.WindowsPicker()
    pid = record["session"]["pid"]
    if kind == "board":
        try:
            native.find(pid, ".brd")
        except ValueError as error:
            if (str(error) == "Picker button or file type does not match the requested selection"
                    and native.find(pid, ".sch") == receipt["schematic"]["identity"]):
                return awaiting_picker()
            raise
    args = record["pins"]["args"]
    destination = record["native"]["destination"]
    result = picker.select_file(bridge, args["request_id"], kind, record["session"]["session_id"],
                                args["schematic_sha256"], args["board_sha256"], destination["project_id"],
                                destination["folder_id"], destination["name"], native=native)
    if result["status"] == "submitted":
        return _pending(ticket, f"{kind}_submitted")
    return awaiting_picker()


def _unknown(request_id, reason):
    return dict(status="state_unknown", request_id=request_id, error=str(reason), next_action=_INSPECT)


def _terminal(path, record, result):
    _write(path, dict(record, result=result))
    return result


def _previous(bridge, path, rid, directory, binding):
    if not path.exists():
        return None
    previous = eagle_import.request_id(_read(path)["request_id"])
    if previous == rid:
        return None
    record = _read(directory / f"{previous}.json")
    result = record.get("result", {})
    if result.get("status") in {"failed", "saved_linked"} or record["stage"] == "prepared":
        return None
    try:
        if record["session"] != binding:
            raise ValueError("Earlier ticket belongs to a different add-in session binding")
        ticket = _ticket(bridge, previous)
        _identity(record, ticket)
        if picker._session(bridge._info()) != binding:
            raise ValueError("Add-in session changed during inspection")
        if ticket["status"] in _NO_IMPORT or _saved(ticket):
            return None
        if result.get("status") != "state_unknown" and ticket["status"] in {"queued", _PICKER_STATUS}:
            return _pending(ticket, "previous_request_pending",
                            next_action="Resume the existing candidate import request before starting another.")
    except _OBSERVATION_ERRORS as error:
        return _unknown(previous, error)
    return _unknown(previous, "An earlier candidate import is unresolved in this add-in session")


def _advance(bridge, path, record, binding):
    args = record["pins"]["args"]
    rid = args["request_id"]
    if record["session"] != binding:
        raise ValueError("Pinned add-in session changed")
    stage = record["stage"]
    if stage not in {"prepared", "start_attempted", "pending", "finish_attempted"}:
        raise ValueError("Unrecognized candidate import journal stage")
    if stage == "prepared":
        record["stage"] = "start_attempted"
        _write(path, record)
        try:
            ticket = picker.begin(bridge, args, expected_binding=binding)
        except BridgeOpError as error:
            if (error.code in {"needs_addin", "import_pending"}
                    or (error.code == "fusion_error" and error.message ==
                        "All open documents must be saved and unmodified before import")):
                return _terminal(path, record, dict(status="failed", request_id=rid, error=str(error)))
            raise
        record["native"] = _identity(record, ticket)
        if _saved(ticket):
            return _terminal(path, record, ticket)
        if ticket["status"] in _NO_IMPORT:
            return _terminal(path, record, dict(ticket, status="failed", native_status=ticket["status"]))
        if ticket["status"] not in {"queued", _PICKER_STATUS}:
            raise ValueError(ticket.get("error") or "Native start did not return a pending ticket")
        record["stage"] = "pending"
        _write(path, record)
        return _pending(ticket, "not_started" if ticket["status"] == "queued" else "awaiting_schematic_picker")

    ticket = _ticket(bridge, rid)
    identity = _identity(record, ticket)
    if picker._session(bridge._info()) != binding:
        raise ValueError("Add-in session changed while reading the ticket")
    if _saved(ticket):
        return _terminal(path, record, ticket)
    status = ticket["status"]
    if status in _NO_IMPORT:
        return _terminal(path, record, dict(ticket, status="failed", native_status=status))
    if stage == "finish_attempted" or status in {"state_unknown", "verifying", "saved_linked"}:
        raise ValueError(ticket.get("error") or "Native import or save completion remains unverified")
    if stage == "start_attempted":
        record["native"] = identity
        _low_level(record)
        record["stage"] = "pending"
        _write(path, record)
    if status == "queued":
        return _pending(ticket, "not_started")
    if status != _PICKER_STATUS or ticket.get("dispatch_acknowledged") is not True or ticket.get("save_requested"):
        raise ValueError("Native ticket is outside the verified pending picker state")
    pending = _select(bridge, record, ticket) if _IS_WINDOWS else _manual(bridge, record, ticket)
    if pending is not None:
        return pending

    if picker._session(bridge._info()) != binding:
        raise ValueError("Add-in session changed before finish")
    record["stage"] = "finish_attempted"
    _write(path, record)
    ticket = bridge.call("finish_eagle_pair_import", {"request_id": rid}, timeout=90)
    _identity(record, ticket)
    if picker._session(bridge._info()) != binding or not _saved(ticket):
        raise ValueError(ticket.get("error") or "Native finish did not verify all saved linked identities")
    return _terminal(path, record, ticket)


def import_candidate(bridge, manifest_path, expected_manifest_sha256, request_id, project_id, name, folder=""):
    """Validate immutable inputs and submit at most one native action under the shared lock."""
    try:
        rid = eagle_import.request_id(request_id)
        if not all(isinstance(value, str) and value.strip() for value in (project_id, name)):
            raise ValueError("Provide the explicit destination project ID and new product name")
        if (not isinstance(folder, str) or "\\" in folder
                or (folder and any(segment in {"", ".", ".."} for segment in folder.split("/")))
                or len(name) > 128 or any(c in "/\\" or ord(c) < 32 for c in name)):
            raise ValueError("Provide an existing project-relative folder and a valid new product name")
        pins = _validate(manifest_path, expected_manifest_sha256,
                         dict(request_id=rid, project_id=project_id, folder=folder, name=name))
    except (OSError, ValueError, TypeError, KeyError, IndexError, ParseError) as error:
        return dict(status="failed", request_id=request_id, error=str(error))

    directory = Path(data_dir("candidate-imports"))
    path = directory / f"{rid}.json"
    try:
        with bridge.lock.hold("import_eagle_candidate"):
            record = _read(path) if path.exists() else None
            if record is not None:
                if record.get("pins") != pins:
                    return dict(status="failed", request_id=rid,
                                error="Request ID is already bound to different candidate or destination pins")
                if "result" in record:
                    return record["result"]
            try:
                binding = picker._session(bridge._info())
                if record is None or record["stage"] == "prepared":
                    active = directory / (canonical_digest([binding["session_id"], binding["pid"]]) + ".active")
                    previous = _previous(bridge, active, rid, directory, binding)
                    if previous is not None:
                        return previous
                    if any((Path(data_dir(subdir)) / f"{rid}.json").exists()
                           for subdir in ("import-requests", "import-pickers")):
                        return dict(status="failed", request_id=rid,
                                    error="Request ID already belongs to a low-level paired import")
                    if record is None:
                        record = dict(pins=pins, session=binding, stage="prepared")
                        _write(path, record)
                    _write(active, {"request_id": rid})
                return _advance(bridge, path, record, binding)
            except _OBSERVATION_ERRORS as error:
                if record is None or record["stage"] == "prepared":
                    return dict(status="failed", request_id=rid, error=str(error))
                return _terminal(path, record, _unknown(rid, error))
    except FusionBusy as error:
        return dict(status="pending", request_id=rid, error=str(error))
    except _OBSERVATION_ERRORS as error:
        return _unknown(rid, error)
