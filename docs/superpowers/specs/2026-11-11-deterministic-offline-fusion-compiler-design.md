# Deterministic offline Fusion Electronics compiler

## Baseline

The clean feature branch started at `a6dae91847b7bb9102100d9aa15eca4d8e5da6e2`,
which matched the refreshed `origin/main`. The CLI reported `0.3.0+workflow.34`
and listed its tools. The existing paired-import path requires add-in `0.14.23+`.

The full baseline suite ran 863 tests. 862 passed and
`test_route_pair_writes_the_planned_diameter` errored because the isolated
test environment had no live Fusion `bridge.json`. The full suite was run with
permission to write test fixtures in the isolated worktree.

## Workflow

The compiler consumes a versioned JSON design, a hash-pinned empty EAGLE
schematic and board, and hash-pinned local EAGLE libraries. It resolves the
parts, emits a flat pair offline, validates electrical and physical contact
parity, and publishes an immutable candidate. Selected routing can be planned
under explicit limits and promoted from an immutable job. The CLI and MCP
operate on the same offline services. Fusion participates in the final
guarded import, save, export readback, and native verification.

The empty template pair supplies the EAGLE header, drawing settings, grid,
layer declarations, board design rules, preserved classes, and document
dialect. Validate a compatible copper stack, flat schematic, and absence of
existing parts, elements, signals, and routes. Preserve the template outline
when the specification supplies an empty outline. Hierarchical modules are
outside version 1.

## Design specification

`server/fusion_mcp/data/offline-design.schema.json` documents the version-1 Draft 2020-12 JSON Schema
shape and `tests/fixtures/compiler/minimal/design.json` is a complete example.
The runtime checks the shape, numerical domains, cross-references, and pinned
sources. Every object with named fields rejects unknown keys. The top-level
keys are `version`, `name`, `units`, `templates`, `libraries`, `parts`,
`schematic`, `board`, `nets`, and `routing`. The version is integer 1 and
the units are `mm`.

- Templates are `schematic` and `board` records with `path` and `sha256`.
  Each library adds a unique `name`. Source paths are absolute local paths,
  and each pin has 64 hexadecimal SHA-256 characters. Read exact bytes and
  reject hash mismatches, symlinks, oversized files, and XML entity
  declarations. Library acquisition and device or package substitution are
  outside the compiler.
- Identifiers use ASCII letters, digits, or characters in `_.$@+!~-`.
  References, gates, pins, net names, libraries, devicesets, and class names
  are nonempty. An explicit empty `device` or `technology` selects the exact
  unnamed EAGLE entry. Preserve spelling in the spec and resolve its references
  by exact name. Reject case-insensitive duplicates where EAGLE identity would
  be ambiguous. Preserve values, descriptions, and attribute values as text.
- Each part declares its reference, exact library/deviceset/device/technology,
  value, attributes, gate placements, and a board placement or `null`. A
  physical placement may instead be declared once in `board.placements` with
  the part's `ref`. A part with no placement is schematic-only.
- Each sheet declares a positive integer `number` and optional
  `description`. Each gate placement names a declared sheet and includes
  `x`, `y`, and a rotation of 0, 90, 180, or 270 degrees. Each board placement
  includes `x`, `y`, `rotation` in [0, 360), `side` (`top` or `bottom`), and
  the `fixed` boolean. Optional `allow_outside` permits placement outside the
  board outline and defaults to false. Reject undeclared or duplicate references and duplicate
  placements of a part's gate. Preserve multiple sheets and multi-unit devices.
- Each net declares `name`, `class`, gate-qualified `pins` (`part`, `gate`,
  `pin`), `schematic_style`, and `route`. A pin appears in at most one net.
  `labels` requires an explicit `port` of `inter-block` or `inter-sheet`.
  Inter-sheet ports span multiple declared sheets. Use exact pinned symbols
  for ordinary named nets. Routed nets need at least two distinct resolved
  physical contacts. The shape check rejects routed nets without any placed
  pin. The library resolution step checks distinct pads because one pin can
  connect to multiple pads.
- Board `outline`, keepout `vertices`, and pour `vertices` are ordered
  `[x,y]` points. Close polygons between the last and first point. Holes
  declare `x`, `y`, and `drill`. Keepouts name copper `layers`. Pours
  declare `net`, `layer`, `vertices`, and `width`, with optional
  `isolate`, `rank`, `thermals`, and `orphans`. Net classes declare
  `number`, `name`, `width`, `drill`, and optional class-keyed
  `clearances`. Keep template classes that the spec leaves in place. Optional
  `board.copper_layers` declares at least two copper export layer IDs in
  stack order. When omitted, derive the expected stack from the compatible
  templates. IDs use the board XML export numbering in the range 1–304.
- Optional net `wires` declare `x1`, `y1`, `x2`, `y2`, `layer`, and
  `width`. Optional net `vias` declare `x`, `y`, `drill`, and through-board
  `extent` (such as `1-16`), with optional `diameter`. The span must match
  the outer layers of the exported stack. Reject invalid layers,
  protected copper changes, off-centre endpoints, and policy violations.
  Preserve slot declarations from pinned geometry and report unsupported
  checks as unverified.
- All numbers are finite. Booleans are accepted only for boolean fields.
  Normalize emitted millimetres to 0.0001 mm and reject placements outside
  the explicit board bounds. Canonicalize ordering of independent records.
  Retain the order of geometry such as outline vertices and route points.

## Routing

`routing` supplies `selected_nets`, `protected_nets`, `policies`, and
`budget`, and may set `require_fully_routed`. Net policies can specify
`allowed_layers` and `max_vias_per_connection`. A per-net limit can restrict
the effective job limit. Require explicit values for budgets above the default
and validate them against inclusive ceilings.

| Field | Default | Maximum |
| --- | ---: | ---: |
| `max_nodes_per_attempt` | 200,000 | 1,000,000 |
| `max_nodes_per_connection` | 300,000 | 2,000,000 |
| `max_attempts_per_connection` | 2 | 8 |
| `max_seconds_per_attempt` | 15 | 60 |
| `max_seconds_per_job` | 600 | 1,800 |
| `max_vias_per_connection` | 2 | 8 |
| `ripup_rounds` | 2 | 8 |

All supplied values are positive. The two duration values may be fractional.
The router enforces node, wall time, attempt, via, rip-up, and layer limits.
A failed connection records its termination and candidate blockers. Independent
connections can continue up to the job deadline. Protected or existing copper
stays outside rip-up unless explicitly eligible. An exhausted budget ends the
attempt without increasing it.

Each deterministic `routing-jobs/<job-id>/` includes `request.json`,
`input-board.brd`, `candidate-board.brd`, `routes.json`,
`diagnostics.json`, `board.svg`, and `job-manifest.json`. The job ID derives
from the input board SHA-256, canonical policy hash, ordered connection IDs,
and router contract version. Apply or promotion verifies the hashes and
copies the persisted board. Render, validation, promotion, and import consume
persisted artifacts without rerunning routing.

## Candidate, validation, and import

The same specification and exact source bytes produce the same schematic,
board, manifest, reports, SVGs, and HTML. Canonical artifacts exclude
timestamps, absolute output paths, random IDs, and platform-specific path
separators. Track the specification, compiler, router, and manifest versions
explicitly. Publish through temporary sibling files or directories and atomic
renames, with fsync where practical. Reject symlinks and unrelated nonempty
output directories.

`candidate-manifest.json` includes `manifest_version`, `candidate_id`,
`compiler_version`, `status`, `inputs`, `artifacts`, `routing_job`, and
`findings`. Inputs bind the spec, template and named library hashes.
Artifacts bind normalized relative paths and SHA-256 values for the
schematic, board, validation report, and preview when available. Candidate
identity derives from canonical input hashes, compiler contract version, and
routing candidate hash when present. The canonical manifest has no
self-referential hash. Changed inputs or outputs require a new identity.

Validation distinguishes `pass`, `fail`, and `unverified`. It checks the
existing importer signature, exact schematic pin-to-pad-to-board-signal
membership, unique identities, outline and placement geometry, route
connectivity, layer and via policies, and keepout collisions. When required,
zero offline airwires is an import gate. A required failure or unverified
finding blocks import. Native ERC, DRC, polygon refill, 3D, and manufacturing
review remain separate acceptance gates.

Import pins the manifest and pair and binds a fresh 32-character lowercase
hex request ID to one candidate, add-in session, destination project and
folder, and new product name. The resumable operation composes the existing
guarded importer and advances at most one irreversible native step per call.

```text
not_started
→ awaiting_schematic_picker
→ schematic_submitted
→ awaiting_board_picker
→ board_submitted
→ verifying
→ saved_linked
```

A normal wait returns `pending`. The same arguments resume the same ticket.
`state_unknown` requires inspection of the existing ticket before any further
action. It stops automatic retries. Preserve unrelated open documents and
return the verified linked identities. Non-Windows sessions retain the existing
manual picker path.

## Minimal fixture provenance

The empty pair retains the EAGLE version, board outline, classes, and two-layer
design-rule structure from the repository's `tests/fixtures/mini.sch` and
`mini.brd`, with compatible synthetic settings, grid, layer declarations, and
empty document sections. The existing offline paired-import reader verifies
the flat empty signature and exact hashes. Native Fusion import acceptance
requires the later disposable-project test.

The synthetic library contains a three-pin header, resistor, LED, and NPN
transistor with explicit gate/pin/pad mappings. The expected summary lists
one sheet, four parts and elements, and ten contacts on five nets. The checked-in
design uses the absolute prefix `/compiler-fixtures/minimal/`. The test helper
replaces only that prefix with the actual fixture directory before loading
the design. The pinned asset hashes refer to the committed LF XML bytes.

Version 1 leaves native construction, hierarchy, curved routing, blind or
buried vias, automatic placement, GPIO reassignment, 3D models, CAM export,
manufacturing approval, and production design replacement outside the
workflow.
