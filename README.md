# fusion-electronics-mcp

An MCP server that lets AI agents read, review and edit PCB designs in
Autodesk Fusion Electronics: schematic capture, placement, impedance-controlled
routing, pours and stitching, design checks, and JLCPCB manufacturing outputs.

Every change is verified by reading the design back from Fusion and is undone if
the result does not match. Each tool call is one undo step in Fusion, and
nothing is saved until you ask.

![A dual CRPS power-supply backplane designed in Fusion through the MCP](docs/images/crps-3d.png)

*A dual CRPS power-supply backplane built in Fusion through this server: schematic drawn
as blocks from a netlist, passives placed by rule, routed with power pours kept whole,
GND stitched, silkscreen and 3D models checked.*

<table>
<tr>
<td width="50%" align="center"><img src="docs/images/crps-layout.png" alt="Routed layout: bus lanes beside the 12 V pour, GND pours and stitching"><br><sub>Layout</sub></td>
<td width="50%" align="center"><img src="docs/images/crps-schematic.png" alt="Block-style schematic: each connector with its passives wired to it"><br><sub>Schematic</sub></td>
</tr>
<tr>
<td width="50%" align="center"><img src="docs/images/crps-installed.png" alt="The backplane in its enclosure, plugged into both CRPS power supplies"><br><sub>Installed with both power supplies</sub></td>
<td width="50%" align="center"><img src="docs/images/crps-jlcpcb-preview.png" alt="JLCPCB's assembly preview of the board, every part matched to its JLCPCB part number"><br><sub>JLCPCB placement preview</sub></td>
</tr>
</table>

## What it can do

- **Review**: board and schematic summaries, parts, nets, DRC and ERC, a
  schematic review (unconnected power pins, boxes drawn as nets, stray wires,
  nets that need labels), a picture of the board (`render_board`), and
  annular rings of every via and through-hole pad (`check_annular_ring`), and
  solder mask openings that merge or leave too thin a dam (`check_solder_mask`).
- **Signal integrity**: differential pairs, length matching, and impedance
  estimates (IPC-2141 closed form) against the design's real layer stackup;
  same-layer pour too close to 50 ohm and USB traces (`check_pour_clearance`).
- **Schematic capture**: place parts, connect pins (labels added
  automatically), rename nets or single segments, add sheets with your frame.
  Draw a whole schematic from a KiCad board as reviewable blocks: each IC or
  connector with its passives wired to it, labels only where a net leaves the
  block, rails as power symbols, with a preview before anything is drawn.
- **Placement**: move and rotate parts, import placement or netlists from a
  KiCad board, score placements and suggest moves, and place passives around
  the part they serve by rule (`place_clusters`: pull-ups and series parts on
  their pin's escape line, decaps first, bridges along the package edge, LED
  and FET chains following the part they hang off), previewed before moving.
  Check placement (courtyards, pad gaps, silkscreen, tidiness), move parts in
  batches as one undo step, line series parts up on their pads
  (`place_inline`) and tidy to a grid without touching critical parts.
- **Net classes and rules**: list, create (as a rule file you load) and assign
  net classes; teardrop and pair rules; JLCPCB limits; DRC after every write
  reporting only new errors.
- **Length and current**: length groups through series parts with cited
  typical tolerances, coupled detours for matching, trace widths from a net's
  current and the weakest part's rating.
- **Routing**, in the order a person routes a board: pours (with priority
  ranks), a GND via beside every ground pad (`ground_vias`), all the short
  pin-to-pin hops at once (`route_close`), buses laid as ordered parallel lanes
  along a path you choose (`lay_bus`), then everything left with a router that
  rips up and reroutes when blocked (`route_remaining`). Routes are 45-degree,
  keep out of connector pin fields, avoid cutting power pours, and are smoothed;
  every write is checked (connections made, no layer change without a via).
  Also single connections or nets (`route_trace`, `route_net`), impedance-
  controlled differential pairs (`route_pair`), via stitching that keeps off
  parts and other nets' pours, copying a KiCad board's routing
  (`import_routing_from_kicad`), and Fusion's autorouter.
  Routing is still in development: these tools route boards and pours today,
  step by step with you reviewing each stage, but routing a whole board on its
  own, start to finish, is not there yet.
- **Parts and libraries**: build parts from a JSON definition into a Fusion
  library (pads and pin connections verified), attach 3D models (refused if
  the body would sit upside down), update a design from its libraries
  (re-pulling parts Fusion's own update leaves on an old 3D model), push the
  board to its 3D PCB and check every part's model is on the right side.
- **Design rules and stackups**: every JLCPCB impedance stackup (16 four-layer,
  14 six-layer, built from JLC's published tables) plus a 2-layer 1.6 mm set,
  each as a .edru (rules and stackup) and a .estackup, to load in Fusion's DRC
  dialog or Layer Stack Manager (`list_design_rules`). Rules meet JLCPCB's
  published minimums, with a 0.3 mm minimum drill (smaller costs more at JLC;
  lower it in the file if you need to).
- **JLCPCB**: BOM and CPL (pick and place) files; placement orientation checked
  against the footprint JLC places each part with (still check JLC's placement
  preview before ordering, see below); a check of your gerber zip
  against the design (every layer, the outline, every drilled hole, paste and
  mask on every SMD pad).

`fusion-electronics-mcp tools` lists the available tools.

## Requirements

- Autodesk Fusion (desktop) with Electronics, on Windows or macOS
- Python 3.12 or newer
- An MCP client, for example Claude Code or Claude Desktop

## Install

```bash
pip install "fusion-electronics-mcp[render] @ git+https://github.com/groundplane-studio/fusion-electronics-mcp"
```

The `[render]` extra adds matplotlib for `render_board`; leave it out if you do
not need board pictures.

### Connect it to Fusion

Install the bundled add-in and run it from **Utilities > Add-Ins** in Fusion
(tick Run on Startup):

```bash
fusion-electronics-mcp install-addin
```

The add-in listens on 127.0.0.1 only, behind a per-session token.

Without the add-in, the server can fall back to **Fusion's own MCP server**
(Preferences > General > API > **Fusion MCP Server**). That works for reading
and most edits, but on current Fusion builds it does not save libraries, it
stops answering after about a minute, and it cancels Fusion's dialogs, so
saving and pushing to the 3D PCB need the add-in.

### Add it to your MCP client

Claude Code:

```bash
claude mcp add fusion-electronics -- fusion-electronics-mcp
```

Claude Desktop (`claude_desktop_config.json`; use the full path to the command
if it is not on your PATH):

```json
{
  "mcpServers": {
    "fusion-electronics": { "command": "fusion-electronics-mcp" }
  }
}
```

### Check the setup

With Fusion running:

```bash
fusion-electronics-mcp doctor
```

## Read-only ULP probe

`run_bundled_ulp(name="probe", design="PCB")` runs the reviewed, bundled probe
in the active board or schematic. `design` is optional and checks the exact
document name; the tool never switches documents. It reports group membership,
check status, variant, grid/layers, and either board pour-contour samples or
schematic `nets()` / `allnets()` data. It does not save, recalculate pours or run DRC/ERC.

Only `probe` is accepted: no caller-supplied paths, source or ULP arguments.
Results use a private temporary file which is removed after the call. Selected-object,
net and pour lists are capped at 100 items and contour samples at 8 wires per pour;
counts are complete and layers are reported in full.
`checked` describes Fusion's existing check status, not a check performed by this tool.
An empty contour result does not prove that a pour has been calculated.

Requires server `0.3.0+workflow.9` and add-in `0.14.12` (reinstall/restart the
add-in), or the updated server's built-in transport. Restart the MCP connection
to discover the new tool. See [live findings and limitations](docs/ulp-probe.md).

### Registered custom and existing Fusion ULPs

Server `0.3.0+workflow.10` / add-in `0.14.13` adds `discover_fusion_ulps`,
`register_ulp`, `list_registered_ulps` and `unregister_ulp`. Existing downloaded
Fusion ULP Manager programs can be registered by their reviewed source hash.

- `run_registered_ulp`: structured read results, with unchanged-design checks.
- `preview_ulp_edit` → `apply_ulp_edit`: review exact commands and expected attribute
  changes, then apply the approved hash once and verify the exported result.
- `run_legacy_ulp`: explicitly requested runs of registered unchanged ULPs, including
  programs that edit directly or show dialogs. Retains before/after snapshots and an
  observed diff; no guaranteed preview, completion or rollback.

Sources must be reviewed and self-contained. Registration is not a security sandbox.
See [registration, output contracts and limitations](docs/registered-ulps.md).

## Libraries

### Find parts in Fusion libraries

`search_fusion_libraries(query="USB C")` searches Fusion's local Library Manager
index across local, public and hub libraries, including inactive libraries.
Search by part number or words such as `ESP32` or `10k 0603`: names, descriptions,
packages, technologies and attributes are searched. Exact device names and
complete-word matches rank first. Each result includes `device` and
`library_name` for `add_part`, the package and attributes, and the library's
path/URN, version, activation and download status. Duplicate library names are
flagged: resolve the intended library in Library Manager before placement.

Use `limit` (1..100, default 25) and the returned `next_offset` to page results;
`library_name` optionally filters by an exact, case-insensitive library name.
The search reads the index without opening documents or changing libraries.
It needs no new environment variables or add-in changes. After updating the
server, restart your MCP connection to expose the new tool.

**Coverage:** this searches locally indexed metadata, not every component in
the cloud. Libraries without indexed devices are counted in `coverage`; matching
library names/descriptions appear separately in `unindexed_libraries`, with
`unindexed_total` and `unindexed_next_offset` for pagination. Refresh/download
libraries in Fusion's Library Manager to populate missing records, and activate
the desired library before placing a part. Cached status does not prove that a
library is available in the active design or current hub.

The index is discovered under Fusion's standard `Electron` directory on Windows
and macOS. If more than one account index exists, pass `database_path` to the
intended account's `libraries.db`; it is never selected silently. This also
supports nonstandard installations. The index schema is internal to Fusion;
unsupported schema changes produce an error rather than an empty search.
Autodesk documents the cache locations in its
[library troubleshooting guidance](https://forums.autodesk.com/t5/fusion-electronics-forum/attention-how-to-resolve-missing-3d-models-or-errors-when/m-p/12808621).

`search_library` remains a separate search of this MCP's JSON component catalog.

### Groundplane's libraries (optional)

[groundplane-studio/fusion-libraries](https://github.com/groundplane-studio/fusion-libraries)
has the libraries we design with: passives with a variant per value and its JLCPCB
part number, schematic frames and power symbols, and our active parts and
connectors. Upload the `.flbr` files to a Fusion project (Data Panel > Upload) and
add them to a design from the Library Manager. Then point the server's schematic
tools at the frames and power symbols:

```
FUSION_MCP_SHEET_FRAME=FRAME_B_L@GPLIB_SCHEMATIC
FUSION_MCP_GROUND_SYMBOL=GND_EARTH@GPLIB_SCHEMATIC
FUSION_MCP_POWER_SYMBOL=12V@GPLIB_SCHEMATIC
```

Any library works: these settings only name the devices to use.

### Your own parts library

For parts no library has, the server keeps part definitions (JSON files) in its
component library folder and builds them into a Fusion library of yours:

1. In Fusion, create an empty electronics library in your project (for example
   "MCP Library") and save it.
2. Get the part's footprint as a KiCad `.kicad_mod`: from KiCad's libraries, or
   JLCPCB's own footprint and 3D model with
   [easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py)
   (`easyeda2kicad --full --lcsc_id=C2040`; a separate program, AGPL-3.0).
   Fetch one part at a time with a pause between parts: EasyEDA refuses bursts
   (HTTP 403), so if it does, wait a while and try again.
3. Ask your agent to create the part (`create_library_part`: footprint, name,
   value, JLCPCB number, pin names). Two-pin passives get the same symbols as
   the rest of your schematic; other parts get a box with named pins.
4. Open your library in Fusion and have the agent run `insert_library_part`,
   `save_design`, then `attach_3d_model` with the part's STEP file. A model
   that would sit upside down is refused.
   To add the footprint to a device set you already have instead (another
   connector under your `CONN_USBC_24P`, say), run `add_library_variant`: it
   draws only the package and adds it as a new variant on the existing
   symbol, with the attributes you give each technology (`dnp_technology`
   adds a `_DNP` one with `NC/` on the value). `get_library_deviceset` shows
   a device set's pins, variants and attributes first.
5. Close the library (`close_library`) and place the part with `add_part`.

Footprints keep plated slots (`(drill oval ...)` becomes a Fusion slot pad)
and unplated slots (milled on layer 46). `>NAME` / `>VALUE` are 0.8128 mm,
vector, ratio 8 %, bottom-left, above the footprint's top-left corner.
Fusion 2706.1.14 crashes in Create 3D Package, so `attach_3d_model` refuses
on that build unless `accept_crash_risk=true`: save the library first.

Part definitions live in `%LOCALAPPDATA%\fusion-electronics-mcp\library\parts`
on Windows (`FUSION_MCP_LIBRARY` to change), so you can keep them in git.

### Datasheet-backed custom parts

`create_library_part` also accepts these optional fields:
- `pin_groups`: group name to an ordered list of symbol pin names. Include every
  pin exactly once. Groups are separated by a blank row on each side.
- `pin_units`: unit/gate name to a list of pin names, again covering every pin
  exactly once. This creates separate symbols in one device and verifies
  gate-qualified pin-to-pad connections when inserting the library part.
- `pin_sides`: pin name to `left` or `right`; unspecified pins keep their default.
- `datasheet`: `source` (HTTP(S) URL or local file), `revision`, `pages` (1-based
  PDF page numbers), and optional `sha256`. Local files are hashed; a supplied
  hash must match. URLs are recorded without downloading anything. Provenance
  is stored in the part JSON and exported as `DATASHEET_*` Fusion attributes.

For example, with pins named IN, OUT, VDD and GND:
```json
{
  "pin_groups": {"Signal": ["IN", "OUT"], "Power": ["VDD", "GND"]},
  "pin_units": {"A": ["IN", "OUT"], "P": ["VDD", "GND"]},
  "pin_sides": {"IN": "left", "OUT": "right"},
  "datasheet": {"source": "C:/project/datasheets/part.pdf", "revision": "Rev B", "pages": [3, 7]}
}
```
Existing part definitions remain compatible. This records evidence and generates
library symbols; it does not mark a pin mapping or footprint as reviewed. KiCad
footprints are still required. Multi-unit automatic block placement is not yet
supported; use Fusion's unit placement controls. Keep pin names unique across
units so existing schematic tools can address them unambiguously.

## Using it

Open a design in Fusion, then ask your agent things like:

- "Review the schematic and list anything that looks wrong."
- "Check the impedance and length matching of the USB and Ethernet pairs."
- "Route USB_DP/USB_DN as a 90 ohm pair from J2 to J4, matched to 0.1 mm."
- "Add a GND pour on both inner layers and stitch it, keeping vias 0.6 mm from the pairs."
- "Export the JLCPCB BOM and CPL, and check the gerber zip in my Downloads."

Gerbers: Fusion's CAM cannot be driven from outside, so export the gerber zip
from Fusion's CAM Processor yourself; `check_gerbers` then checks it against the
design before you upload it.

### Before you order from JLCPCB

`check_jlc_orientation` compares each part's footprint with the one JLCPCB places
it with and writes the rotation and offset corrections into the CPL. Parts it cannot
match with confidence (pads that are named differently, polarity it cannot tell)
are listed under `needs_review` rather than guessed. It is a check, not a
guarantee: footprints JLCPCB has never published, or has changed, are not covered.

So before you pay for assembly, open JLCPCB's component placement preview on the
order page and look at every part: pin 1 and polarity marks (diodes, LEDs,
electrolytics, ICs, connectors) must line up with the board's markings, and
every part must sit on its pads. Rotate any that do not right there, then put the
same correction on the part in your library (`JLC-ROTATION`, `JLC-X-OFFSET`,
`JLC-Y-OFFSET`) so the next order comes out right.

## Safety and privacy

- **No telemetry.** The server talks to Fusion on 127.0.0.1 only.
- **The add-in only does design edits.** It listens on 127.0.0.1 only, answers
  only requests carrying the per-session token it writes to your user folder,
  and runs only Fusion design commands from a fixed list (no `RUN`, `SCRIPT`,
  `SYSTEM`, file or export commands), so an agent cannot use it to run
  programs. The one local file it reads is a STEP model you name for
  `attach_3d_model`, which it imports into your Fusion project.
- **One exception, on request**: `check_jlc_orientation` with `fetch=true`
  downloads footprints from easyeda.com (cached forever, at least 15 s between
  requests). Nothing else leaves your machine. The service tools
  (`request_design_review`, `get_assembly_quote`) are stubs and make no network
  calls.
- **Fusion's questions get safe answers.** Fusion asks things with pop-up
  dialogs; a watchdog answers the ones a tool expects and cancels the rest
  (Cancel / No, never Yes). Windows only so far: on macOS a dialog waits for you.
- **Your keyboard stays yours.** If Fusion grabs focus while a tool runs, focus
  goes back to the window you were typing in (`FUSION_MCP_KEEP_FOCUS=0` turns
  this off).
- When you are in the middle of a command in Fusion, edits wait for you
  (Fusion refuses them) instead of cancelling your command.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FUSION_MCP_TRANSPORT` | `auto` | `addin`, `builtin` (Fusion's MCP server), or `auto` (the add-in when it is running, else the built-in server) |
| `FUSION_MCP_BUILTIN_URL` | `http://127.0.0.1:27182/mcp` | Fusion's MCP server address |
| `FUSION_MCP_LIBRARY` | per-user data folder | component library (part JSON files) |
| `FUSION_MCP_SHEET_FRAME` | none | frame for new schematic sheets, `DEVICE@LIBRARY` |
| `FUSION_MCP_GROUND_SYMBOL` | none | ground symbol for block schematics, `DEVICE@LIBRARY` |
| `FUSION_MCP_POWER_SYMBOL` | none | power symbol for block schematics (its value is set to each rail's name), `DEVICE@LIBRARY` |
| `FUSION_MCP_KEEP_FOCUS` | `1` | hand keyboard focus back when Fusion takes it |
| `FUSION_MCP_LOCK_WAIT_S` | `30` | how long a call waits while another Claude session is using Fusion (only one at a time) |
| `FUSION_MCP_DRC_AFTER_WRITES` | `1` | run DRC before and after every write and report the new errors (`0` to turn off) |
| `FUSION_MCP_UNDO_ON_NEW_DRC` | `0` | `1` also undoes a write that adds copper DRC errors |
| `FUSION_MCP_EASYEDA_CACHE` | per-user data folder | EasyEDA footprint cache |
| `FUSION_MCP_EASYEDA_INTERVAL_S` | `15` | minimum seconds between EasyEDA requests |

The per-user data folder is `%LOCALAPPDATA%\fusion-electronics-mcp` on Windows
and `~/Library/Application Support/fusion-electronics-mcp` on macOS.

## Known limits

- Routing is still in development. Boards and pours can be routed with the
  tools above, one stage at a time, but fully automatic routing of a whole board
  is still being worked on: expect to guide it and review each stage.
- Gerbers are exported from Fusion's CAM dialog by hand (checked by the tool).
- Impedance figures are closed-form estimates, typically within about 10%;
  confirm critical pairs with your fab's calculator.
- The dialog watchdog and focus guard are Windows-only so far.
- Built and tested against Fusion 2705.1.15. Fusion's undocumented command
  interface can change between releases; `docs/architecture.md` lists the
  behaviours the tools depend on.

## Development

```bash
git clone https://github.com/groundplane-studio/fusion-electronics-mcp
cd fusion-electronics-mcp
pip install -e ".[render]"
python -m unittest discover -s tests
```

`docs/architecture.md` explains how the server, Fusion and the add-in fit
together. `tests/live/` holds scripts that drive a running Fusion.

## About

Made by [Ground Plane Studio](https://groundplanestudio.com/), a hardware
development team: industrial design, electronics, firmware and manufacturing,
from concept to production. Want a board designed, reviewed or brought to
production? [Get in touch](https://groundplanestudio.com/contact).

## Disclosure

- **Not affiliated with Autodesk.** Autodesk, Fusion and EAGLE are trademarks of
  Autodesk, Inc. This is an independent project, not made, endorsed or supported
  by Autodesk. The same goes for JLCPCB and EasyEDA.
- **Built with AI.** Most of the code was written with Claude Code, directed,
  reviewed and tested on real boards by the engineers at Ground Plane Studio. The
  tools that edit a design check the result and undo the change when it does not
  match, and there is a large automated test suite, but treat it like any tool
  you did not write yourself.
- **No warranty.** It is provided as is (see `LICENSE`). An agent can get a design
  wrong. Check every board yourself (DRC, ERC, your fab's previews) before you
  order, keep your designs versioned, and remember that what you send to
  fabrication is your call.

## License

MIT. See `LICENSE`.

### Reviewed schematic plans and project checks

The design-independent workflow adds six tools:

- `preview_schematic_edits(operations, expected_document_id)` exports the active schematic and linked board, generates a local HTML/SVG preview and immutable JSON plan, and writes nothing to CAD. Obtain `document_id` from `get_context` (updated add-in required).
- `apply_schematic_plan(plan_path, expected_sha256)` applies only the exact preview/hash explicitly reviewed and authorized by the user. It rejects changed documents, source files, connectivity or placement. Every operation has read-back verification; nothing is saved. A timeout, dialog or mismatch returns `state_unknown` with completed operation indices. Stop and inspect; no automatic retry or blind undo is attempted.
- `check_datasheet_pins(part_ids=None)` compares private pin-evidence JSON, exact local PDF hashes, library definitions, and the active schematic. Missing/unreviewed/changed evidence returns incomplete, never pass.
- `record_pin_evidence_review(evidence_path, expected_content_sha256, status, reviewer)` appends an explicit human approval/rejection and preserves a backup. The hash is `fusion_offline.pin_evidence.content_hash(evidence)`; editing reviewed content invalidates approval.
- `check_project_requirements(rules_path)` checks reviewed counts, channel assignments, connector pad tables, rails, voltage ranges and isolation topology. Physical clearance, creepage, insulation and regulatory acceptance remain unknown; topology does not certify hardware.
- `preview_circuit_block(block_path, instances, port_connections, expected_document_id, rules_path)` expands 1..64 instances through the same plan path. All ports are explicit; private nets are namespaced; references are allocated deterministically. Unknown/failed template or project reviews block apply.

Contracts live in `library/edit-plan.schema.json`, `pin-evidence.schema.json`,
`project-rules.schema.json`, and `circuit-block.schema.json`. Private files belong
beside the configured `parts` directory, not in the public source repository.
There is no OCR or automatic evidence approval. Use exact package/MPN-specific,
reviewed pin rows, including NC/DNC, reserved and exposed-pad rows. Existing `MP`
and `MPN` attributes are both understood.

Typed operations use these exact fields (coordinates are mm):

```json
[
  {"op":"add_sheet","sheet":2,"title":"Analog front end"},
  {"op":"place_unit","part":"U1","gate":"P","sheet":2,"x":20,"y":30,"rotation":0},
  {"op":"connect","net":"VCC","pins":[{"part":"U1","gate":"P","pin":"VDD"}]},
  {"op":"label_net","net":"VCC","sheet":2}
]
```

Use only operations needed by the design: labeling an already labeled net is
rejected. `add_part` requires `ref`, absolute `library_path` and `evidence_path`,
`library`, `deviceset`, `device` (empty string is valid), first `gate`, `sheet`,
`x`, `y`, and `rotation`. Cache the exact installed Fusion library XML first;
preview never stages temporary parts in a production document. The evidence
source is resolved relative to its JSON file and must match its SHA256.
The cached library must include the physical package and MPN attribute.
Fusion embeds only used library items; a full cache is accepted only when all
existing parts retain identical electrical definitions and geometry. Net names
in previews and repeated blocks use Fusion's uppercase spelling.

Multi-unit identity is `(part, gate, pin)`. Pin helpers accept `gate=` and refuse
ambiguous unqualified names; interactive pin specs also accept `PART.GATE.PIN`.
New units may be invoked on any existing sheet; duplicate invocation is refused.
Moving an already placed unit between sheets still requires Fusion's Move UI;
this first typed operation set deliberately has no delete/cut operations.
Explicit block unit positions use all gates; the older automatic block-layout
helper still refuses multi-unit parts rather than silently dropping gates.

A plan's HTML includes the proposed sheets, gate-qualified pad mappings, and a
structured before/after diff. `expected_sha256` is an integrity check, not a
credential: the agent must still obtain the user's explicit apply authorization.
Only the new batch/block tools use this guarded path; legacy tools retain their
existing interfaces. Changes made outside the MCP can invalidate a plan.

Development verification: `PYTHONPATH=server:offline:tests python -m unittest discover -s tests`.
On Windows use semicolons in `PYTHONPATH`. Offline success is distinct from
acceptance on a disposable Fusion design. Never exercise write tests on a user's
production schematic, and never claim bench or regulatory acceptance from these tests.


Library tools accept an optional `library_directory` for per-call project selection.
It does not change another call, another server session, or the configured default.
Keep separate designs in separate external library directories. No project-specific
counts, parts, net names, or electrical rules are built into the server.


### Integration safeguards

- `open_design` and `open_library` accept an optional `data_file_id` for direct cloud lookup. The add-in validates the file's name, kind, project and requested folder, and remembers known IDs to avoid repeated project enumeration.
- Document activation and closing use the linked Electronics Design family, including differently named schematic and PCB tabs. Creating a design checks that both children are actually linked before reporting success. Missing compatible toolbar commands are reported as unsupported. On Fusion 2706.1.14, `new_design` refuses before opening tabs because native creation did not establish verifiable links; create, link and save the family in Fusion first.
- 3D updates require an observed PCB association or an exact saved file reference; unrelated mechanical documents are not used for Pull.
- Autorouting dispatches under one transaction lock. An uncertain Fusion result returns `state_unknown`; inspect Fusion and let the job stop before retrying. Any retained autorouter settings backup is included in the result.
- Differential-pair reports measure copper only; unrouted pairs have no passing skew verdict. Via-bearing pair routes derive omitted diameters from the board's restring rules and annular-ring policy, and reject drills below the board minimum before writing.
- Pin evidence checks include device connections on uninvoked gates and reject extra mappings to reviewed unconnected pads. Connector rules require an existing part and a nonempty pad map.
- Drill comparisons use board coordinates; shifted or mirrored drill output is not silently realigned. Native CAM export remains a manual operation.
