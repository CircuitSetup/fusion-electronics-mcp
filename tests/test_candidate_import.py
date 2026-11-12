"""Candidate import exercises real pins, journals, picker guards and bridge locking."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fusion_mcp import eagle_pair_picker as picker, server
from fusion_mcp.addin.FusionElectronicsMCP import eagle_import
from fusion_mcp.addin.FusionElectronicsMCP.management import EventLog
from fusion_mcp.fusion_lock import FusionLock
from fusion_offline.compiler.canonical import canonical_json_bytes
from fusion_offline.compiler.compile import compile_candidate
from test_compiler_schema import minimal_design
from test_eagle_pair_picker import Native


class CandidateImportTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        stage_root = Path(tempfile.gettempdir()).resolve() / "fusion-electronics-mcp"
        stage_root.mkdir(exist_ok=True)
        self.stage = Path(self.enterContext(tempfile.TemporaryDirectory(
            prefix="eagle-pair-", dir=stage_root)))
        self.candidate = self.compile("candidate")
        self.info = dict(pid=123, token="token", event_session_id="session", addin_version="0.14.23")
        self.events = EventLog()
        self.events.session_id = "session"
        self.events.supported = ["commandStarting", "commandTerminated"]
        self.destination = dict(project_id="project", folder_id="folder-id", folder="existing",
                                name="Product")
        self.docs = [dict(kind=kind, document_id=f"doc-{kind}", data_file_id=f"file-{kind}",
                          project_id="project", saved=True, modified=False)
                     for kind in ("design", "schematic", "board")]
        self.calls = []
        self.ticket = None
        self.start_error = None
        self.finish_error = None
        self.finish_status = "saved_linked"
        self.native = Native()
        self.bridge = SimpleNamespace(lock=FusionLock(str(self.root / "fusion.lock")),
                                      _info=lambda: dict(self.info), call=self.call)
        self.enterContext(patch.object(server.session, "bridge", self.bridge))
        self.enterContext(patch.object(picker, "data_dir", self.data))
        self.enterContext(patch.object(picker, "WindowsPicker", return_value=self.native))

    def data(self, *parts):
        return str(self.root.joinpath(*parts))

    def compile(self, name, *, full=False):
        spec = self.root / f"{name}.json"
        design = minimal_design()
        design["routing"]["require_fully_routed"] = full
        raw = json.dumps(design).encode()
        spec.write_bytes(raw)
        return compile_candidate(spec, sha256(raw).hexdigest(), self.root / name)

    def import_(self, **changes):
        args = dict(manifest_path=str(self.candidate.path),
                    expected_manifest_sha256=self.candidate.sha256, request_id="a" * 32,
                    project_id="project", folder="existing", name="Product")
        from fusion_mcp import candidate_import
        with patch.object(candidate_import, "data_dir", self.data), patch.object(
                candidate_import, "_IS_WINDOWS", changes.pop("windows", True)):
            return server.import_eagle_candidate(**(args | changes))

    def call(self, op, args, **kwargs):
        self.calls.append(op)
        if op == "events":
            return self.events.read(**args)
        if op == "start_eagle_pair_import":
            if self.start_error:
                raise self.start_error
            raw, signature = eagle_import.read_pair(args)
            staged = {}
            for kind, content in zip(("schematic", "board"), raw):
                path = self.stage / Path(args[f"{kind}_path"]).name
                path.write_bytes(content)
                staged[kind] = str(path)
            self.ticket = dict(request_id=args["request_id"], session_id=self.info["event_session_id"],
                               status="queued", staged=staged, expected=eagle_import.summary(signature),
                               destination=dict(self.destination),
                               next_action="Select the staged files and call the low-level finish tool.")
        if op == "finish_eagle_pair_import":
            if self.finish_error:
                raise self.finish_error
            self.ticket.update(status=self.finish_status)
            if self.finish_status == "saved_linked":
                self.ticket.update(documents=self.docs, link_verification="verified_after_save",
                                   save_requested=True)
        if self.ticket is None:
            raise ValueError("Unknown import request")
        return deepcopy(self.ticket)

    def open_picker(self):
        self.events.append("commandStarting", command_id="ImportSCHAndBRDCmd")
        self.ticket.update(status="awaiting_file_selection", dispatch_acknowledged=True)

    def terminate(self):
        self.events.append("commandTerminated", command_id="ImportSCHAndBRDCmd")

    def submit_pair(self):
        self.assertEqual(self.import_()["status"], "pending")
        self.open_picker()
        self.assertEqual(self.import_()["status"], "pending")
        self.native.identity = [20, 21, 22]
        self.assertEqual(self.import_()["status"], "pending")
        self.assertEqual(self.native.posts, [[10, 11, 12], [20, 21, 22]])

    def test_rejects_unapproved_and_unpinned_input_before_bridge_access(self):
        nonimportable = self.compile("unrouted", full=True)
        noncompiler = self.root / "ordinary.brd"
        noncompiler.write_bytes(self.candidate.read_artifact("board"))
        attempts = [
            dict(manifest_path=str(nonimportable.path), expected_manifest_sha256=nonimportable.sha256),
            dict(manifest_path=str(noncompiler), expected_manifest_sha256=sha256(noncompiler.read_bytes()).hexdigest()),
            dict(expected_manifest_sha256="0" * 64),
        ]
        for args in attempts:
            with self.subTest(args=args):
                self.assertEqual(self.import_(**args)["status"], "failed")
        (self.candidate.path.parent / "board.svg").write_bytes(b"changed")
        self.assertEqual(self.import_()["status"], "failed")
        self.assertEqual(self.calls, [])

    def test_revalidates_repinned_pair_instead_of_trusting_pass_report(self):
        manifest = self.candidate.data
        board = self.candidate.read_artifact("board").replace(b'layer="20"', b'layer="21"')
        path = self.candidate.path.parent / manifest["artifacts"]["board"]["path"]
        path.write_bytes(board)
        manifest["artifacts"]["board"]["sha256"] = sha256(board).hexdigest()
        raw = canonical_json_bytes(manifest)
        self.candidate.path.write_bytes(raw)
        self.assertEqual(self.import_(expected_manifest_sha256=sha256(raw).hexdigest())["status"], "failed")
        self.assertEqual(self.calls, [])

    def test_import_uses_existing_pair_reader_before_start(self):
        with patch.object(eagle_import, "read_pair", side_effect=ValueError("signature refused")):
            self.assertEqual(self.import_()["status"], "failed")
        self.assertEqual(self.calls, [])

    def test_binds_request_to_exact_candidate_path_and_destination(self):
        self.assertEqual(self.import_()["status"], "pending")
        identical = self.compile("copy")
        self.assertEqual(identical.sha256, self.candidate.sha256)
        for changes in (dict(name="Other"), dict(folder="other"), dict(project_id="other"),
                        dict(manifest_path=str(identical.path)),
                        dict(expected_manifest_sha256=self.candidate.sha256.upper())):
            with self.subTest(changes=changes):
                self.assertEqual(self.import_(**changes)["status"], "failed")
        self.assertEqual(self.calls.count("start_eagle_pair_import"), 1)
        self.assertEqual(self.calls.count("eagle_pair_import_status"), 0)

    def test_advances_once_per_invocation_and_waits_for_importer_termination(self):
        queued = self.import_()
        self.assertEqual(queued["status"], "pending")
        self.assertIn("Resume import_eagle_candidate", queued["next_action"])
        self.assertEqual(self.calls.count("start_eagle_pair_import"), 1)
        self.assertEqual(self.native.posts, [])
        self.calls.clear()
        self.assertEqual(self.import_()["status"], "pending")
        self.assertEqual(self.calls, ["eagle_pair_import_status"])
        self.open_picker()
        self.calls.clear()
        self.assertEqual(self.import_()["status"], "pending")
        self.assertEqual(self.native.posts, [[10, 11, 12]])
        # One orchestration read plus the selector's two guard reads.
        self.assertEqual(self.calls.count("eagle_pair_import_status"), 3)
        self.assertEqual(self.import_()["status"], "pending")
        self.assertEqual(self.native.posts, [[10, 11, 12]])
        self.native.identity = [20, 21, 22]
        self.assertEqual(self.import_()["status"], "pending")
        self.assertEqual(self.native.posts, [[10, 11, 12], [20, 21, 22]])
        self.assertEqual(self.import_()["status"], "pending")
        self.assertNotIn("finish_eagle_pair_import", self.calls)
        self.terminate()
        self.calls.clear()
        receipt = self.import_()
        self.assertEqual(receipt["status"], "saved_linked")
        self.assertEqual(receipt["documents"], self.docs)
        self.assertEqual(self.calls, ["eagle_pair_import_status", "events", "finish_eagle_pair_import"])
        self.calls.clear()
        self.info["token"] = "changed"
        self.assertEqual(self.import_(), receipt)
        self.assertEqual(self.calls, [])

    def test_interrupted_start_never_resubmits(self):
        self.start_error = SystemExit("server exited")
        with self.assertRaises(SystemExit):
            self.import_()
        self.start_error = None
        self.assertEqual(self.import_()["status"], "state_unknown")
        self.calls.clear()
        self.assertEqual(self.import_()["status"], "state_unknown")
        self.assertEqual(self.calls, [])

    def test_crash_during_picker_post_is_consumed(self):
        self.import_()
        self.open_picker()
        with patch.object(self.native, "post", side_effect=SystemExit("server exited")):
            with self.assertRaises(SystemExit):
                self.import_()
        self.assertEqual(self.import_()["status"], "state_unknown")
        self.assertEqual(self.import_()["status"], "state_unknown")
        self.assertEqual(self.native.posts, [])

    def test_reuses_consumed_successful_picker_receipt(self):
        self.import_()
        self.open_picker()
        original = picker.select_file

        def exit_after_post(*args, **kwargs):
            original(*args, **kwargs)
            raise SystemExit("server exited")

        with patch.object(picker, "select_file", side_effect=exit_after_post):
            with self.assertRaises(SystemExit):
                self.import_()
        self.native.identity = [20, 21, 22]
        self.assertEqual(self.import_()["status"], "pending")
        self.assertEqual(self.native.posts, [[10, 11, 12], [20, 21, 22]])

    def test_interrupted_finish_does_not_submit_a_second_save(self):
        self.submit_pair()
        self.terminate()
        self.finish_error = SystemExit("server exited")
        with self.assertRaises(SystemExit):
            self.import_()
        self.finish_error = None
        self.assertEqual(self.import_()["status"], "state_unknown")
        self.assertEqual(self.calls.count("finish_eagle_pair_import"), 1)

    def test_lost_finish_response_can_recover_saved_identities_read_only(self):
        self.submit_pair()
        self.terminate()
        def exit_after_save(op, args, **kwargs):
            result = self.call(op, args, **kwargs)
            if op == "finish_eagle_pair_import":
                raise SystemExit("server exited")
            return result
        with patch.object(self.bridge, "call", side_effect=exit_after_save):
            with self.assertRaises(SystemExit):
                self.import_()
        self.calls.clear()
        result = self.import_()
        self.assertEqual(result["status"], "saved_linked")
        self.assertEqual(result["documents"], self.docs)
        self.assertEqual(self.calls, ["eagle_pair_import_status"])

    def test_rejects_unverified_finish(self):
        self.submit_pair()
        self.terminate()
        self.finish_status = "state_unknown"
        result = self.import_()
        self.assertEqual(result["status"], "state_unknown")
        self.assertIn("get_eagle_pair_import_status", result["next_action"])
        self.assertEqual(self.import_(), result)
        self.assertEqual(self.calls.count("finish_eagle_pair_import"), 1)

    def test_ticket_session_or_destination_change_stops_progress(self):
        for key in ("session", "destination"):
            with self.subTest(key=key):
                self.assertEqual(self.import_(request_id=("b" if key == "session" else "c") * 32)["status"],
                                 "pending")
                self.open_picker()
                if key == "session":
                    self.info["token"] = "changed"
                else:
                    self.ticket["destination"]["folder_id"] = "other-folder"
                self.assertEqual(self.import_(request_id=("b" if key == "session" else "c") * 32)["status"],
                                 "state_unknown")
                self.assertEqual(self.native.posts, [])
                self.info["event_session_id"] = "new-session"
                self.events.session_id = "new-session"

    def test_unproven_command_history_blocks_finish(self):
        self.submit_pair()
        self.events.append("commandStarting", command_id="ImportSCHAndBRDCmd")
        self.terminate()
        self.assertEqual(self.import_()["status"], "state_unknown")
        self.assertNotIn("finish_eagle_pair_import", self.calls)

    def test_non_windows_returns_staged_paths_and_finishes_after_manual_selection(self):
        self.assertEqual(self.import_(windows=False)["status"], "pending")
        self.open_picker()
        result = self.import_(windows=False)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["staged"], self.ticket["staged"])
        self.assertIn("Select the staged schematic and then the board", result["next_action"])
        self.assertEqual(self.native.posts, [])
        self.assertNotIn("finish_eagle_pair_import", self.calls)
        self.terminate()
        self.assertEqual(self.import_(windows=False)["status"], "saved_linked")

    def test_serializes_concurrent_duplicate_requests(self):
        # Set test dependencies once so worker calls only share the production lock.
        from fusion_mcp import candidate_import
        with patch.object(candidate_import, "data_dir", self.data):
            args = (str(self.candidate.path), self.candidate.sha256, "a" * 32, "project", "Product",
                    "existing")
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(server.import_eagle_candidate, *args) for _ in range(2)]
                self.assertEqual([future.result()["status"] for future in futures], ["pending", "pending"])
        self.assertEqual(self.calls.count("start_eagle_pair_import"), 1)
        self.assertEqual(self.calls.count("eagle_pair_import_status"), 1)

    def test_unresolved_start_blocks_fresh_id_in_same_session(self):
        self.start_error = OSError("start response was lost")
        self.assertEqual(self.import_()["status"], "state_unknown")
        self.start_error = None
        self.calls.clear()
        blocked = self.import_(request_id="b" * 32)
        self.assertEqual(blocked["status"], "state_unknown")
        self.assertEqual(blocked["request_id"], "a" * 32)
        self.assertEqual(self.calls, ["eagle_pair_import_status"])

    def test_resolved_previous_ticket_allows_fresh_id(self):
        self.import_()
        self.ticket.update(status="cancelled_native_rejection")
        self.calls.clear()
        result = self.import_(request_id="b" * 32)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.calls.count("eagle_pair_import_status"), 1)
        self.assertEqual(self.calls.count("start_eagle_pair_import"), 1)

    def test_awaits_real_picker_handoff_when_schematic_filter_is_still_visible(self):
        self.import_()
        self.open_picker()
        self.import_()
        original = self.native.find

        def find(pid, extension):
            if extension == ".brd":
                raise ValueError("Picker button or file type does not match the requested selection")
            return original(pid, extension)

        with patch.object(self.native, "find", side_effect=find):
            self.assertEqual(self.import_()["status"], "pending")
        self.assertEqual(self.native.posts, [[10, 11, 12]])

    def test_native_rejection_and_proven_not_started_end_without_automatic_retry(self):
        self.import_()
        self.ticket.update(status="not_started")
        self.assertEqual(self.import_()["status"], "failed")
        self.assertEqual(self.import_()["status"], "failed")
        self.assertEqual(self.calls.count("start_eagle_pair_import"), 1)

    def test_session_rotation_between_journal_and_start_cannot_launch_native_command(self):
        original = picker.begin

        def begin_after_rotation(*args, **kwargs):
            self.info.update(token="new-token", event_session_id="new-session")
            self.events.session_id = "new-session"
            return original(*args, **kwargs)

        with patch.object(picker, "begin", side_effect=begin_after_rotation):
            self.assertEqual(self.import_()["status"], "state_unknown")
        self.assertNotIn("start_eagle_pair_import", self.calls)

    def test_read_only_recovery_of_acknowledged_start_does_not_start_again(self):
        original = picker.begin

        def exit_after_start(*args, **kwargs):
            original(*args, **kwargs)
            raise SystemExit("server exited")

        with patch.object(picker, "begin", side_effect=exit_after_start):
            with self.assertRaises(SystemExit):
                self.import_()
        self.calls.clear()
        self.assertEqual(self.import_()["status"], "pending")
        self.assertEqual(self.calls, ["eagle_pair_import_status"])
        self.assertEqual(self.native.posts, [])

    def test_other_request_observes_pending_without_starting(self):
        self.import_()
        self.calls.clear()
        result = self.import_(request_id="b" * 32, name="Other")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["request_id"], "a" * 32)
        self.assertEqual(self.calls, ["eagle_pair_import_status"])

    def test_import_does_not_compile_or_route(self):
        with patch("fusion_offline.compiler.compile.compile_candidate",
                   side_effect=AssertionError("recompiled")), patch(
                       "fusion_offline.compiler.routing_job.route_all",
                       side_effect=AssertionError("rerouted")):
            self.assertEqual(self.import_()["status"], "pending")
        self.assertEqual(self.calls.count("start_eagle_pair_import"), 1)

    def test_malformed_repinned_routing_intent_returns_failed_before_start(self):
        manifest = self.candidate.data
        intent = json.loads(self.candidate.read_artifact("routing_intent"))
        intent["routing"]["policies"] = ["invalid"]
        raw = canonical_json_bytes(intent)
        artifact = manifest["artifacts"]["routing_intent"]
        (self.candidate.path.parent / artifact["path"]).write_bytes(raw)
        artifact["sha256"] = sha256(raw).hexdigest()
        raw = canonical_json_bytes(manifest)
        self.candidate.path.write_bytes(raw)
        self.assertEqual(self.import_(expected_manifest_sha256=sha256(raw).hexdigest())["status"], "failed")
        self.assertEqual(self.calls, [])

    def test_explicit_original_document_guard_refusal_is_terminal_failed(self):
        from fusion_mcp.bridge import BridgeOpError

        self.start_error = BridgeOpError("fusion_error",
                                         "All open documents must be saved and unmodified before import")
        result = self.import_()
        self.assertEqual(result["status"], "failed")
        self.assertIn("saved and unmodified", result["error"])
        self.assertEqual(self.import_(), result)
        self.assertEqual(self.calls.count("start_eagle_pair_import"), 1)
        self.assertNotIn("finish_eagle_pair_import", self.calls)
        self.assertEqual(self.native.posts, [])

        self.start_error = None
        self.assertEqual(self.import_(request_id="b" * 32)["status"], "pending")

    def assert_cancelled_picker_is_unresolved(self, posts):
        self.native.identity = None
        self.terminate()
        self.calls.clear()
        result = self.import_()
        self.assertEqual(result["status"], "state_unknown")
        self.assertIn("get_eagle_pair_import_status", result["next_action"])
        self.assertEqual(self.calls.count("events"), 1)
        self.assertNotIn("finish_eagle_pair_import", self.calls)
        self.assertEqual(self.native.posts, posts)
        self.calls.clear()
        self.assertEqual(self.import_(), result)
        self.assertEqual(self.calls, [])

    def test_cancellation_before_schematic_selection_stops_waiting(self):
        self.import_()
        self.open_picker()
        self.assert_cancelled_picker_is_unresolved([])

    def test_cancellation_after_schematic_selection_stops_waiting_for_board(self):
        self.import_()
        self.open_picker()
        self.import_()
        self.assert_cancelled_picker_is_unresolved([[10, 11, 12]])
