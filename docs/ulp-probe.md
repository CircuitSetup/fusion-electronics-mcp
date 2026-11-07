# Bundled ULP probe

The `run_bundled_ulp` MCP tool executes only the packaged `probe.ulp` through
`Electron.run RUN`. General `RUN` and `SCRIPT` remain blocked by the command
allowlist. Adding another program requires a code change and a read-only review;
the name is not a filename or a way to submit code. The runner supplies its own
temporary output path, limits the result to 2 MiB, checks the completed JSON
envelope and active document/modified flag, and cleans up on failure or success.
There is no design-change transaction and no automatic undo.

The bundled program reads board/schematic data and writes its result. It contains
no editor commands, dialogs, group mutations, network calls or external processes.
This is a restriction on reviewed bundled programs, not a sandbox for arbitrary ULP code.

## Live results — November 7, 2026

Verified with add-in 0.14.12 in Fusion 2706.1.14, using the existing linked `PCB`
and `Schematic` documents. The board had 15 elements and 18 signals; the
schematic had 15 parts and 3 sheets. Repeated probes returned identical data.
The board probe took about 40 ms and schematic probe about 62 ms inside Fusion;
these are individual observations, not an end-to-end performance benchmark.

Before/after EAGLE exports were byte-identical. Active document, all open
documents' modified flags, layer visibility and API pour settings were unchanged.
The live check restored the PCB after checking the schematic. No design was saved.

| Data | Observation and value beyond current reads |
|---|---|
| Schematic `allnets()` | Returned 18 nets versus 16 from `nets()` and the XML sheet-net list. The additional names were `V+` and `V-`, also present among board signals. There were no schematic modules, so this observation alone does not establish hierarchical-module behavior. The current parser reads sheet nets; ULP provides a direct view of the expanded schematic net set. |
| Layer `used` | ULP returned 0 for 24 layers that the native API marked used, including Pads and several empty bottom-side/documentation layers. Preserve this as a discrepancy to investigate; do not automatically replace the API interpretation. Names and visibility agreed for all ULP layers. The API also enumerated 11 schematic/simulation layers absent from the board ULP iteration. |
| Group membership | `ingroup()` is available for elements, schematic instances, wires and vias. The probe returned empty groups in these designs. This is live UI state absent from exported design XML and not exposed by the project's existing readers; nonempty selections remain untested. |
| Computed pour contours | ULP offers `contours()` / `fillings()` separately from the drawn outline. The installed Python `PolyPour` API exposes outline `wires`, but no corresponding contour/filling collections. The tested board had zero pours, so nonempty computed geometry remains unverified. |
| Grid, check status and ordinary counts | Primarily overlap with XML/native API. The probe returned `checked=0`; it did not run a check. ULP `variant()` returned the raw string `''` in this design. |
| Hierarchical parts/nets | The program counts `allparts()` and `allnets()` as well as ordinary parts/nets. The installed Python `Schematic` class has `parts`, `nets`, and `modules`, but no `allParts`/`allNets` collections. Whether traversing modules reproduces every expanded ULP result was not tested. |

The installed API comparison used Fusion's own `Api/Python/packages/adsk/electron.py`.
Claims above concern this build and the project's current readers, not every
possible route through Autodesk's API.

## Result conventions and limits

- `sample_limits` gives the 100-item cap for selected-object, net and pour lists,
  and the 8-wire contour sample cap. Layers are reported in full.
  `counts` and each pour's `contour_wires` / `filling_wires` are totals.
- Contour samples are `[x1_mm, y1_mm, x2_mm, y2_mm, curve_degrees]`.
  They are diagnostic samples, not complete manufacturing or clearance geometry.
- Grid `distance` is in `unitdist`, not necessarily millimetres. Unit codes follow
  Autodesk's `UL_GRID` constants. Layer numbers are Fusion's native numbers
  (Bottom was 304), not the classic XML export's renumbered copper stack.
- No recalculation is performed. Empty/stale fill data and existing check status
  must not be interpreted as new DRC or copper-connectivity evidence.
- Live testing covered Windows/add-in transport. Built-in transport asset lookup
  and dispatch have offline coverage, but its endpoint was unavailable for live testing.

## Reproduce

With the updated add-in running and a board or schematic active:

```text
python tests/live/probe_bundled_ulp.py --output <local-report.json>
```

The script invokes the public tool twice under the shared Fusion lock, compares
exports/context and existing API reads, and writes a local evidence report.
Raw live reports may contain design names/identifiers and are not committed.

Autodesk references:
[RUN](https://help.autodesk.com/cloudhelp/ENU/Fusion-ECAD/files/ECD-CLI-R.htm),
[UL_SCHEMATIC](https://help.autodesk.com/cloudhelp/ENU/Fusion-ECAD/files/ECD-ULP-SCHEMATIC.htm),
[UL_POLYPOUR](https://help.autodesk.com/cloudhelp/ENU/Fusion-ECAD/files/ECD-ULP-POLYPOUR.htm).
The latter's linked signal reference spells the loop `polypours`; this build
requires `polyPours`, confirmed by compilation and Autodesk's installed
`cmd-draw-polygon-contours-as-wire` example.
