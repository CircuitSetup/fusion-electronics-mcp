"""Offline MCP adapters for pinned EAGLE compiler artifacts."""
from __future__ import annotations

from functools import wraps
import json
from pathlib import Path
import stat
from xml.etree.ElementTree import ParseError

from mcp.server.mcpserver.exceptions import ToolError

from fusion_offline.compiler import SPEC_VERSION
from fusion_offline.compiler.compile import compile_candidate
from fusion_offline.compiler.manifest import MANIFEST_FILENAME, ArtifactManifest, CandidateManifest, load_candidate_manifest
from fusion_offline.compiler.paths import _checked_stat, _local_path, normalize_artifact_path
from fusion_offline.compiler.render import render_candidate
from fusion_offline.compiler.routing_job import JOB_MANIFEST_FILENAME, plan_routing_job, promote_routing_job
from fusion_offline.routing_budget import MAX_SEARCH_STATES, ROUTING_BUDGET_DEFAULTS, ROUTING_BUDGET_MAXIMUMS

from . import data_dir


_ROUTING_LIMITS = {"node_limit", "attempt_limit", "time_limit", "job_deadline", "max_vias"}


def _input_errors(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, ParseError, FileNotFoundError, FileExistsError, NotADirectoryError,
                IsADirectoryError, PermissionError) as error:
            raise ToolError(str(error)) from None
    return call


def _workspace() -> Path:
    return _local_path(data_dir("offline-compiler"), require_absolute=True)


def _output_directory(directory: str | None, *, routing: bool = False) -> Path:
    root = _workspace()
    if directory is None and routing:
        target = root
    elif not directory:
        raise ValueError("output directory must name a path under the compiler workspace")
    else:
        path = Path(directory)
        target = _local_path(path if path.is_absolute() else root / normalize_artifact_path(directory))
        if not target.is_relative_to(root) or (target == root and not routing):
            raise ValueError(f"output directory must be inside the compiler workspace: {root}")
        if target != root:
            normalize_artifact_path(target.relative_to(root).as_posix())

    # Check each existing ancestor before descending through it. Only the workspace
    # and its missing parents are created here; the compiler publishes the artifacts.
    destination = target / "routing-jobs" if routing else target
    for path in (*reversed(destination.parents), destination):
        try:
            mode = _checked_stat(path).st_mode
        except FileNotFoundError:
            if not root.is_relative_to(path):
                break
            path.mkdir(exist_ok=True)
            mode = _checked_stat(path).st_mode
        if not stat.S_ISDIR(mode):
            raise ValueError(f"workspace output parent must be a directory: {path}")
        for marker in (MANIFEST_FILENAME, JOB_MANIFEST_FILENAME):
            try:
                (path / marker).lstat()
            except FileNotFoundError:
                continue
            raise ValueError(f"cannot write inside a published candidate or routing job: {path}")
    return target


def workflow() -> dict:
    return {
        "schema_path": str(Path(__file__).with_name("data") / "offline-design.schema.json"),
        "schema_version": SPEC_VERSION,
        "workspace": str(_workspace()),
        "supported": [
            "Flat EAGLE schematic/board candidates from local hash-pinned empty templates and EAGLE libraries",
            "Multiple sheets, multi-unit parts, explicit placements, nets, keepouts and pour declarations",
            "Bounded selected-net routing, immutable routing jobs, offline validation and SVG/HTML review",
        ],
        "unsupported": [
            "Hierarchical EAGLE modules, library acquisition and automatic part substitution",
            "Blind or buried vias, arbitrary native slot geometry checks and actual polygon fill verification",
            "Native Fusion ERC/DRC, manufacturing approval and datasheet compliance",
        ],
        "sequence": [
            "compile_eagle_candidate",
            "plan_eagle_candidate_routing (optional; inspect the saved job preview)",
            "promote_eagle_candidate_routing (optional; uses the exact saved job)",
            "render_eagle_candidate",
            "validate_eagle_candidate and review the exact manifest and artifacts",
            "import_eagle_candidate (explicit native import after review)",
        ],
        "routing_budget": {
            "defaults": dict(ROUTING_BUDGET_DEFAULTS),
            "maximums": dict(ROUTING_BUDGET_MAXIMUMS),
            "max_search_states": MAX_SEARCH_STATES,
        },
        "output_policy": "Use a relative or contained absolute path with an existing parent in the workspace.",
        "fusion_writes": False,
        "fusion_effects": "Offline compilation and routing never open, alter, or save a Fusion document.",
    }


def _receipt(handle: ArtifactManifest, data: dict) -> dict:
    artifacts = {key: {**entry, "path": str(handle.path.parent / entry["path"])}
                 for key, entry in data["artifacts"].items()}
    return {
        "manifest_path": str(handle.path), "manifest_sha256": handle.sha256,
        "artifacts": artifacts, "preview_path": artifacts["preview"]["path"],
    }


def _routing_outcome(handle: ArtifactManifest, prefix: str = "") -> dict:
    diagnostics = json.loads(handle.read_artifact(f"{prefix}diagnostics"))
    outcome = json.loads(handle.read_artifact(f"{prefix}routes"))
    status = "pass"
    if outcome["failed"]:
        status = "partial" if any(row["termination"] in _ROUTING_LIMITS
                                  for row in diagnostics.values()) else "fail"
    return {"routing_status": status, "failed_connections": outcome["failed"], "diagnostics": diagnostics}


def _candidate_receipt(candidate: CandidateManifest) -> dict:
    data = candidate.data
    result = _receipt(candidate, data)
    result.update(candidate_id=data["candidate_id"], status=data["status"],
                  validation_status=data["status"], findings=data["findings"])
    if job := data["routing_job"]:
        result.update(job_id=job["job_id"], job_manifest_sha256=job["manifest_sha256"],
                      **_routing_outcome(candidate, "routing_"))
    return result


@_input_errors
def compile_eagle(spec_path: str, spec_sha256: str, output_directory: str) -> dict:
    return _candidate_receipt(compile_candidate(spec_path, spec_sha256, _output_directory(output_directory)))


@_input_errors
def plan_routing(manifest_path: str, manifest_sha256: str, selected_nets: list[str] | None,
                 output_directory: str | None) -> dict:
    job = plan_routing_job(manifest_path, manifest_sha256, selected_nets,
                           output_directory=_output_directory(output_directory, routing=True))
    data = job.data
    result = _receipt(job, data)
    result.update(_routing_outcome(job))
    return dict(result, status=result["routing_status"], job_id=data["job_id"],
                job_manifest_path=result["manifest_path"], job_manifest_sha256=job.sha256)


@_input_errors
def promote_routing(manifest_path: str, manifest_sha256: str, job_manifest_path: str,
                    job_manifest_sha256: str, output_directory: str) -> dict:
    candidate = promote_routing_job(manifest_path, manifest_sha256, job_manifest_path,
                                    job_manifest_sha256, _output_directory(output_directory))
    return dict(_candidate_receipt(candidate), job_manifest_path=job_manifest_path)


@_input_errors
def render(manifest_path: str, manifest_sha256: str) -> dict:
    return _candidate_receipt(render_candidate(manifest_path, manifest_sha256))


@_input_errors
def validate(manifest_path: str, manifest_sha256: str) -> dict:
    candidate = load_candidate_manifest(manifest_path, manifest_sha256)
    return dict(_candidate_receipt(candidate), report=json.loads(candidate.read_artifact("report")))
