# Fusion Electronics MCP & AI PCB Builder

Build complete PCB candidates with an AI coding assistant using the **offline EAGLE compiler**. The assistant writes a JSON design spec, then compiles the schematic, board outline, and component placement, routes the board, and reviews HTML/SVG previews and validation results. All of that runs with Fusion closed.

**Start offline for new boards.** Compiling a design in batches is faster than building it through individual live MCP calls because it avoids repeated round trips to Fusion for placement, wiring, and routing. Use the Fusion Electronics MCP tools to import the reviewed candidate as a linked Electronics Design, run native checks, and make targeted edits in Fusion.

- [Build a new board offline](#build-a-new-board-offline)
- [Features](#features)
- [Quick start](#quick-start)
- [Documentation](#documentation)
- [Configuration](#configuration)

![Dual CRPS power-supply backplane designed with Fusion Electronics MCP](docs/images/crps-3d.png)

*A dual CRPS power-supply backplane built with this server, from schematic capture through placement, routing, and manufacturing review.*

<table>
<tr>
<td width="50%"><img src="docs/images/crps-schematic.png" alt="Backplane schematic"><br>Schematic</td>
<td width="50%"><img src="docs/images/crps-layout.png" alt="Routed backplane PCB"><br>PCB layout</td>
</tr>
<tr>
<td width="50%"><img src="docs/images/crps-installed.png" alt="Backplane installed with two power supplies"><br>Installed board</td>
<td width="50%"><img src="docs/images/crps-jlcpcb-preview.png" alt="JLCPCB component placement preview"><br>Assembly preview</td>
</tr>
</table>

## Features

- **Offline PCB builder for AI tools:** Compile a JSON design spec, local templates, and libraries into paired `.sch` and `.brd` files. Plan and review routing, validate the candidate, and import the reviewed result into Fusion.
- **Schematic design:** Place parts and multi-unit symbols, organize functional blocks and sheets, and connect pins with continuous right-angle wires. Preview batch edits and reusable circuit blocks before applying them.
- **Placement and routing:** Arrange support components around ICs, align inline parts, and move components in batches. Route individual nets, differential pairs, parallel buses, and remaining connections; add copper pours, ground vias, and stitching.
- **Keepouts:** Route around supported package and board restrictions, define explicitly authorized header connection corridors, and preflight library keepout changes. Corridors affect offline planning only and do not waive native DRC.
- **Design checks:** Inspect parts, nets, placement, ERC/DRC findings, annular rings, solder mask, and pour clearances. Check differential-pair lengths, estimate impedance and trace widths, and compare designs with reviewed datasheet evidence and project requirements.
- **Parts and libraries:** Search Fusion libraries and the local component catalog. Create symbols and device mappings from KiCad footprints, add package variants and STEP models, and update parts and models in existing designs.
- **Design reuse:** Import schematic connectivity, placement, and routing from KiCad boards. Reuse reviewed circuit blocks with explicit ports and separate internal nets.
- **Manufacturing preparation:** Use bundled JLCPCB rules and two-, four-, and six-layer stackups. Export BOM/CPL files, check assembly orientation, and compare exported Gerbers with the design.
- **Fusion automation and inspection:** Run reviewed ULPs and trusted scripts, manage add-ins, inspect selections and commands, and read lifecycle events. Inspect 3D bodies and measure interference and clearance.

List the available tools with:

```bash
fusion-electronics-mcp tools
```

## Quick start

### 1. Install the server

Requires **Python 3.12+** and **Git**. Live editing and native import also require Autodesk Fusion desktop on Windows or macOS. Use server **0.4.0+** and bundled add-in **0.14.23+** for the compiler/import workflow.

```bash
git clone https://github.com/CircuitSetup/fusion-electronics-mcp.git
cd fusion-electronics-mcp
python -m pip install .
fusion-electronics-mcp --version
```

The base install includes offline HTML/SVG previews. To add live board rendering and PDF inspection:

```bash
python -m pip install ".[render,documents]"
```

### 2. Connect your MCP client

Register `fusion-electronics-mcp` as a local stdio server. For clients that use an `mcpServers` configuration:

```json
{
  "mcpServers": {
    "fusion-electronics": {
      "command": "fusion-electronics-mcp"
    }
  }
}
```

Use the executable's absolute path if your client cannot find it on `PATH`. In Claude Code, you can register it with:

```bash
claude mcp add fusion-electronics -- fusion-electronics-mcp
```

### 3. Connect Fusion

Skip this step for offline work.

```bash
fusion-electronics-mcp install-addin
```

In Fusion, open **Utilities > Add-Ins > Scripts and Add-Ins**, select **FusionElectronicsMCP** on the **Add-Ins** tab, and click **Run**. Enable **Run on Startup** if desired.

Run `fusion-electronics-mcp doctor`, then call `get_context()` from your MCP client to confirm the connection and active design.

When updating, reinstall the package, run `install-addin --force`, stop and restart the add-in, and reconnect your MCP client. Fusion's built-in MCP server is also supported for compatible live operations; paired import requires the bundled add-in.

## Workflows

### Build a new board offline

Give your AI assistant the [detailed offline compiler instructions](docs/offline-compiler.md) and the [LED switch example](tests/fixtures/compiler/minimal/design.json). Have it draft the design spec, run the compiler from the terminal, and iterate on placement and routing using the preview and validation report.

1. **Prepare the specification.** Define the outline, parts, placements, and nets. Reference empty EAGLE schematic/board templates and local `.lbr` files using absolute paths and SHA-256 hashes. The board template supplies the stackup and design rules.
2. **Compile.** Run `compile-candidate` to generate `candidate.sch`, `candidate.brd`, a preview, a validation report, and a manifest.
3. **Route and review.** Use `route-candidate` to plan routing, inspect its preview and diagnostics, then promote the saved result to a new candidate. Keep each manifest path and its returned hash together.
4. **Validate and import.** Use `render-candidate` and `validate-candidate` to review the final preview and findings. Required `fail` or `unverified` findings block import. Set `routing.require_fully_routed` when all connections must be routed.

Pass the final manifest path and hash to `import_eagle_candidate` through MCP. If your assistant uses MCP for the build itself, the offline tools below run the same compiler with Fusion closed. Call `get_offline_compiler_workflow()` to locate the schema and workspace, then use a fresh output directory within that workspace.

| Step | MCP tools |
| --- | --- |
| Compile | `compile_eagle_candidate` |
| Plan and publish routing | `plan_eagle_candidate_routing`, `promote_eagle_candidate_routing` |
| Review | `render_eagle_candidate`, `validate_eagle_candidate` |
| Import | `import_eagle_candidate` |

Before import, save all open Fusion documents and clear pending edits and dialogs. Supply the reviewed manifest and hash, the active project ID, an existing destination folder, an unused design name, and one fresh 32-character lowercase hex request ID.

For `pending`, follow `next_action` and resume with the same arguments. Windows supports automated native file selection; on macOS, select the returned staged schematic and board. `saved_linked` confirms completion. For `state_unknown`, inspect `get_eagle_pair_import_status(request_id)` and Fusion before continuing.

After import, refill polygons where needed, run native ERC/DRC, and inspect airwires, footprints, placement, routing, and 3D models. Offline validation does not replace native or manufacturing review.

### Work on an existing Fusion design

Open the design and call `get_context()` to confirm the document and editor. Use `get_eagle_command_guide()` for supported tools, Electronics commands, and scripts.

For reviewed schematic changes:

1. Call `preview_schematic_edits` or `preview_circuit_block` to generate a preview and plan without changing CAD.
2. Review the proposed sheets, wiring, pin mappings, and before/after changes.
3. Explicitly authorize the exact plan, then apply it with `apply_schematic_plan`.
4. Review the result and run schematic checks and ERC before saving.

Plans reject changed source state and verify each operation. Applying a plan does not save the design. If a timeout, dialog, or mismatch returns `state_unknown`, inspect the result before retrying or undoing. This preview/apply workflow applies to the designated plan tools; other tools retain their own interfaces.

Example requests:

- “Review the schematic for unconnected power pins and wiring problems.”
- “Place the decoupling capacitors around U1 and show the proposed placement.”
- “Check the USB and Ethernet differential-pair lengths and impedance.”
- “Route USB_DP/USB_DN as a 90-ohm pair, matched to 0.1 mm.”
- “Export the JLCPCB BOM and CPL, then check the Gerber ZIP.”

### Import a local EAGLE schematic and board

For an existing pair, use `start_eagle_pair_import` with absolute, same-directory, same-basename `.sch`/`.brd` paths, their SHA-256 hashes, and an explicit destination. Save all open documents first and use one fresh request ID throughout.

Select the staged schematic and board in Fusion, then call `finish_eagle_pair_import`. The importer verifies connectivity and linked document identities before saving. Check progress with `get_eagle_pair_import_status`; an uncertain result must be resolved before starting another import. Hierarchical EAGLE modules are unsupported.

## Libraries and datasheets

Use `search_fusion_libraries` to search Fusion's locally indexed libraries, including inactive entries. Download missing libraries and activate the selected library in Library Manager before placement. `search_library` searches the MCP's separate JSON component catalog.

For custom parts, use a verified KiCad footprint with `create_library_part`, then insert it into an active Fusion library with `insert_library_part`. Save the library before attaching a STEP model where supported, and close it before placing its parts.

Custom definitions support grouped pins, multiple units, package variants, and datasheet references. Multi-unit parts use gate-qualified pins and explicit unit placement; automatic block layout does not support them. Include the full pinout, including NC/DNC and exposed pads. Use `check_datasheet_pins` and `check_project_requirements` to compare the design with reviewed evidence; missing, stale, or unreviewed evidence cannot pass.

Keep project-specific definitions and evidence outside the public repository. Set `FUSION_MCP_LIBRARY` for the default catalog, or pass `library_directory` for a single call.

Optional [Ground Plane Studio libraries](https://github.com/groundplane-studio/fusion-libraries) provide parts, JLCPCB attributes, frames, and power symbols. Upload their `.flbr` files to a Fusion project and enable them in Library Manager. Any compatible library can be used.

See the [datasheet workflow](docs/datasheet-workflow.md) for sourcing parts, inspecting documents, recording requirements, and preparing stackups.

## Manufacturing review

Export Gerbers manually from Fusion's CAM Processor, then run `check_gerbers`. Use `check_jlc_orientation` to compare assembly footprints and apply supported CPL rotation/offset corrections; uncertain matches are marked `needs_review`.

Before ordering, inspect every component in JLCPCB's placement preview, including pin 1, polarity, and pad alignment. Store corrections in the library's `JLC-ROTATION`, `JLC-X-OFFSET`, and `JLC-Y-OFFSET` attributes for future orders.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `FUSION_MCP_TRANSPORT` | `auto` | `addin`, `builtin`, or automatic selection, preferring the add-in |
| `FUSION_MCP_BUILTIN_URL` | `http://127.0.0.1:27182/mcp` | Fusion's built-in MCP endpoint |
| `FUSION_MCP_LIBRARY` | Per-user folder | Component library location |
| `FUSION_MCP_SHEET_FRAME` | None | Sheet frame as `DEVICE@LIBRARY` |
| `FUSION_MCP_GROUND_SYMBOL` | None | Ground symbol as `DEVICE@LIBRARY` |
| `FUSION_MCP_POWER_SYMBOL` | None | Power symbol as `DEVICE@LIBRARY`; value follows the rail name |
| `FUSION_MCP_KEEP_FOCUS` | `1` | Restore keyboard focus after Fusion takes it |
| `FUSION_MCP_LOCK_WAIT_S` | `30` | Seconds to wait for another MCP operation |
| `FUSION_MCP_DRC_AFTER_WRITES` | `1` | Check DRC before/after writes and report new errors |
| `FUSION_MCP_UNDO_ON_NEW_DRC` | `0` | Undo writes that introduce copper DRC errors when set to `1` |
| `FUSION_MCP_EASYEDA_CACHE` | Per-user folder | Downloaded footprint cache |
| `FUSION_MCP_EASYEDA_INTERVAL_S` | `15` | Minimum interval between EasyEDA requests |

The per-user data folder is `%LOCALAPPDATA%\fusion-electronics-mcp` on Windows and `~/Library/Application Support/fusion-electronics-mcp` on macOS.

## Safeguards and limitations

- **Routing needs review.** Whole-board routing still requires guidance. Confirm critical impedance estimates with your manufacturer's calculator.
- **Verification has boundaries.** Unsupported geometry is refused or reported as unverified. Native slot dimensions and CAM cuts require inspection. 3D checks cover currently loaded supported solids and cannot detect missing or outdated models.
- **Checks do not certify hardware.** Pin evidence and project topology checks do not establish electrical performance, creepage, insulation, or regulatory compliance.
- **Fusion compatibility varies.** On Fusion 2706.1.14, `new_design` refuses unverifiable document links and `attach_3d_model` refuses execution by default because of a known native crash. Create, link, and save new design families manually on that build.
- **The bridge is local.** The add-in listens on `127.0.0.1` with a per-session token. Compiler artifacts stay on local disk; imports save to the selected Fusion project. Optional footprint lookup downloads and caches EasyEDA data.
- **Scripts require trust.** Registered ULPs and scripts are not sandboxed. Legacy ULPs and scripts can change or save designs without guaranteed completion or rollback. Review their source before execution.
- **Uncertain operations need inspection.** Check Fusion before repeating an uncertain import, save, or autorouter operation. The dialog watchdog and focus guard are Windows-only.
- **External services are placeholders.** `request_design_review` and `get_assembly_quote` are stubs.

## Documentation

| Guide | Covers |
| --- | --- |
| [Offline compiler](docs/offline-compiler.md) | Specification, CLI/MCP workflow, routing, import, and native acceptance |
| [Fusion/EAGLE commands](server/fusion_mcp/data/eagle-command-guide.md) | Schematic, PCB, library, and script workflows |
| [Datasheet workflow](docs/datasheet-workflow.md) | Part sourcing, evidence, constraints, and stackup preparation |
| [Registered ULPs](docs/registered-ulps.md) | Source registration, reviewed edits, and legacy execution |
| [ULP probe](docs/ulp-probe.md) | Read-only inspection and result limits |
| [Architecture](docs/architecture.md) | Server/add-in design and Fusion integration behavior |

## Development

From the repository root:

```bash
python -m pip install -e ".[render,documents]"
python -m unittest discover -s tests
```

The [live tests](tests/live/) exercise a running Fusion instance. Run write tests only against disposable designs; CAD checks do not establish physical hardware performance.

## About and license

Created by [Ground Plane Studio](https://groundplanestudio.com/), a hardware development team covering industrial design, electronics, firmware, and manufacturing. [Contact the team](https://groundplanestudio.com/contact) for design, review, or production work.

Most of the code was written with Claude Code under engineering direction and review. This is an independent project, not affiliated with or endorsed by Autodesk, JLCPCB, or EasyEDA. Autodesk, Fusion, and EAGLE are trademarks of Autodesk, Inc.

Released under the [MIT License](LICENSE), without warranty. Review designs and manufacturing outputs before fabrication.
