# Registered ULPs

Requires server `0.3.0+workflow.10`, running add-in `0.14.13`, and a fresh MCP
connection. General EAGLE `RUN` and `SCRIPT` commands remain blocked. These tools
execute only registered sources whose SHA-256 still matches the reviewed copy.

## Register existing Fusion programs or your own source

1. Use `discover_fusion_ulps(query, limit, offset)` to find programs already downloaded
   through Fusion's ULP Manager. Results include local source paths and SHA-256.
   Uncached cloud programs must first be downloaded in Fusion; any other local `.ulp`
   source can also be registered by path.
2. Read/review the source and obtain explicit authorization for its intended effects.
   Call `register_ulp(name, source_path, mode, editors, argument_count, reviewed_sha256)`.
   Names are lowercase identifiers; editors are `board`, `schematic`, or (legacy only)
   `library`. Arguments are positional strings, with an exact registered count (0–16).
3. Use `get_context` for the active document's session ID. Every execution requires
   `expected_document_id`; it never activates a different document automatically.
4. `list_registered_ulps` lists validated registrations. `unregister_ulp` revokes a name.

Registration stores a pinned source copy and manifest under
`%APPDATA%/fusion-electronics-mcp/registered-ulps`. Changing the original source does
not silently change what executes: review and register the new hash. Revocation or
changed registration invalidates existing plans. No ULP is registered automatically.

All modes require self-contained UTF-8 source, at most 512 KiB. `#include` dependencies
are rejected because their bytes would not be pinned; prepare a reviewed self-contained
copy. Sources needing adjacent resources may need adaptation. Arguments cannot contain
quotes, backslashes, semicolons or control characters. Prefer forward slashes for paths.

## Read and preview-producing edit modes

These modes must not edit the design directly. They use exactly one literal
`output(MCP_OUTPUT, "wt")` block. The runner substitutes a private output path;
`argv[1]` is your first registered argument. Use `exit(0)`/`exit(1)`, never
`exit(commands)`. No dialogs, includes/macros, network/process calls, config access,
external file reads, group changes or additional output blocks are accepted by the
source checks. These conservative checks are **not a sandbox**: review trusted code.

The output must be one JSON object (at most 2 MiB) with exactly these fields:

```json
{"schema":1,"mode":"read","editor":"board","data":{"elements":15},"complete":true}
```

For `mode="edit"`, replace `data` with `commands` and `changes`:

```json
{
  "schema": 1, "mode": "edit", "editor": "board", "complete": true,
  "commands": "MOVE 'R1' (6 5);",
  "changes": [{"document":"board",
    "xpath":"./drawing/board/elements/element[@name='R1']",
    "attributes":{"x":"6","y":"5"}}]
}
```

Each XPath must identify exactly one existing supported object. Changes are string
attribute values (null removes an attribute). The initial contract supports attributes
of elements, instances, parts, attributes, wires, vias, text, labels, nets and signals;
it does not model insertion, deletion or reordered nodes. Commands are restricted to
MOVE, ROTATE, VALUE, NAME, ATTRIBUTE, CHANGE, MIRROR and SMASH; the runner sets an mm
grid around them. Every affected exported attribute, including linked schematic
changes, must be declared. Selection-dependent commands and implicit side effects
can therefore fail read-back even when Fusion accepts them.

`run_registered_ulp` and `preview_ulp_edit` compare exported board/schematic state
before and after generating output. The preview writes escaped HTML and a JSON plan
under the server's `ulp-plans` data directory. Review exact commands, expected changes
and SHA-256 before authorizing `apply_ulp_edit(plan_path, expected_sha256)`.

Apply rechecks source registration, target identity/version and the whole exported
state. It validates the frozen commands again and dispatches once. The generator is
not rerun. Read-back compares the entire exported design to the expected result,
normalizing XML formatting and equivalent numeric coordinate spellings. It does not
prove electrical correctness or capture state absent from XML exports.

An attempted plan cannot be retried. Timeout, unexpected dialogs, failed export or
unexpected changes return `state_unknown`; inspect Fusion before recovery. No automatic
undo or save occurs. Before/expected XML is in the plan and available read-back XML is
retained in the attempt report.

Packaged examples in the add-in's `ulps` directory:

- `example-counts.ulp`: read mode, board/schematic, zero arguments.
- `example-move.ulp`: edit mode, board, one reference argument such as `R1`; proposes
  moving it 1 mm right. Apply only on a disposable unconnected fixture for testing:
  connected wires or moved labels may introduce additional undeclared changes.

## Existing legacy ULPs

Register unchanged direct/interactive programs with `mode="legacy"`. Only run them
when the user explicitly requests the particular run, using `run_legacy_ulp` with
the exact source SHA-256 and active document ID. Registration is not standing approval
for future runs. Fusion dialogs are left for the user, not automatically answered.

The runner executes a pinned copy in a retained `legacy-ulp-*` artifact directory
under the system temporary Fusion MCP work directory. `argv[0]` and source-relative
paths therefore differ from the original location. The returned artifact directory
retains files generated there. Programs using design paths or absolute paths can
write elsewhere; those effects are not confined or rolled back.

The server saves a before snapshot before dispatch, then attempts an after snapshot
and reports structural XML changes (counts and up to 100 samples) for the board and
linked schematic, or active library. Full snapshots remain in `ulp-runs` reports.
The success label is `completed_unverified`: it means the call returned and snapshots
were obtained, not that the program achieved its intent or all delayed work finished.
An execution/export error, tab switch or saved-version change is `state_unknown`;
the before snapshot is retained even if no after snapshot could be obtained.

There is no guaranteed command preview, completion or rollback. Legacy programs may
save designs, modify other documents/files, use the network, or launch processes.
Those external effects and unsurfaced Fusion state are outside the snapshot diff.
