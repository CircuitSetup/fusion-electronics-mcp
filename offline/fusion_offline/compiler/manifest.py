"""Immutable candidate publication and verification."""
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re

from . import COMPILER_CONTRACT_VERSION, MANIFEST_VERSION, SPEC_VERSION
from .canonical import canonical_digest, canonical_json_bytes
from .paths import _local_path, atomic_directory, atomic_write, normalize_artifact_path, read_pinned_file

COMPILER_VERSION = "0.3.0+workflow.35"
MANIFEST_FILENAME = "candidate-manifest.json"
_ARTIFACT_NAMES = {
    "schematic": "candidate.sch", "board": "candidate.brd", "report": "validation.json",
    "preview": "board.svg", "routing_intent": "routing-intent.json",
}
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_REQUIRED_CHECKS = ("pair_signature", "contacts", "outline", "placements", "routing_geometry", "keepouts")


def _pin(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid canonical SHA-256 pin")


def _identity(inputs, routing_job):
    return canonical_digest({
        "inputs": inputs, "compiler_contract_version": COMPILER_CONTRACT_VERSION,
        "routing_candidate_sha256": routing_job["candidate_board_sha256"] if routing_job else None,
    })


def _checked_artifacts(artifacts):
    if not isinstance(artifacts, dict) or not _ARTIFACT_NAMES.keys() <= artifacts.keys():
        raise ValueError("candidate manifest is missing required artifacts")
    seen = {MANIFEST_FILENAME.casefold()}
    for entry in artifacts.values():
        if not isinstance(entry, dict) or entry.keys() != {"path", "sha256"}:
            raise ValueError("invalid candidate artifact record")
        path = entry["path"]
        if not isinstance(path, str) or normalize_artifact_path(path) != path:
            raise ValueError("candidate artifact path must be normalized and relative")
        if path.casefold() in seen:
            raise ValueError("candidate artifact paths must be unique")
        seen.add(path.casefold())
        _pin(entry["sha256"])
    sch, board = (PurePosixPath(artifacts[name]["path"]) for name in ("schematic", "board"))
    if sch.suffix != ".sch" or board.suffix != ".brd" or sch.with_suffix(".brd") != board:
        raise ValueError("candidate pair must have the same directory and basename")


def _checked_inputs(inputs):
    pins = ("spec_sha256", "template_schematic_sha256", "template_board_sha256")
    if (not isinstance(inputs, dict) or inputs.keys() != {*pins, "spec_version", "libraries"}
            or type(inputs["spec_version"]) is not int or inputs["spec_version"] != SPEC_VERSION
            or not isinstance(inputs["libraries"], list)):
        raise ValueError("invalid candidate input pins")
    for key in pins:
        _pin(inputs[key])
    libraries = inputs["libraries"]
    names = []
    for record in libraries:
        if (not isinstance(record, dict) or record.keys() != {"name", "sha256"}
                or not isinstance(record["name"], str) or not record["name"]):
            raise ValueError("invalid candidate library pin")
        _pin(record["sha256"])
        names.append(record["name"])
    if names != sorted(names) or len({name.casefold() for name in names}) != len(names):
        raise ValueError("candidate library pins must have unique, sorted names")


def _checked_job(routing_job, board_pin):
    if routing_job is None:
        return
    if (not isinstance(routing_job, dict)
            or not {"job_id", "candidate_board_sha256"} <= routing_job.keys()
            or routing_job.keys() - {"job_id", "candidate_board_sha256", "manifest_sha256"}):
        raise ValueError("invalid candidate routing job record")
    for value in routing_job.values():
        _pin(value)
    if routing_job["candidate_board_sha256"] != board_pin:
        raise ValueError("routing job board SHA-256 does not match candidate board")


def finding_status(findings):
    required = {finding["status"] for finding in findings if finding["required"]}
    return "fail" if "fail" in required else "unverified" if "unverified" in required else "pass"


def _checked_manifest(data):
    fields = {"manifest_version", "compiler_version", "candidate_id", "status",
              "inputs", "artifacts", "routing_job", "findings"}
    if (not isinstance(data, dict) or data.keys() != fields
            or type(data["manifest_version"]) is not int or data["manifest_version"] != MANIFEST_VERSION
            or data["compiler_version"] != COMPILER_VERSION):
        raise ValueError("unsupported candidate manifest shape or version")
    _checked_artifacts(data["artifacts"])
    _checked_inputs(data["inputs"])
    _checked_job(data["routing_job"], data["artifacts"]["board"]["sha256"])
    if data["candidate_id"] != _identity(data["inputs"], data["routing_job"]):
        raise ValueError("candidate identity does not match input pins")
    findings = data["findings"]
    if (not isinstance(findings, list) or not findings or any(
        not isinstance(row, dict) or row.keys() != {"check", "status", "required", "detail"}
        or row["status"] not in ("pass", "fail", "unverified") or type(row["required"]) is not bool
        or not isinstance(row["check"], str) or not isinstance(row["detail"], str) for row in findings
    ) or data["status"] != finding_status(findings)):
        raise ValueError("candidate findings do not support its validation status")
    by_check = {}
    for row in findings:
        check = row["check"]
        if check in by_check:
            raise ValueError(f"candidate has a duplicate {check} finding")
        by_check[check] = row["required"]
    for check in _REQUIRED_CHECKS:
        if not by_check.get(check):
            raise ValueError(f"candidate must include a required {check} finding")
    if "full_routing" not in by_check:
        raise ValueError("candidate must include a full_routing finding")
    return by_check["full_routing"]


@dataclass(frozen=True)
class CandidateManifest:
    path: Path
    sha256: str
    _data: dict = field(repr=False)

    @property
    def data(self) -> dict:
        return deepcopy(self._data)

    def read_artifact(self, key: str) -> bytes:
        entry = self._data["artifacts"][key]
        return read_pinned_file(self.path.parent / entry["path"], entry["sha256"], _MAX_ARTIFACT_BYTES)


def load_candidate_manifest(path: str | Path, expected_sha256: str) -> CandidateManifest:
    """Read the pinned manifest and verify all contained artifact paths and hashes."""
    path = _local_path(path)
    raw = read_pinned_file(path, expected_sha256, _MAX_MANIFEST_BYTES)
    data = json.loads(raw)
    if canonical_json_bytes(data) != raw:
        raise ValueError("candidate manifest must use canonical JSON")
    full_routing_required = _checked_manifest(data)
    manifest = CandidateManifest(path, expected_sha256.lower(), data)
    for key in data["artifacts"]:
        artifact = manifest.read_artifact(key)
        if key == "report":
            report = json.loads(artifact)
            if not isinstance(report, dict) or any(
                report.get(field) != data[field] for field in ("status", "findings")
            ):
                raise ValueError("candidate report and manifest validation disagree")
        if key == "routing_intent":
            intent = json.loads(artifact)
            try:
                expected = intent["routing"]["require_fully_routed"]
            except (KeyError, TypeError):
                raise ValueError("candidate routing intent is missing require_fully_routed") from None
            if expected is not full_routing_required:
                raise ValueError("candidate full_routing finding disagrees with the pinned routing intent")
    return manifest


def publish_candidate(output_directory, *, inputs, artifacts, report, routing_job=None) -> CandidateManifest:
    """Publish the exact provided artifacts and their manifest in one rename."""
    target = _local_path(output_directory)
    records = {key: {"path": _ARTIFACT_NAMES[key], "sha256": sha256(raw).hexdigest()}
               for key, raw in artifacts.items()}
    data = {
        "manifest_version": MANIFEST_VERSION,
        "compiler_version": COMPILER_VERSION,
        "candidate_id": _identity(inputs, routing_job),
        "status": report["status"],
        "inputs": inputs,
        "artifacts": records,
        "routing_job": routing_job,
        "findings": report["findings"],
    }
    _checked_manifest(data)
    manifest_bytes = canonical_json_bytes(data)
    pin = sha256(manifest_bytes).hexdigest()
    with atomic_directory(target) as staging:
        for key, raw in artifacts.items():
            atomic_write(staging / records[key]["path"], raw)
        atomic_write(staging / MANIFEST_FILENAME, manifest_bytes)
        load_candidate_manifest(staging / MANIFEST_FILENAME, pin)
    return CandidateManifest(target / MANIFEST_FILENAME, pin, data)
