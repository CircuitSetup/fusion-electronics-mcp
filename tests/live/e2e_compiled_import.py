"""Opt-in Windows/Fusion acceptance of the compiler's disposable fixture.

    python tests/live/e2e_compiled_import.py --disposable-project-id ID --name NEW_PRODUCT

Make the disposable project active in Fusion and leave all open documents saved
and unmodified. Enable Polygon calculation for RATSNEST in Fusion. The imported
family remains saved and open. The report and exports are retained under
--artifacts (a new directory), or in a new temp folder.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from functools import partial
from hashlib import sha256
import json
from math import isclose, isfinite
import os
from pathlib import Path
import sys
from tempfile import mkdtemp
from time import monotonic, sleep
from uuid import uuid4
import xml.etree.ElementTree as ET

KINDS = {"design", "schematic", "board"}
MAX_IMPORT_CALLS = 24
IMPORT_WAIT_SECONDS = 180


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Acceptance:
    def __init__(self, root, project_id, name):
        self.root = root
        self.data = {
            "status": "running", "project_id": project_id, "name": name,
            "request_id": uuid4().hex, "phases": {},
        }

    def write(self):
        temporary = self.root / "report.tmp"
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.root / "report.json")

    @contextmanager
    def phase(self, name):
        started = monotonic()
        row = self.data["phases"][name] = {}
        try:
            yield row
        except BaseException as error:
            row["error"] = str(error)
            raise
        finally:
            row["elapsed_seconds"] = round(monotonic() - started, 3)
            self.write()


def unchanged(original, context):
    def document_state(row):
        return {key: value for key, value in row.items() if key != "active"}

    current = {row["document_id"]: document_state(row) for row in context["documents"]}
    require(all(current.get(row["document_id"]) == document_state(row) for row in original),
            "An unrelated open Electronics document changed or closed")


def clean(row):
    return row["saved"] is True and row["modified"] is False


def fresh_saved_family(ticket, project_id, original):
    require(ticket["link_verification"] == "verified_after_save", "Saved family links are unverified")
    rows = ticket["documents"]
    saved = {row["kind"]: row for row in rows}
    require(len(rows) == 3 and saved.keys() == KINDS, "Expected three saved family members")
    for key in ("document_id", "data_file_id"):
        ids = {row[key] for row in rows}
        require(len(ids) == 3 and all(ids), f"Saved family has missing or duplicate {key}")
    require(all(clean(row) and row["project_id"] == project_id for row in rows),
            "Saved family is outside the disposable project or is modified")
    require(not {row["document_id"] for row in rows} & {row["document_id"] for row in original},
            "Saved family overlaps an unrelated open document")
    return saved


def advance_import(server, candidate, report):
    rid = report.data["request_id"]
    args = (str(candidate.path), candidate.sha256, rid, report.data["project_id"], report.data["name"])
    deadline = monotonic() + IMPORT_WAIT_SECONDS
    for attempt in range(1, MAX_IMPORT_CALLS + 1):
        with report.phase(f"import_{attempt:02}") as phase:
            try:
                ticket = server.import_eagle_candidate(*args)
            except BaseException:
                report.data["status"] = "state_unknown"
                raise
            phase["ticket"] = ticket
            if ticket["status"] == "state_unknown":
                report.data["status"] = "state_unknown"
                raise RuntimeError(f"Native import state is unknown for {rid}. Inspect Fusion and the saved ticket.")
            require(ticket["request_id"] == rid, "Fusion reported a different import request")
            if ticket["status"] == "saved_linked":
                return ticket
            require(ticket["status"] == "pending" and not ticket.get("manual_selection"),
                    f"Native import stopped with status {ticket['status']}")
        if monotonic() >= deadline or attempt == MAX_IMPORT_CALLS:
            break
        sleep(0.75)
    report.data["status"] = "pending"
    raise RuntimeError(f"Import {rid} reached the acceptance harness's resumption limit")


def reopen_family(server, project_id, name, saved, original):
    context = server.get_context()
    unchanged(original, context)
    before = {row["document_id"]: row for row in context["documents"]}
    ids = {row["document_id"] for row in saved.values()}
    require(all(row["document_id"] in before and clean(before[row["document_id"]])
                and before[row["document_id"]]["data_file_id"] == row["data_file_id"]
                and before[row["document_id"]]["project_id"] == project_id for row in saved.values()),
            "The exact saved family is not open and clean")
    names = {before[identity]["name"].casefold() for identity in ids} | {name.casefold()}
    require(all(row["document_id"] in ids for row in before.values() if row["name"].casefold() in names),
            "An unrelated open document shares the imported family's name")
    # Native close selects by displayed name and refuses ambiguous families.
    displayed_name = before[saved["design"]["document_id"]]["name"]
    closed = server.close_design(displayed_name, discard_changes=False)
    unchanged(original, closed)
    files = {row["data_file_id"] for row in saved.values()}
    require(not any(row["document_id"] in ids or row["data_file_id"] in files for row in closed["documents"]),
            "The imported family did not fully close")

    reopened = server.open_design(name, folder="", data_file_id=saved["design"]["data_file_id"])
    unchanged(original, reopened)
    active = reopened["active_document"] or {}
    require(not reopened.get("already_open") and active.get("data_file_id") == saved["design"]["data_file_id"]
            and active.get("document_id") not in ids and active.get("project_id") == project_id and clean(active),
            "Fusion did not reopen the exact saved design")
    return reopened


def is_active_member(context, kind, target):
    doc = context["active_document"] or {}
    return (context["active_editor"] == kind and doc.get("data_file_id") == target["data_file_id"]
            and doc.get("project_id") == target["project_id"] and clean(doc))


def active_member(server, kind, saved):
    context = server.session.activate(kind)
    require(is_active_member(context, kind, saved[kind]), f"Fusion did not activate the exact reopened {kind}")
    return context["active_document"]


def pair_signatures(schematic, board):
    from fusion_mcp.addin.FusionElectronicsMCP import eagle_import

    sch, brd = ET.fromstring(schematic), ET.fromstring(board)
    sheets = sch.findall("./drawing/schematic/sheets/sheet")
    electrical = sorted(
        (number, net.attrib["name"], ref.attrib["part"], ref.attrib["gate"], ref.attrib["pin"])
        for number, sheet in enumerate(sheets, start=1)
        for net in sheet.findall("./nets/net")
        for ref in net.findall("./segment/pinref")
    )
    placed_gates = sorted(
        (number, instance.attrib["part"], instance.attrib["gate"])
        for number, sheet in enumerate(sheets, start=1)
        for instance in sheet.findall("./instances/instance")
    )
    return {"electrical_pins": electrical, "placed_gates": placed_gates,
            "physical_pair": eagle_import.signature(sch, brd)}


def copper_signature(board):
    """Compare saved route geometry at EAGLE export precision, excluding airwires."""
    root = ET.fromstring(board)
    copper = []
    for signal in root.findall("./drawing/board/signals/signal"):
        name = signal.attrib["name"]
        for wire in signal.findall("wire"):
            layer = int(wire.attrib["layer"])
            if layer == 19:
                continue
            a, b = (tuple(round(float(wire.attrib[f"{axis}{end}"]), 5) for axis in "xy") for end in (1, 2))
            curve = round(float(wire.get("curve", "0")), 5)
            if a > b:
                a, b, curve = b, a, -curve
            copper.append((name, "wire", layer, a, b, round(float(wire.attrib["width"]), 5), curve))
        for via in signal.findall("via"):
            sizes = tuple(round(float(via.get(key, "0")), 5) for key in ("x", "y", "drill", "diameter"))
            copper.append((name, "via", via.attrib["extent"], sizes, via.get("shape", "round")))
    return Counter(copper)


def verify_active(server, kind, doc, operation):
    context = server.get_context()
    require(is_active_member(context, kind, doc)
            and context["active_document"]["document_id"] == doc["document_id"],
            f"Active document changed during native {operation}")


def native_repour(server, saved, request_id, phase):
    doc = active_member(server, "board", saved)
    source = Path(__file__).with_name("measure-repoured-copper.ulp")
    digest = sha256(source.read_bytes()).hexdigest()
    name = f"compiler-repour-{request_id}"
    server.register_ulp(name, str(source), "read", ["board"], 0, digest)
    try:
        phase["command"] = server.session.bridge.call("run", {
            "commands": "RATSNEST;", "editor": "board", "expected_document_id": doc["document_id"],
        })
        unexpected = [row for row in phase["command"].get("dialogs", ()) if not row.get("expected")]
        require(not unexpected, f"Unexpected native RATSNEST dialog: {unexpected}")
        verify_active(server, "board", doc, "RATSNEST")
        observed = server.run_registered_ulp(name, [], doc["document_id"])
        verify_active(server, "board", doc, "repoured copper measurement")
    finally:
        server.unregister_ulp(name)
    require(observed["document"]["document_id"] == doc["document_id"],
            "Repoured copper measurement was returned for a different document")
    require(observed["sha256"] == digest, "Native fill measurement used an unexpected ULP source")
    phase["measurement_document_id"] = doc["document_id"]
    phase["measurement_data_file_id"] = doc["data_file_id"]
    phase["measurement_ulp_sha256"] = digest
    phase["measurement"] = observed["data"]


def compare_airwires(exported, measurement):
    """Compare undirected spans while keeping the exact live coordinates in the report."""
    def point(value):
        require(isinstance(value, (list, tuple)) and len(value) == 2
                and all(type(v) in (int, float) and isfinite(v) for v in value),
                "Invalid native airwire endpoint")
        return tuple(value)

    def key(net, start, end):
        require(isinstance(net, str) and net, "Invalid native airwire net")
        return net, *sorted((point(start), point(end)))

    def evidence(counts):
        return [{"net": net, "start_mm": list(start), "end_mm": list(end)}
                for net, start, end in sorted(counts.elements())]

    native = measurement.get("airwire_spans")
    count = measurement.get("airwires")
    require(isinstance(native, list) and type(count) is int and count == len(native),
            "Native airwire count does not match endpoint evidence")
    live_counts = Counter(key(row["net"], row["start_mm"], row["end_mm"]) for row in native)
    xml_counts = Counter(
        key(signal.attrib["name"], [float(wire.attrib[axis]) for axis in ("x1", "y1")],
            [float(wire.attrib[axis]) for axis in ("x2", "y2")])
        for signal in ET.fromstring(exported).findall("./drawing/board/signals/signal")
        for wire in signal.findall("wire[@layer='19']")
    )
    return {"native_by_net": dict(Counter(net for net, _, _ in live_counts.elements())),
            "export_by_net": dict(Counter(net for net, _, _ in xml_counts.elements())),
            "native_only": evidence(live_counts - xml_counts),
            "export_only": evidence(xml_counts - live_counts),
            "matching": evidence(live_counts & xml_counts)}


def measured_fill_rows(measurement):
    """Convert live directed contour edges into filled islands with their own void rings."""
    from fusion_offline.pour_clearance import _inside

    rows = []
    for fill in measurement["fills"]:
        edges = fill.get("edges_mm")
        require(type(fill.get("hatched")) is int and fill["hatched"] == 0
                and isinstance(edges, list) and len(edges) == fill["contour_wires"] and edges,
                "Incomplete or unsupported native fill contours")
        outer, holes, points = [], [], []
        twice_area = 0
        last = None
        for edge in edges:
            require(isinstance(edge, list) and len(edge) == 5
                    and all(type(v) in (int, float) and isfinite(v) for v in edge) and edge[4] == 0,
                    "Unsupported curved or invalid native fill contour")
            start, end = tuple(edge[:2]), tuple(edge[2:4])
            require(start != end and (not points or start == last), "Disconnected native fill contour")
            points.append(start)
            last = end
            if end != points[0]:
                continue
            require(len(points) >= 3, "Degenerate native fill contour")
            area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))
            require(area != 0, "Degenerate native fill contour")
            twice_area += area
            if area < 0:
                outer.append((-area, [points]))
            else:
                holes.append(points)
            points = []
        require(not points and outer, "Incomplete native fill contour")
        require(isclose(-twice_area / 2, fill["area_mm2"], rel_tol=1e-8, abs_tol=1e-8),
                "Native fill contour area differs from the measurement")
        for hole in holes:
            containers = [(area, rings) for area, rings in outer if _inside(rings[0], *hole[0])]
            require(containers, "Native fill contour hole has no enclosing island")
            min(containers, key=lambda row: row[0])[1].append(hole)
        rows.extend({"net": fill["net"], "layer": fill["layer"], "rings": rings} for _, rings in outer)
    return rows


def require_measured_fills(board, measurement):
    expected = Counter((signal.attrib["name"], int(polygon.attrib["layer"]))
                       for signal in ET.fromstring(board).findall(".//signal")
                       for polygon in signal.findall("polygon") if polygon.get("pour") != "cutout")
    fills = measurement["fills"]
    require(Counter((row["net"], row["layer"]) for row in fills) == expected,
            "Native fill measurements do not match the candidate's declared pours")
    require(all(type(row["contour_wires"]) is int and row["contour_wires"] > 0
                and type(row["area_mm2"]) in (int, float)
                and isfinite(row["area_mm2"]) and row["area_mm2"] > 0 for row in fills),
            "The repoured board has a polygon without positive measured filled area")


def native_check(server, kind, command, saved):
    doc = active_member(server, kind, saved)
    result = server.session.bridge.call("run", {
        "commands": command, "editor": kind, "expected_document_id": doc["document_id"],
    })
    if kind == "schematic":
        unexpected = [row for row in result.get("dialogs", ()) if not row.get("expected")]
        require(not unexpected, f"Unexpected native ERC dialog: {unexpected}")

    operation = command.rstrip(";")
    verify_active(server, kind, doc, operation)
    findings = server.session.errors(kind)
    verify_active(server, kind, doc, operation)
    return {"command": result, "findings": findings}


def require_inner_restrict_round_trip(candidate, exported):
    from fusion_offline.design import parse_board_design
    from fusion_offline.stitch import board_obstacles

    def restrictions(raw):
        root = ET.fromstring(raw)
        stack = parse_board_design(root).copper_layers
        inner = set(stack[1:-1])
        shapes = []
        for obs in board_obstacles(root)[0]:
            if not (obs.restrict_layer and obs.layers and inner.intersection(obs.layers)):
                continue
            require(obs.kind == "polygon", "Unexpected inner copper restriction shape")
            vertices, half = obs.data[2:]
            vertices = tuple((round(x, 4), round(y, 4)) for x, y in vertices)
            start = vertices.index(min(vertices))
            vertices = vertices[start:] + vertices[:start]
            vertices = min(vertices, vertices[:1] + vertices[:0:-1])
            shapes.append((tuple(round(x, 4) for x in obs.bounds), tuple(sorted(obs.layers)),
                           vertices, round(half, 4)))
        return stack, sorted(shapes)

    expected_stack, expected = restrictions(candidate)
    actual_stack, actual = restrictions(exported)
    require(len(expected_stack) == 4 and actual_stack == expected_stack and len(expected) >= 2 and actual == expected,
            f"Reopened inner copper restrictions differ: expected {expected}, got {actual}")
    return [{"bounds": list(bounds), "layers": list(layers)} for bounds, layers, _, _ in actual]


def native_board_semantics(server, saved, candidate_board, exported_board):
    from fusion_mcp.bridge import BridgeOpError
    from fusion_offline.compiler.roundtrip import compare_native_board

    source = {}
    doc = active_member(server, "board", saved)
    rules_xml = None
    try:
        working = server.session.bridge.call("design_rules", timeout=60)
    except BridgeOpError as error:
        if error.code != "not_found":
            raise
        source["unavailable"] = str(error)
    else:
        require(working["board"] == doc["name"], "Working copy belongs to a different board")
        rules_xml = working["xml"]
        source.update(board=working["board"], modified=working["modified"],
                      sha256=sha256(rules_xml.encode()).hexdigest())
    verify_active(server, "board", doc, "native rule readback")
    result = compare_native_board(candidate_board, exported_board, native_rules_xml=rules_xml)
    return {**result, "working_copy_rules": source, "data_file_id": doc["data_file_id"]}


def require_native_board_semantics(semantics):
    for group in ("rules", "restrictions"):
        require(semantics[group]["status"] == "pass", f"Native board {group} are not verified")


def measured_fill_validation(candidate, schematic, board, measurement):
    from fusion_offline.compiler.validate import validate_candidate

    try:
        require_measured_fills(board, measurement)
        rows = measured_fill_rows(measurement)
        intent = json.loads(candidate.read_artifact("routing_intent"))
        candidate_board = candidate.read_artifact("board")
        provenance = None
        # Native export can split or normalize copper, invalidating original geometry ownership.
        if board == candidate_board and candidate.data.get("routing_job"):
            provenance = json.loads(candidate.read_artifact("routing_provenance"))
        report = validate_candidate(schematic, board, intent, provenance=provenance, measured_fills=rows,
                                    measured_fills_complete=True)
    except (ValueError, RuntimeError) as error:
        return {"status": "unverified", "detail": str(error)}
    findings = [row for row in report["findings"] if row["required"] or row["check"] == "full_routing"]
    statuses = {row["status"] for row in findings}
    status = "fail" if "fail" in statuses else "pass" if statuses == {"pass"} else "unverified"
    return {"status": status, "source": "Live Fusion post-RATSNEST polygon contours",
            "findings": findings, "airwires": report["airwires"], "planes": report["planes"]}


def require_offline_acceptance(validation):
    counts = validation.get("airwires_by_net", {})
    planes = validation.get("planes", {})
    require(validation["status"] == "pass"
            and validation["airwires"] == sum(counts.values())
            and all(count == 0 or planes.get(net, {}).get("status") == "unverified"
                    for net, count in counts.items()),
            "The offline candidate must pass with any remaining opens awaiting native plane fill")


def run_acceptance(server, report, build, materialize, *, inner_restrict=False):
    from fusion_offline.drc import summarize

    project_id, name = report.data["project_id"], report.data["name"]
    bridge = server.session.bridge
    with report.phase("offline_compile_validate") as phase:
        _, job, candidate = build(report.root, materialize(report.root))
        validation = json.loads(candidate.read_artifact("report"))
        routing_job = candidate.data.get("routing_job") or {}
        phase.update(candidate_id=candidate.data["candidate_id"], manifest=str(candidate.path),
                     manifest_sha256=candidate.sha256,
                     routing_job_id=job.data["job_id"] if job else routing_job.get("job_id"),
                     validation=validation)
        require_offline_acceptance(validation)

    with bridge.lock.hold("compiled candidate live acceptance"):
        with report.phase("preflight") as phase:
            context = server.get_context()
            original = context["documents"]
            active = context["active_document"]
            phase["versions"] = {
                "server": context["server"]["version"],
                **{key: value for key, value in bridge.call("ping", timeout=15).items()
                   if key in {"fusion_version", "addin_version"}},
            }
            require(phase["versions"].keys() == {"server", "fusion_version", "addin_version"},
                    "Could not verify all three running versions")
            project = server.list_designs(folder="", limit=1)
            phase["active_project"] = {"id": project["project_id"], "name": project["project"]}
            require(project["project_id"] == project_id,
                    "Fusion's active project does not match the explicit disposable project ID")
            require(all(clean(row) for row in original) and (active is None or clean(active)),
                    "Save open documents and clear modifications before running acceptance")
            require(not any(row["name"].casefold() == name.casefold() for row in original),
                    "An unrelated open document already uses the destination name")

        ticket = advance_import(server, candidate, report)
        with report.phase("saved_linked") as phase:
            saved = fresh_saved_family(ticket, project_id, original)
            phase["saved_documents"] = saved
        with report.phase("reopen") as phase:
            phase["context"] = reopen_family(server, project_id, name, saved, original)
        with report.phase("export_signatures") as comparison:
            actual = {}
            for kind, suffix in (("schematic", "sch"), ("board", "brd")):
                active_member(server, kind, saved)
                actual[kind] = server.session.export(kind)
                (report.root / f"reopened.{suffix}").write_bytes(actual[kind])
            candidate_board = candidate.read_artifact("board")
            comparison["expected"] = pair_signatures(candidate.read_artifact("schematic"), candidate_board)
            comparison["exported"] = pair_signatures(actual["schematic"], actual["board"])
            comparison["export_sha256"] = {kind: sha256(raw).hexdigest() for kind, raw in actual.items()}
            comparison["matches"] = comparison["expected"] == comparison["exported"]
            if inner_restrict:
                comparison["inner_restrict"] = require_inner_restrict_round_trip(candidate_board, actual["board"])
            comparison["raw_export_layer_19_spans"] = len(
                ET.fromstring(actual["board"]).findall(".//signal/wire[@layer='19']")
            )
        with report.phase("native_erc") as erc:
            erc.update(native_check(server, "schematic", "ERC;", saved))
        with report.phase("native_repour_airwires") as repour:
            native_repour(server, saved, report.data["request_id"], repour)
            repour["airwire_comparison"] = compare_airwires(actual["board"], repour["measurement"])
            board_doc = active_member(server, "board", saved)
            repoured_board = server.session.export("board")
            verify_active(server, "board", board_doc, "post-repour board export")
            (report.root / "repoured.brd").write_bytes(repoured_board)
            repour["board_sha256"] = sha256(repoured_board).hexdigest()
            repour["post_repour_airwire_comparison"] = compare_airwires(repoured_board, repour["measurement"])
            repour["post_repour_signature_matches"] = (
                pair_signatures(actual["schematic"], repoured_board) == comparison["exported"]
            )
            expected_copper, observed_copper = map(copper_signature, (candidate_board, repoured_board))
            repour["route_geometry_changes"] = {
                "missing": sorted((expected_copper - observed_copper).elements()),
                "added": sorted((observed_copper - expected_copper).elements()),
            }
        with report.phase("native_board_semantics") as semantics:
            semantics.update(native_board_semantics(server, saved, candidate_board, repoured_board))
        with report.phase("measured_fill_connectivity") as fills:
            fills.update(measured_fill_validation(candidate, actual["schematic"], repoured_board,
                                                  repour["measurement"]))
        with report.phase("native_drc") as drc:
            drc.update(native_check(server, "board", "DRC;", saved))
            drc["summary"] = summarize(drc["findings"]["errors"])
        with report.phase("acceptance"):
            unchanged(original, server.get_context())
            require(comparison["matches"], "Reopened electrical or physical contact signature differs")
            require(repour["post_repour_signature_matches"], "Post-repour electrical or physical contact signature differs")
            require(not any(repour["route_geometry_changes"].values()), "Post-repour trace or via geometry differs")
            measurement = repour["measurement"]
            require(type(measurement["airwires"]) is int
                    and measurement["airwires"] == drc["summary"]["airwires"] == 0,
                    "The repoured board has native airwires")
            require_measured_fills(candidate_board, measurement)
            require_native_board_semantics(semantics)
            require(fills["status"] == "pass", "Post-repour routing, fill connectivity, or keepouts failed validation")
            for result in (erc, drc):
                require(result["findings"]["count"] == 0 and result["findings"]["errors"] == [],
                        f"Unexpected native {result['findings']['kind']} findings")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-project-id", required=True, help="Explicit ID of a disposable Fusion project")
    parser.add_argument("--name", required=True, help="New Fusion product name in the disposable project root")
    parser.add_argument("--artifacts", type=Path, help="New directory for the acceptance report and exports")
    parser.add_argument("--inner-restrict", action="store_true", help="Use the four-layer restriction fixture")
    parser.add_argument("--manifest", type=Path, help="Existing compiler candidate manifest")
    parser.add_argument("--manifest-sha256", help="Exact SHA-256 of the existing compiler candidate manifest")
    args = parser.parse_args(argv)
    if not args.disposable_project_id.strip() or not args.name.strip():
        parser.error("The disposable project ID and destination name must be nonempty")
    if bool(args.manifest) != bool(args.manifest_sha256) or (args.manifest and args.inner_restrict):
        parser.error("Provide both manifest flags and omit --inner-restrict for an existing candidate")
    if sys.platform != "win32":
        parser.error("This harness requires Windows and the authenticated Fusion add-in picker")
    if args.artifacts:
        root = args.artifacts.resolve()
        root.mkdir(parents=True)
    else:
        root = Path(mkdtemp(prefix="fusion-compiled-acceptance-")).resolve()
    report = Acceptance(root, args.disposable_project_id, args.name)
    print(f"Acceptance report: {root / 'report.json'}", flush=True)
    try:
        os.environ["FUSION_MCP_TRANSPORT"] = "addin"
        from fusion_mcp import server

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from test_compiler_e2e import build_routed_fixture, materialize_spec

        if args.manifest:
            from fusion_offline.compiler.manifest import load_candidate_manifest

            def build_pinned(_root, pins):
                return None, None, load_candidate_manifest(*pins)

            run_acceptance(server, report, build_pinned, lambda _: (args.manifest, args.manifest_sha256))
        else:
            run_acceptance(server, report, build_routed_fixture,
                           partial(materialize_spec, inner_restrict=args.inner_restrict), inner_restrict=args.inner_restrict)
        report.data["status"] = "pass"
    except Exception as error:
        if report.data["status"] == "running":
            report.data["status"] = "fail"
        report.data["error"] = str(error)
        print(str(error), file=sys.stderr)
    finally:
        report.write()
    return 0 if report.data["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
