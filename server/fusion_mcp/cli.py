"""Command line: run the MCP server, install the Fusion add-in, check the setup.

    fusion-electronics-mcp                  run the MCP server (stdio), what MCP clients start
    fusion-electronics-mcp install-addin    put the Fusion add-in in Fusion's AddIns folder
    fusion-electronics-mcp doctor           check Python, the add-in, Fusion and optional pieces
    fusion-electronics-mcp tools            list the MCP tools
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from xml.etree.ElementTree import ParseError

from . import PROJECT_URL, __version__

ADDIN_NAME = "FusionElectronicsMCP"
BUNDLED_ADDIN = os.path.join(os.path.dirname(__file__), "addin", ADDIN_NAME)


def fusion_addins_dir() -> str:
    """Fusion's per-user AddIns folder (Windows and macOS)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
        return os.path.join(base, "Autodesk", "Autodesk Fusion 360", "API", "AddIns")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns")
    raise SystemExit("Autodesk Fusion runs on Windows and macOS only.")


def manifest_version(folder: str) -> str | None:
    try:
        with open(os.path.join(folder, f"{ADDIN_NAME}.manifest"), encoding="utf-8") as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


def _link(src: str, dst: str) -> None:
    if sys.platform == "win32":
        import _winapi                      # a junction needs no admin rights, unlike a symlink
        _winapi.CreateJunction(src, dst)
    else:
        os.symlink(src, dst, target_is_directory=True)


def install_addin(link: bool = False, force: bool = False) -> int:
    dest_root = fusion_addins_dir()
    os.makedirs(dest_root, exist_ok=True)
    folders = [(os.path.join(os.path.dirname(BUNDLED_ADDIN), name), os.path.join(dest_root, name))
               for name in (ADDIN_NAME, 'FusionElectronicsMCPReload')]
    for src, dest in folders:
        same = os.path.realpath(dest) == os.path.realpath(src)
        if os.path.lexists(dest) and not force and not (same and link):
            print(f"{dest} already exists. Run again with --force to replace it.")
            return 1
    for src, dest in folders:
        same = os.path.realpath(dest) == os.path.realpath(src)
        if same and link:
            print(f"Already linked: {dest} -> {src}")
            continue
        if os.path.lexists(dest):
            if os.path.islink(dest): os.unlink(dest)
            elif os.path.isjunction(dest): os.rmdir(dest)
            elif same: raise ValueError('Refusing to replace the bundled source directory')
            else: shutil.rmtree(dest)
        if link:
            _link(src, dest)
        else:
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", ".vscode", "*.pyc"))
        print(f"{'Linked' if link else 'Copied'}: {dest}")
    print(f"Installed add-in {manifest_version(BUNDLED_ADDIN)} and its reload helper. "
          "Use reload_mcp_addin if 0.14.14+ is already running. For first activation or an older "
          "running version: Fusion > Utilities > Scripts and Add-Ins > Add-Ins > "
          f"{ADDIN_NAME} > Stop then Run (enable Run on Startup).")
    return 0


def doctor() -> int:
    ok = True

    def line(good: bool | None, what: str, detail: str = "") -> None:
        nonlocal ok
        mark = {True: "ok  ", False: "FAIL", None: "info"}[good]
        ok = ok and good is not False
        print(f"[{mark}] {what}" + (f": {detail}" if detail else ""))

    line(sys.version_info >= (3, 12), "Python", sys.version.split()[0] + " (3.12 or newer needed)")
    line(None, "fusion-electronics-mcp", __version__)
    bundled = manifest_version(BUNDLED_ADDIN)
    from .builtin import URL as BUILTIN_URL, BuiltinClient
    from .bridge import Bridge, BridgeOpError, BridgeUnavailable
    builtin = BuiltinClient().available()
    line(True if builtin else None, "Fusion's built-in MCP server (fallback)",
         f"answering at {BUILTIN_URL}" if builtin else
         "not answering (Fusion closed, or its MCP server is off)")
    try:
        dest = os.path.join(fusion_addins_dir(), ADDIN_NAME)
        installed = manifest_version(dest) if os.path.exists(dest) else None
        if installed:
            line(True if installed == bundled else False, "Add-in (recommended)",
                 f"{installed} at {dest}" + ("" if installed == bundled else
                 f"; this package ships {bundled}: fusion-electronics-mcp install-addin --force"))
        else:
            line(False, "Add-in (recommended)",
                 "not installed: fusion-electronics-mcp install-addin, then run it in Fusion (Utilities > "
                 "Add-Ins, Run on Startup)" + ("; the built-in server works meanwhile, without saves of "
                                              "libraries or 3D pushes" if builtin else ""))
    except SystemExit as ex:
        line(False, "Platform", str(ex))
    try:
        b = Bridge(keep_focus=False)
        mode = b.transport()
        info = b.call("ping", {}, timeout=15)
        line(True, "Connected to Fusion", f"via {'the built-in MCP server' if mode == 'builtin' else 'the add-in'}, "
             f"Fusion {info.get('fusion_version')}")
        if mode == "addin" and info.get("addin_version") != bundled:
            line(False, "Running add-in version", f"{info.get('addin_version')} != {bundled}; restart the add-in in Fusion")
    except (BridgeUnavailable, BridgeOpError) as ex:
        line(False, "Connected to Fusion", str(ex))
    from . import dialogs
    line(dialogs.supported() or None, "Dialog watchdog",
         "available" if dialogs.supported() else "Windows only so far: dialogs on this OS need a person")
    try:
        import matplotlib  # noqa: F401
        line(True, "render_board", "matplotlib installed")
    except ImportError:
        line(None, "render_board", 'optional; pip install "fusion-electronics-mcp[render]"')
    from .library import Library
    lib = Library()
    n = len([f for f in os.listdir(lib.directory) if f.endswith(".json")]) if os.path.isdir(lib.directory) else 0
    line(None, "Component library", f"{lib.directory} ({n} parts; FUSION_MCP_LIBRARY to change)")
    from . import easyeda
    line(None, "EasyEDA footprint cache", f"{easyeda.cache_dir()} ({len(os.listdir(easyeda.cache_dir()))} parts)")
    print("\nAll good." if ok else f"\nSomething needs attention (see FAIL lines). Help: {PROJECT_URL}")
    return 0 if ok else 1


def list_tools() -> int:
    import asyncio
    from .server import mcp
    for t in asyncio.run(mcp.list_tools()):
        a = t.annotations
        kind = "read" if a and a.read_only_hint else ("change" if a and a.destructive_hint else "add")
        print(f"{t.name:28s} {kind:6s} {(t.description or '').strip().splitlines()[0]}")
    return 0


_COMPILER_COMMANDS = {
    "compile-candidate": "compile a pinned design specification",
    "route-candidate": "plan routing or promote a pinned routing job",
    "render-candidate": "locate the verified review page",
    "validate-candidate": "read the pinned validation report",
}
_COMPILER_EXIT_CODES = {
    "pass": 0, "invalid_input": 2, "fail": 3, "unverified": 3, "partial": 4, "internal_error": 5,
}
_COMPILER_EXIT_HELP = (
    "Exit codes: 0 pass, 2 invalid input, 3 failed or unverified, "
    "4 budget-limited partial routing, 5 internal error."
)
_ROUTING_LIMITS = {"node_limit", "attempt_limit", "time_limit", "job_deadline", "max_vias"}


def _compiler_parsers(sub):
    for command, description in _COMPILER_COMMANDS.items():
        parser = sub.add_parser(command, help=description, description=description,
                                epilog=_COMPILER_EXIT_HELP)
        source = "spec" if command == "compile-candidate" else "manifest"
        parser.add_argument(f"--{source}", required=True, help="absolute local input path")
        parser.add_argument(f"--{source}-sha256", required=True, help="exact SHA-256 of the input file")
        if command in ("compile-candidate", "route-candidate"):
            parser.add_argument("--output-directory", required=command == "compile-candidate",
                                help="output location (required for promotion)")
        if command == "route-candidate":
            parser.add_argument("--net", action="append", help="select a net for planning (repeatable)")
            parser.add_argument("--job-manifest", help="absolute local routing job manifest path for promotion")
            parser.add_argument("--job-manifest-sha256", help="exact SHA-256 of the routing job manifest")


def _compiler_result(handle, status):
    preview = handle.path.parent / handle.data["artifacts"]["preview"]["path"]
    return {"manifest_path": str(handle.path), "manifest_sha256": handle.sha256,
            "status": status, "preview_path": str(preview)}


def _routing_status(diagnostics):
    terminations = {row["termination"] for row in diagnostics.values()}
    if terminations & _ROUTING_LIMITS:
        return "partial"
    return "fail" if terminations - {"routed"} else "pass"


def _run_compiler(args):
    try:
        from fusion_offline.compiler.compile import compile_candidate
        from fusion_offline.compiler.manifest import load_candidate_manifest
        from fusion_offline.compiler.render import render_candidate
        from fusion_offline.compiler.routing_job import plan_routing_job, promote_routing_job

        if args.cmd == "compile-candidate":
            candidate = compile_candidate(args.spec, args.spec_sha256, args.output_directory)
        elif args.cmd == "route-candidate":
            if args.job_manifest is not None or args.job_manifest_sha256 is not None:
                if not (args.job_manifest and args.job_manifest_sha256 and args.output_directory) or args.net:
                    raise ValueError("promotion requires --job-manifest, --job-manifest-sha256 and "
                                     "--output-directory, and cannot use --net")
                candidate = promote_routing_job(
                    args.manifest, args.manifest_sha256, args.job_manifest,
                    args.job_manifest_sha256, args.output_directory,
                )
            else:
                job = plan_routing_job(args.manifest, args.manifest_sha256, args.net,
                                       output_directory=args.output_directory)
                diagnostics = json.loads(job.read_artifact("diagnostics"))
                result = _compiler_result(job, _routing_status(diagnostics))
                return _emit_compiler_result(dict(result, diagnostics=diagnostics))
        elif args.cmd == "render-candidate":
            candidate = render_candidate(args.manifest, args.manifest_sha256)
        else:
            candidate = load_candidate_manifest(args.manifest, args.manifest_sha256)

        data = candidate.data
        result = _compiler_result(candidate, data["status"])
        if args.cmd == "validate-candidate":
            result["report"] = json.loads(candidate.read_artifact("report"))

        if data["routing_job"]:
            diagnostics = json.loads(candidate.read_artifact("routing_diagnostics"))
            routing_status = _routing_status(diagnostics)
            result.update(routing_status=routing_status, diagnostics=diagnostics)
            blocked_checks = {row["check"] for row in data["findings"]
                              if row["required"] and row["status"] != "pass"}
            if routing_status == "partial" and blocked_checks == {"full_routing"}:
                result["status"] = "partial"

        if data["status"] != "pass":
            result["findings"] = data["findings"]
    except (ValueError, ParseError, FileNotFoundError, FileExistsError, NotADirectoryError,
            IsADirectoryError, PermissionError) as error:
        result = {"status": "invalid_input", "error": str(error)}
    except Exception as error:
        result = {"status": "internal_error", "error": f"{type(error).__name__}: {error}"}
    return _emit_compiler_result(result)


def _emit_compiler_result(result):
    status = result["status"]
    if status != "pass":
        print(result.get("error") or f"Offline compiler outcome: {status}", file=sys.stderr)
        for finding in result.get("findings", ()):
            if finding["required"] and finding["status"] != "pass":
                print(f"{finding['check']}: {finding['detail']}", file=sys.stderr)
        for connection, row in result.get("diagnostics", {}).items():
            if row["termination"] != "routed":
                print(f"{connection}: {row['termination']}", file=sys.stderr)
    print(json.dumps(result))
    return _COMPILER_EXIT_CODES[status]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fusion-electronics-mcp",
                                description="MCP server for Autodesk Fusion Electronics.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    ia = sub.add_parser("install-addin", help="install the Fusion add-in into Fusion's AddIns folder")
    ia.add_argument("--link", action="store_true", help="link instead of copy (for development)")
    ia.add_argument("--force", action="store_true", help="replace an existing install")
    sub.add_parser("doctor", help="check the setup")
    sub.add_parser("tools", help="list the MCP tools")
    sub.add_parser("serve", help="run the MCP server on stdio (the default)")
    _compiler_parsers(sub)
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = p.parse_args(argv)
    except SystemExit as error:
        if error.code != 2 or not argv or argv[0] not in _COMPILER_COMMANDS:
            raise
        print(json.dumps({"status": "invalid_input"}))
        return _COMPILER_EXIT_CODES["invalid_input"]
    if args.cmd in _COMPILER_COMMANDS:
        return _run_compiler(args)
    if args.cmd == "install-addin":
        return install_addin(args.link, args.force)
    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "tools":
        return list_tools()
    from .server import mcp
    mcp.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
