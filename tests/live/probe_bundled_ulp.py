"""Read-only live check on the active board/schematic; writes evidence only to --output.

Run with the updated add-in active:
  python tests/live/probe_bundled_ulp.py --output /path/to/probe.json
"""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from fusion_mcp.server import run_bundled_ulp, session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with session.bridge.lock.hold("live_ulp_probe"):
        before_context = session.context()
        editor = before_context["active_editor"]
        assert editor in ("board", "schematic"), "activate a board or schematic first"
        before = session.export(editor)
        api = {"errors": session.errors(editor)}
        if editor == "board":
            api["layers"] = session.bridge.call("layers")
            api["pours"] = session.bridge.call("pours")
        result = run_bundled_ulp(design=before_context["active_document"]["name"])
        repeat = run_bundled_ulp(design=before_context["active_document"]["name"])
        after = session.export(editor)
        after_context = session.context()
        root = ET.fromstring(before)
        checks = {
            "xml_unchanged": before == after,
            "context_unchanged": before_context == after_context,
            "repeat_data_equal": result["data"] == repeat["data"],
        }
        data = result["data"]
        differences = {}
        if editor == "board":
            ulp_layers = {row["number"]: row for row in data["layers"]}
            api_layers = {row["number"]: row for row in api["layers"]["layers"]}
            differences["api_only_layers"] = sorted(api_layers.keys() - ulp_layers.keys())
            differences["layer_used"] = [
                {"number": n, "name": row["name"], "ulp": row["used"], "api": api_layers[n]["used"]}
                for n, row in ulp_layers.items() if n in api_layers and row["used"] != api_layers[n]["used"]]
            checks.update({
                "elements_match_xml": data["counts"]["elements"] == len(root.findall(".//board/elements/element")),
                "signals_match_xml": data["counts"]["signals"] == len(root.findall(".//board/signals/signal")),
                "layer_names_visibility_match_api": all(
                    n in api_layers and all(row[k] == api_layers[n][k] for k in ("name", "visible"))
                    for n, row in ulp_layers.items()),
                "pours_match_api": data["counts"]["pours"] == len(api["pours"]["pours"]),
                "layers_unchanged": api["layers"] == session.bridge.call("layers"),
                "pours_unchanged": api["pours"] == session.bridge.call("pours"),
            })
        else:
            xml_nets = sorted({node.get("name") for node in root.findall(".//schematic/sheets/sheet/nets/net")})
            differences["nets"] = {"xml_sheet_nets": xml_nets, "ulp_nets": data["nets"],
                                   "ulp_allnets": data["allnets"]}
            differences["xml_modules"] = len(root.findall(".//schematic/modules/module"))
            checks.update({
                "parts_match_xml": data["counts"]["parts"] == len(root.findall(".//schematic/parts/part")),
                "sheets_match_xml": data["counts"]["sheets"] == len(root.findall(".//schematic/sheets/sheet")),
            })
        report = {"fusion": session.bridge.call("ping"), "context": before_context,
                  "probe": result, "api": api, "checks": checks, "differences": differences,
                  "xml_sha256": hashlib.sha256(before).hexdigest(),
                  "xml_grid": root.find(".//grid").attrib if root.find(".//grid") is not None else None}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(args.output), "checks": checks, "counts": data["counts"],
                          "pours": data.get("pours"), "ms": result["ms"]}, indent=2))
        assert all(checks.values()), "live probe comparison failed; inspect evidence"


if __name__ == "__main__":
    main()
