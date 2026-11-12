"""Persist bounded routing outcomes and promote the exact saved board."""
from dataclasses import asdict
from hashlib import sha256
from itertools import pairwise
import json
import xml.etree.ElementTree as ET

from ..design import parse_schematic_design
from ..route_all import Conn, _commit, canonicalize_routed_signals, route_all
from ..router import Route, copper_layers, via_layers
from ..routing_budget import RoutingBudget
from . import ROUTER_CONTRACT_VERSION
from .board import route_geometry, route_owners
from .canonical import canonical_digest, canonical_json_bytes, eagle_xml_bytes
from .board_svg import render_board_svg
from .compile import _publish
from .manifest import ArtifactManifest, CandidateManifest, _MAX_MANIFEST_BYTES, _pin, load_candidate_manifest
from .model import _validate_source_xml
from .paths import _check_parents, _local_path, _read_local_file, atomic_directory, atomic_write, read_pinned_file
from .validate import _pad_centres, validate_candidate

JOB_MANIFEST_FILENAME = "job-manifest.json"
_JOB_FILES = {
    "request": "request.json", "input_board": "input-board.brd", "candidate_board": "candidate-board.brd",
    "routes": "routes.json", "diagnostics": "diagnostics.json", "preview": "board.svg",
}
_JOB_VERSION = 1


class RoutingJobManifest(ArtifactManifest):
    """Handle to the complete immutable routing job and its artifact pins."""


def _digest(raw):
    return sha256(raw).hexdigest()


def _canonical_object(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise ValueError("routing job JSON must be a canonical object")
    return value


def _identity(base_manifest_sha256, input_board_sha256, policy_sha256, connection_ids):
    return canonical_digest({
        "base_manifest_sha256": base_manifest_sha256, "input_board_sha256": input_board_sha256,
        "policy_sha256": policy_sha256,
        "connection_ids": connection_ids, "router_contract_version": ROUTER_CONTRACT_VERSION,
    })


def _verify_job(path, pin):
    raw = read_pinned_file(path, pin, _MAX_MANIFEST_BYTES)
    data = _canonical_object(raw)
    if (data.keys() != {"job_version", "router_contract_version", "job_id", "request_sha256",
                        "input_board_sha256", "candidate_board_sha256", "policy_sha256", "artifacts"}
            or type(data["job_version"]) is not int or data["job_version"] != _JOB_VERSION
            or type(data["router_contract_version"]) is not int
            or data["router_contract_version"] != ROUTER_CONTRACT_VERSION):
        raise ValueError("unsupported routing job manifest")
    entries = data["artifacts"]
    if not isinstance(entries, dict) or entries.keys() != _JOB_FILES.keys():
        raise ValueError("incomplete routing job manifest")
    for key, name in _JOB_FILES.items():
        entry = entries[key]
        if not isinstance(entry, dict) or entry.keys() != {"path", "sha256"} or entry["path"] != name:
            raise ValueError(f"invalid routing job artifact record for {key}")
        _pin(entry["sha256"])
        if key in ("request", "input_board", "candidate_board") and entry["sha256"] != data[f"{key}_sha256"]:
            raise ValueError(f"routing job {key} SHA-256 does not match its artifact pin")
    if {entry.name for entry in path.parent.iterdir()} != {*_JOB_FILES.values(), JOB_MANIFEST_FILENAME}:
        raise ValueError("incomplete routing job directory or unexpected files")
    job = RoutingJobManifest(path, pin.lower(), data)
    payloads = {key: job.read_artifact(key) for key in entries}
    request = _canonical_object(payloads["request"])
    if (request.keys() != {"base_manifest_sha256", "policy", "connections"}
            or not isinstance(request["policy"], dict) or not isinstance(request["connections"], list)):
        raise ValueError("invalid routing job request")
    _pin(request["base_manifest_sha256"])
    for row in request["connections"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("invalid routing job connection ID")
    ids = [row["id"] for row in request["connections"]]
    if ids != sorted(set(ids)) or data["policy_sha256"] != canonical_digest(request["policy"]):
        raise ValueError("routing job connections or policy SHA-256 do not match request")
    if data["job_id"] != _identity(
        request["base_manifest_sha256"], data["input_board_sha256"], data["policy_sha256"], ids,
    ):
        raise ValueError("routing job identity does not match its inputs")

    routes = _canonical_object(payloads["routes"])
    diagnostics = _canonical_object(payloads["diagnostics"])
    if (routes.keys() != {"routes", "failed", "rips", "provenance"}
            or any(not isinstance(routes[key], dict) for key in ("routes", "failed", "provenance"))
            or type(routes["rips"]) is not int or routes["rips"] < 0):
        raise ValueError("invalid routing job outcome")
    successes, failures = set(routes["routes"]), set(routes["failed"])
    if (successes & failures or successes | failures != set(ids) or diagnostics.keys() != set(ids)
            or routes["provenance"].keys() != successes):
        raise ValueError("incomplete or contradictory routing job outcome")
    for key, row in diagnostics.items():
        if (not isinstance(row, dict) or "elapsed_ms" in row or not isinstance(row.get("termination"), str)
                or (row["termination"] == "routed") != (key in successes)):
            raise ValueError("invalid routing job diagnostics")
    _verify_routes(request["connections"], routes["routes"], routes["provenance"])
    _verify_board_delta(payloads["input_board"], payloads["candidate_board"], request, routes["provenance"])
    return job


def load_routing_job(job_manifest_path, expected_sha256) -> RoutingJobManifest:
    """Read a pinned job and verify its entire artifact directory."""
    path = _local_path(job_manifest_path)
    if path.name != JOB_MANIFEST_FILENAME:
        raise ValueError("expected a routing job manifest path")
    job = _verify_job(path, expected_sha256)
    if path.parent.name != job.data["job_id"]:
        raise ValueError("routing job directory name does not match its identity")
    return job


def _request(base, board, selected_nets, prior):
    intent = json.loads(base.read_artifact("routing_intent"))
    policy = intent["routing"]
    if selected_nets is None:
        selected_nets = policy["selected_nets"]
    if (not isinstance(selected_nets, (list, tuple))
            or any(not isinstance(net, str) or not intent["nets"].get(net) for net in selected_nets)):
        raise ValueError("selected routing nets must be route-enabled nets")
    policy["selected_nets"] = sorted(set(selected_nets))
    schematic = base.read_artifact("schematic")
    report = validate_candidate(schematic, board, intent, provenance=prior)
    blocked = [row["check"] for row in report["findings"]
               if row["required"] and row["status"] != "pass" and row["check"] != "full_routing"]
    if blocked:
        raise ValueError(f"cannot plan routing with unverified base geometry: {', '.join(blocked)}")
    root = ET.fromstring(board)
    document = root.find("./drawing/board")
    stack = copper_layers(root)
    pads = _pad_centres(document, parse_schematic_design(ET.fromstring(schematic)), stack)
    widths = {node.get("number"): float(node.get("width", "0")) for node in document.findall("./classes/class")}
    classes = {node.get("name"): node.get("class", "0") for node in document.findall("./signals/signal")}
    signals = {node.attrib["name"]: node for node in document.findall("./signals/signal")}
    connections = []
    for net in policy["selected_nets"]:
        groups = report["contact_components"].get(net, [])
        allowed = policy["policies"].get(net, {}).get("allowed_layers", stack)
        escapes = {}
        if net in signals and set(allowed).intersection(stack[1:-1]):
            for via in signals[net].findall("via"):
                xy = tuple(float(via.attrib[axis]) for axis in ("x", "y"))
                layers = tuple(layer for layer in via_layers(root, via) if layer in allowed)
                if layers:
                    escapes[xy] = layers
        launches = {}
        if escapes:
            for wire in signals[net].findall("wire"):
                layer = int(wire.attrib["layer"])
                a, b = (tuple(float(wire.attrib[f"{axis}{end}"]) for axis in ("x", "y")) for end in (1, 2))
                for start, end in ((a, b), (b, a)):
                    if end in escapes:
                        launches.setdefault((*start, layer), []).append((end, escapes[end]))

        def launch(pad, toward):
            connected = [via for point in pad for via in launches.get(point, ())]
            if connected:
                return min(connected, key=lambda row: (sum((a - b) ** 2 for a, b in zip(row[0], toward)), row))
            return pad[0][:2], tuple(point[2] for point in pad)

        # One deterministic spanning path over proven copper components.
        for first, second in pairwise(groups):
            start, goal = min(
                ((tuple(a), tuple(b)) for a in first for b in second),
                key=lambda edge: (
                    sum((a - b) ** 2 for a, b in zip(pads[edge[0]][0][:2], pads[edge[1]][0][:2])), edge,
                ),
            )
            source, source_layers = launch(pads[start], pads[goal][0][:2])
            target, target_layers = launch(pads[goal], source)
            conn = Conn(f"{net}:{canonical_digest([net, start, goal])}", net, source,
                        source_layers, target, target_layers,
                        widths[classes[net]] or 0.25, f"{'.'.join(start)} -> {'.'.join(goal)}")
            connections.append(conn)
    connections.sort(key=lambda conn: conn.id)
    return {
        "base_manifest_sha256": base.sha256, "policy": policy,
        "connections": [asdict(conn) for conn in connections],
    }, root, connections


def _candidate(result, original, connections):
    provenance = {key: {"net": net, "geometry": []}
                  for key, net in connections.items() if key in result.routes}
    for signal in result.root.findall("./drawing/board/signals/signal"):
        for node in signal:
            if (owner := node.attrib.pop("mcp_conn", None)) is not None:
                provenance[owner]["geometry"].append(route_geometry(node))
    for row in provenance.values():
        row["geometry"].sort(key=canonical_json_bytes)
    return _canonical_board(original, provenance)[0], provenance


def _canonical_board(original, additions, prior=None):
    """Rebuild exact emitted copper and transfer ownership through wire subdivision."""
    prior = prior or {}
    if not additions:
        return original, prior
    root = ET.fromstring(original)
    owners = route_owners(root, prior)
    signals = {node.attrib["name"]: node for node in root.findall("./drawing/board/signals/signal")}
    for name, signal in signals.items():
        for node in signal:
            if (owner := owners.get((name, canonical_digest(route_geometry(node))))) is not None:
                node.set("mcp_conn", owner)
    selected = set()
    for owner, row in sorted(additions.items()):
        name = row["net"]
        selected.add(name)
        for geometry in row["geometry"]:
            attributes = dict(sorted(geometry["attributes"].items()))
            ET.SubElement(signals[name], geometry["tag"], {**attributes, "mcp_conn": owner})
    canonicalize_routed_signals(root, selected)
    final = {}
    for name, signal in signals.items():
        for node in signal:
            if (owner := node.attrib.pop("mcp_conn", None)) is not None:
                final.setdefault(owner, {"net": name, "geometry": []})["geometry"].append(route_geometry(node))
    for row in final.values():
        row["geometry"].sort(key=canonical_json_bytes)
    return eagle_xml_bytes(root, original), final


def _prior_provenance(base):
    if "routing_provenance" in base.data["artifacts"]:
        return _canonical_object(base.read_artifact("routing_provenance"))
    return {}


def _verify_routes(connections, routes, provenance):
    root = ET.fromstring("<eagle><drawing><board><signals/></board></drawing></eagle>")
    signals = root.find("./drawing/board/signals")
    by_id = {row["id"]: row for row in connections}
    for key, route in routes.items():
        try:
            conn = Conn(**by_id[key])
            legs = route["legs"]
            if route["net"] != conn.net:
                raise ValueError("route net differs from its connection")
            signal = ET.SubElement(signals, "signal", name=conn.net)
            _commit(root, conn, Route(
                net=conn.net, legs=[(leg["layer"], leg["points"]) for leg in legs],
                widths=[leg.get("width") for leg in legs], vias=route["vias"],
                via_drill=route["via_drill_mm"], via_diameter=route["via_diameter_mm"],
                via_extent=route["via_extent"],
            ))
            for node in signal:
                del node.attrib["mcp_conn"]
            expected = sorted(map(route_geometry, signal), key=canonical_json_bytes)
            if provenance[key]["geometry"] != expected:
                raise ValueError("route geometry differs from its provenance")
            signals.remove(signal)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid saved route geometry for {key}") from error


def _verify_board_delta(original, routed, request, provenance):
    policy = request["policy"]
    selected, protected = policy.get("selected_nets"), policy.get("protected_nets")
    if any(not isinstance(rows, list) or not all(isinstance(net, str) for net in rows)
           for rows in (selected, protected)):
        raise ValueError("invalid routing job net selection")
    changed_nets = set(selected) - set(protected)
    _validate_source_xml(original, "routing job input board")
    _validate_source_xml(routed, "routing job candidate board")
    connections = {row["id"]: row.get("net") for row in request["connections"]}
    if any(row["net"] != connections[key] or row["net"] not in changed_nets for key, row in provenance.items()):
        raise ValueError("routing provenance assigns copper outside its selected connection")
    expected, owners = _canonical_board(original, provenance)
    if expected != routed:
        raise ValueError("saved board differs from canonical routing provenance")
    route_owners(ET.fromstring(routed), owners)


def plan_routing_job(manifest_path, expected_manifest_sha256, selected_nets=None, *,
                     output_directory=None, clock=None) -> RoutingJobManifest:
    """Search once, then publish all pinned job artifacts in one rename."""
    base = load_candidate_manifest(manifest_path, expected_manifest_sha256)
    original = base.read_artifact("board")
    request, root, connections = _request(base, original, selected_nets, _prior_provenance(base))
    request_bytes = canonical_json_bytes(request)
    policy = request["policy"]
    board_pin, policy_pin = _digest(original), canonical_digest(policy)
    job_id = _identity(base.sha256, board_pin, policy_pin, [conn.id for conn in connections])
    output = _local_path(output_directory) if output_directory is not None else base.path.parent.parent
    jobs = output / "routing-jobs"
    if jobs.is_relative_to(base.path.parent):
        raise ValueError("routing jobs must be outside the input candidate directory")
    _check_parents(output)
    output.mkdir(exist_ok=True)
    _check_parents(jobs)
    jobs.mkdir(exist_ok=True)
    target = jobs / job_id
    _check_parents(target)
    path = target / JOB_MANIFEST_FILENAME

    def existing():
        try:
            raw = _read_local_file(path, _MAX_MANIFEST_BYTES)
        except FileNotFoundError as error:
            raise ValueError("incomplete routing job directory") from error
        job = load_routing_job(path, _digest(raw))
        if job.read_artifact("request") != request_bytes or job.read_artifact("input_board") != original:
            raise ValueError("existing routing job does not match the pinned request")
        return job

    if target.exists() or target.is_symlink():
        return existing()
    result = route_all(
        root, connections, budget=RoutingBudget(**policy["budget"]), policies=policy["policies"],
        protected_nets=policy["protected_nets"], clock=clock, canonicalize=False,
    )
    board, provenance = _candidate(result, original, {conn.id: conn.net for conn in connections})
    routes = {key: route.as_dict() for key, route in result.routes.items()}
    for row in routes.values():
        row.pop("diagnostics", None)
    diagnostics = {key: diagnostic.as_dict() for key, diagnostic in result.diagnostics.items()}
    for row in diagnostics.values():
        del row["elapsed_ms"]
    artifacts = {
        "input_board": original, "request": request_bytes, "candidate_board": board,
        "preview": render_board_svg(board, policy, request, diagnostics),
        "routes": canonical_json_bytes({"routes": routes, "failed": result.failed, "rips": result.rips,
                                       "provenance": provenance}),
        "diagnostics": canonical_json_bytes(diagnostics),
    }
    data = {
        "job_version": _JOB_VERSION, "router_contract_version": ROUTER_CONTRACT_VERSION, "job_id": job_id,
        "input_board_sha256": board_pin, "candidate_board_sha256": _digest(board),
        "request_sha256": _digest(request_bytes), "policy_sha256": policy_pin,
        "artifacts": {key: {"path": _JOB_FILES[key], "sha256": _digest(raw)} for key, raw in artifacts.items()},
    }
    manifest = canonical_json_bytes(data)
    manifest_pin = _digest(manifest)
    try:
        with atomic_directory(target) as staging:
            for key, raw in artifacts.items():
                atomic_write(staging / _JOB_FILES[key], raw)
            atomic_write(staging / JOB_MANIFEST_FILENAME, manifest)
            _verify_job(staging / JOB_MANIFEST_FILENAME, manifest_pin)
    except (OSError, ValueError):
        if target.is_dir():
            return existing()
        raise
    return RoutingJobManifest(path, manifest_pin, data)


def preview_routing_job(job_manifest_path, expected_sha256) -> bytes:
    """Return the saved preview after verifying the pinned job."""
    return load_routing_job(job_manifest_path, expected_sha256).read_artifact("preview")


def promote_routing_job(candidate_manifest_path, expected_candidate_sha256, job_manifest_path,
                        expected_job_sha256, output_directory) -> CandidateManifest:
    """Promote the exact persisted routing result from the verified pinned base."""
    base = load_candidate_manifest(candidate_manifest_path, expected_candidate_sha256)
    job = load_routing_job(job_manifest_path, expected_job_sha256)
    output = _local_path(output_directory)
    if any(output.is_relative_to(directory) for directory in (base.path.parent, job.path.parent)):
        raise ValueError("promoted candidate must be outside each input directory")
    request = _canonical_object(job.read_artifact("request"))
    original = job.read_artifact("input_board")
    if request["base_manifest_sha256"] != base.sha256 or original != base.read_artifact("board"):
        raise ValueError("routing job does not match the pinned base candidate")
    prior = _prior_provenance(base)
    expected, _, _ = _request(base, original, request["policy"]["selected_nets"], prior)
    if canonical_json_bytes(expected) != job.read_artifact("request"):
        raise ValueError("routing job policy or connections do not match the pinned base candidate")
    outcome = _canonical_object(job.read_artifact("routes"))
    if prior.keys() & outcome["provenance"].keys():
        raise ValueError("routing job overlaps pre-existing connection provenance")
    board, provenance = _canonical_board(original, outcome["provenance"], prior)
    if board != job.read_artifact("candidate_board"):
        raise ValueError("canonical routing changed the pinned board during promotion")
    record = {
        "job_id": job.data["job_id"], "candidate_board_sha256": job.data["candidate_board_sha256"],
        "manifest_sha256": job.sha256,
    }
    return _publish(output, base.data["inputs"], base.read_artifact("schematic"),
                    board, base.read_artifact("routing_intent"), record,
                    provenance=provenance, board_svg=job.read_artifact("preview"),
                    routing_artifacts={f"routing_{name}": job.read_artifact(name)
                                       for name in ("request", "routes", "diagnostics")})
