"""Opt-in Windows/Fusion acceptance of the compiler's disposable fixture.

    python tests/live/e2e_compiled_import.py --disposable-project-id ID --name NEW_PRODUCT

Make the disposable project active in Fusion and leave all open documents saved
and unmodified. The imported family remains saved and open. The report and
exports are retained under --artifacts (a new directory), or in a new temp folder.
"""
import argparse
from contextlib import contextmanager
import json
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


def native_check(server, kind, command, saved):
    doc = active_member(server, kind, saved)
    result = server.session.bridge.call("run", {
        "commands": command, "editor": kind, "expected_document_id": doc["document_id"],
    })
    if kind == "schematic":
        unexpected = [row for row in result.get("dialogs", ()) if not row.get("expected")]
        require(not unexpected, f"Unexpected native ERC dialog: {unexpected}")

    def verify_active():
        context = server.get_context()
        require(is_active_member(context, kind, doc)
                and context["active_document"]["document_id"] == doc["document_id"],
                f"Active document changed during native {command.rstrip(';')}")

    verify_active()
    findings = server.session.errors(kind)
    verify_active()
    return {"command": result, "findings": findings}


def run_acceptance(server, report, build, materialize):
    from fusion_offline.drc import summarize

    project_id, name = report.data["project_id"], report.data["name"]
    bridge = server.session.bridge
    with report.phase("compile") as phase:
        _, job, candidate = build(report.root, materialize(report.root))
        validation = json.loads(candidate.read_artifact("report"))
        phase.update(candidate_id=candidate.data["candidate_id"], manifest=str(candidate.path),
                     manifest_sha256=candidate.sha256, routing_job_id=job.data["job_id"],
                     validation=validation)
        require(validation["status"] == "pass" and validation["airwires"] == 0,
                "The offline candidate did not pass with zero proven airwires")

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
            comparison["expected"] = pair_signatures(candidate.read_artifact("schematic"),
                                                     candidate.read_artifact("board"))
            comparison["exported"] = pair_signatures(actual["schematic"], actual["board"])
            comparison["matches"] = comparison["expected"] == comparison["exported"]
            comparison["exported_airwires"] = len(ET.fromstring(actual["board"]).findall(".//signal/wire[@layer='19']"))
        with report.phase("native_erc") as erc:
            erc.update(native_check(server, "schematic", "ERC;", saved))
        with report.phase("native_drc") as drc:
            drc.update(native_check(server, "board", "DRC;", saved))
            drc["summary"] = summarize(drc["findings"]["errors"])
        with report.phase("acceptance"):
            unchanged(original, server.get_context())
            require(comparison["matches"], "Reopened electrical or physical contact signature differs")
            require(comparison["exported_airwires"] == drc["summary"]["airwires"] == 0,
                    "The reopened board has native airwires")
            for result in (erc, drc):
                require(result["findings"]["count"] == 0 and result["findings"]["errors"] == [],
                        f"Unexpected native {result['findings']['kind']} findings")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-project-id", required=True, help="Explicit ID of a disposable Fusion project")
    parser.add_argument("--name", required=True, help="New Fusion product name in the disposable project root")
    parser.add_argument("--artifacts", type=Path, help="New directory for the acceptance report and exports")
    args = parser.parse_args(argv)
    if not args.disposable_project_id.strip() or not args.name.strip():
        parser.error("The disposable project ID and destination name must be nonempty")
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

        run_acceptance(server, report, build_routed_fixture, materialize_spec)
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
