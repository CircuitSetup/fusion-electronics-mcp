"""Persisted routing candidates, pinned promotion and partial outcomes."""
from hashlib import sha256
from itertools import count
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from test_compiler_schema import minimal_design

from fusion_offline.compiler.compile import compile_candidate


def pin(raw):
    return sha256(raw).hexdigest()


class RoutingJobsTest(unittest.TestCase):
    def setUp(self):
        from fusion_offline.compiler import routing_job

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.service = routing_job

    def compile(self, name="base", *, fully_routed=False, protected=(), suffix=b""):
        spec = minimal_design()
        spec["routing"]["selected_nets"] = sorted({"GND", "VCC"} - set(protected))
        spec["routing"]["require_fully_routed"] = fully_routed
        spec["routing"]["protected_nets"] = list(protected)
        source = self.root / "design.json"
        raw = json.dumps(spec).encode() + suffix
        source.write_bytes(raw)
        return compile_candidate(source, pin(raw), self.root / name)

    def plan(self, base, nets=None, **kwargs):
        return self.service.plan_routing_job(base.path, base.sha256, nets, **kwargs)

    def promote(self, base, job, name="promoted"):
        return self.service.promote_routing_job(
            base.path, base.sha256, job.path, job.sha256, self.root / name,
        )

    def test_exact_bytes_are_persisted_and_consumed_with_one_search(self):
        base = self.compile()
        with patch.object(self.service, "route_all", wraps=self.service.route_all) as router:
            job = self.plan(base, ["VCC"])
            self.assertEqual(job.path.parent.name, job.data["job_id"])
            self.assertEqual(job.path.parent.parent.name, "routing-jobs")
            self.assertEqual(job.path.name, "job-manifest.json")
            self.assertEqual(job.read_artifact("input_board"), base.read_artifact("board"))
            self.assertEqual(job.data["input_board_sha256"], pin(base.read_artifact("board")))
            self.assertEqual(job.data["request_sha256"], pin(job.read_artifact("request")))
            self.assertEqual(job.data["candidate_board_sha256"], pin(job.read_artifact("candidate_board")))
            request = json.loads(job.read_artifact("request"))
            self.assertEqual(job.data["policy_sha256"], pin(json.dumps(
                request["policy"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode()))
            self.assertEqual(request["policy"]["selected_nets"], ["VCC"])
            self.assertEqual(self.plan(base, ["VCC"]).sha256, job.sha256)
            loaded = self.service.load_routing_job(job.path, job.sha256)
            self.assertEqual(loaded.data, job.data)
            self.assertEqual(self.service.preview_routing_job(job.path, job.sha256), job.read_artifact("preview"))
            promoted = self.promote(base, job)
            self.assertEqual(promoted.read_artifact("board"), job.read_artifact("candidate_board"))
            self.assertEqual(promoted.read_artifact("preview"), job.read_artifact("preview"))
            self.assertEqual(promoted.data["routing_job"]["manifest_sha256"], job.sha256)
            self.assertEqual(json.loads(promoted.read_artifact("report"))["airwires_by_net"]["VCC"], 0)
            self.assertEqual(router.call_count, 1)
        board = ET.fromstring(job.read_artifact("candidate_board"))
        self.assertFalse(any("mcp_conn" in node.attrib for node in board.iter()))
        routes = json.loads(job.read_artifact("routes"))
        self.assertTrue(routes["provenance"])
        self.assertEqual(routes["failed"], {})

    def test_same_request_in_two_directories_has_identical_bytes(self):
        base = self.compile()
        first = self.plan(base, ["VCC", "GND"])
        second = self.plan(base, ["GND", "VCC"], output_directory=self.root / "alternate")
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(first.sha256, second.sha256)
        for key in first.data["artifacts"]:
            self.assertEqual(first.read_artifact(key), second.read_artifact(key))

    def test_identical_boards_from_distinct_base_manifests_get_separate_jobs(self):
        first = self.compile("first")
        second = self.compile("second", suffix=b"\n")
        self.assertEqual(first.read_artifact("board"), second.read_artifact("board"))
        self.assertNotEqual(first.sha256, second.sha256)
        left, right = self.plan(first, []), self.plan(second, [])
        self.assertEqual(left.data["policy_sha256"], right.data["policy_sha256"])
        self.assertEqual(left.path.parent.parent, right.path.parent.parent)
        self.assertNotEqual(left.data["job_id"], right.data["job_id"])
        self.assertEqual(self.plan(first, []).sha256, left.sha256)
        self.assertEqual(self.plan(second, []).sha256, right.sha256)

    def test_equal_boards_from_distinct_routing_jobs_get_separate_candidate_ids(self):
        base = self.compile()
        unselected = self.plan(base, [])
        expired = self.plan(base, ["VCC"], clock=count(0, 1_000_000_000_000).__next__)
        first = self.promote(base, unselected, "first")
        second = self.promote(base, expired, "second")
        self.assertEqual(first.read_artifact("board"), second.read_artifact("board"))
        self.assertNotEqual(first.data["routing_job"]["manifest_sha256"],
                            second.data["routing_job"]["manifest_sha256"])
        self.assertNotEqual(first.data["candidate_id"], second.data["candidate_id"])

    def test_planning_keeps_the_candidate_directory_immutable(self):
        base = self.compile()
        contents = set(base.path.parent.iterdir())
        job = self.plan(base, ["VCC"])
        self.assertEqual(set(base.path.parent.iterdir()), contents)
        self.assertEqual(job.path.parent.parent, self.root / "routing-jobs")
        with self.assertRaisesRegex(ValueError, "candidate directory"):
            self.plan(base, output_directory=base.path.parent / "nested")
        self.assertEqual(set(base.path.parent.iterdir()), contents)

    def test_promotion_cannot_write_inside_its_inputs(self):
        base = self.compile()
        job = self.plan(base, ["VCC"])
        for parent in (base.path.parent, job.path.parent):
            with self.subTest(parent=parent):
                output = parent / "nested"
                with self.assertRaisesRegex(ValueError, "input directory"):
                    self.service.promote_routing_job(base.path, base.sha256, job.path, job.sha256, output)
                self.assertFalse(output.exists())

    def test_all_pins_and_complete_directory_are_required(self):
        base = self.compile()
        job = self.plan(base)
        for key, record in job.data["artifacts"].items():
            path = job.path.parent / record["path"]
            raw = path.read_bytes()
            with self.subTest(key=key):
                path.write_bytes(raw + b" ")
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    self.service.load_routing_job(job.path, job.sha256)
                with self.assertRaises(ValueError):
                    self.plan(base)
                self.assertEqual(path.read_bytes(), raw + b" ")
                path.write_bytes(raw)
        raw = job.path.read_bytes()
        job.path.write_bytes(raw + b" ")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.promote(base, job)
        job.path.write_bytes(raw)
        (job.path.parent / job.data["artifacts"]["routes"]["path"]).unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.service.load_routing_job(job.path, job.sha256)

    def test_other_base_pin_cannot_promote_job_even_with_same_board(self):
        base = self.compile()
        job = self.plan(base)
        other = self.compile("other", fully_routed=True)
        self.assertEqual(other.read_artifact("board"), base.read_artifact("board"))
        with self.assertRaisesRegex(ValueError, "base"):
            self.promote(other, job)
        (base.path.parent / base.data["artifacts"]["board"]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.promote(base, job)

    def test_independent_routes_survive_a_protected_connection(self):
        for require, status in ((False, "pass"), (True, "fail")):
            with self.subTest(require_fully_routed=require):
                base = self.compile(f"base-{require}", fully_routed=require, protected=["GND"])
                job = self.plan(base, ["GND", "VCC"])
                outcome = json.loads(job.read_artifact("routes"))
                diagnostics = json.loads(job.read_artifact("diagnostics"))
                self.assertTrue(outcome["routes"])
                self.assertTrue(outcome["failed"])
                self.assertIn("protected_net", {row["termination"] for row in diagnostics.values()})
                self.assertTrue(all("elapsed_ms" not in row for row in diagnostics.values()))
                promoted = self.promote(base, job, f"promoted-{require}")
                report = json.loads(promoted.read_artifact("report"))
                self.assertEqual(report["status"], status)
                self.assertEqual(report["airwires_by_net"]["VCC"], 0)
                self.assertEqual(report["airwires_by_net"]["GND"], 1)

    def test_job_deadline_publishes_an_outcome_for_every_connection(self):
        base = self.compile()
        job = self.plan(base, clock=count(0, 1_000_000_000_000).__next__)
        request = json.loads(job.read_artifact("request"))
        diagnostics = json.loads(job.read_artifact("diagnostics"))
        self.assertEqual(set(diagnostics), {row["id"] for row in request["connections"]})
        self.assertEqual({row["termination"] for row in diagnostics.values()}, {"job_deadline"})
        self.assertEqual(job.read_artifact("candidate_board"), job.read_artifact("input_board"))

    def test_re_pinned_job_route_records_must_match_saved_copper(self):
        base = self.compile()
        job = self.plan(base, ["VCC"])
        data = job.data
        outcome = json.loads(job.read_artifact("routes"))
        next(iter(outcome["routes"].values()))["legs"] = []
        raw = json.dumps(outcome, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        record = data["artifacts"]["routes"]
        (job.path.parent / record["path"]).write_bytes(raw)
        record["sha256"] = pin(raw)
        manifest = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        job.path.write_bytes(manifest)
        with self.assertRaisesRegex(ValueError, "route geometry"):
            self.service.load_routing_job(job.path, pin(manifest))

    def test_re_pinned_job_cannot_change_an_unselected_net(self):
        base = self.compile()
        job = self.plan(base, ["VCC"])
        data = job.data
        board = ET.fromstring(job.read_artifact("candidate_board"))
        signal = board.find(".//signal[@name='GND']")
        ET.SubElement(signal, "wire", x1="1", y1="1", x2="2", y2="2", width="0.2", layer="1")
        raw = ET.tostring(board)
        record = data["artifacts"]["candidate_board"]
        (job.path.parent / record["path"]).write_bytes(raw)
        data["candidate_board_sha256"] = record["sha256"] = pin(raw)
        manifest = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        job.path.write_bytes(manifest)
        with self.assertRaises(ValueError):
            self.service.promote_routing_job(base.path, base.sha256, job.path, pin(manifest), self.root / "bad")
        self.assertFalse((self.root / "bad").exists())


    def test_promoting_a_second_job_preserves_original_copper_and_provenance(self):
        base = self.compile()
        first = self.plan(base, ["VCC"])
        promoted = self.promote(base, first)
        second = self.plan(promoted, ["GND"])
        final = self.promote(promoted, second, "final")
        earlier = json.loads(promoted.read_artifact("routing_provenance"))
        final_owners = json.loads(final.read_artifact("routing_provenance"))
        self.assertEqual({key: final_owners[key] for key in earlier}, earlier)
        self.assertEqual({row["net"] for row in final_owners.values()}, {"GND", "VCC"})
        for job_key, candidate_key in (("request", "routing_request"), ("routes", "routing_routes"),
                                       ("diagnostics", "routing_diagnostics")):
            self.assertEqual(second.read_artifact(job_key), final.read_artifact(candidate_key))
        before, after = (ET.fromstring(item.read_artifact("board")) for item in (promoted, final))
        query = ".//signal[@name='VCC']"
        self.assertEqual(ET.tostring(before.find(query)), ET.tostring(after.find(query)))

    def test_external_provenance_bounds_vias_per_connection(self):
        from fusion_offline.compiler.board import route_geometry
        from fusion_offline.compiler.validate import validate_candidate

        base = self.compile()
        board = ET.fromstring(base.read_artifact("board"))
        signal = board.find(".//signal[@name='VCC']")
        intent = json.loads(base.read_artifact("routing_intent"))
        intent["routing"]["budget"]["max_vias_per_connection"] = 1
        for a, b, layer in (((4, 10), (8, 10), 1), ((8, 10), (8, 21), 16), ((8, 21), (14.05, 21), 1)):
            ET.SubElement(signal, "wire", x1=str(a[0]), y1=str(a[1]), x2=str(b[0]), y2=str(b[1]),
                          width="0.25", layer=str(layer))
        vias = [ET.SubElement(signal, "via", x="8", y=str(y), extent="1-16", drill="0.3", diameter="0.6")
                for y in (10, 21)]
        provenance = {f"connection-{index}": {"net": "VCC", "geometry": [route_geometry(via)]}
                      for index, via in enumerate(vias)}

        def check(owners):
            report = validate_candidate(base.read_artifact("schematic"), ET.tostring(board),
                                        intent, provenance=owners)
            return next(row for row in report["findings"] if row["check"] == "routing_geometry")

        self.assertEqual(check(provenance)["status"], "pass")
        self.assertIn("exceeds its via budget", check({})["detail"])
        vias[0].set("diameter", "0.7")
        with self.assertRaisesRegex(ValueError, "saved copper geometry"):
            check(provenance)
