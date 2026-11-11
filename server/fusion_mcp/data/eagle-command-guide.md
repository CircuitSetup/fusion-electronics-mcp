# Fusion Electronics and EAGLE command guide

Use this map to plan a build or write an Electronics `.scr`. Check exact syntax
against the [Autodesk Electronics command-line reference](https://help.autodesk.com/cloudhelp/ENU/Fusion-ECAD/files/ECD-CMD-LINE-CMDS.htm).

## Choosing an execution path

1. Call `get_context` and confirm the active design and editor. Read the parts,
   pin endpoints, nets, layers, and existing geometry relevant to the edit.
2. Use a typed MCP tool when it covers the job. Batch repeated placements or
   routing and use a dry run where offered. For reviewed schematic plans, use
   the returned plan and hash with `apply_schematic_plan` after approval.
3. For deterministic edits outside that coverage, generate a short `.scr` with
   explicit names, units and coordinates. Run a small first batch on a disposable
   copy using the Electronics editor's [SCRIPT command](https://help.autodesk.com/cloudhelp/ENU/Fusion-ECAD/files/ECD-CLI-S.htm#script).
   Read back the result before applying the script to the intended design.
4. Use registered ULPs for live design traversal or reviewed changes to existing
   object attributes. Start with `run_bundled_ulp(name="probe")` or
   `run_registered_ulp` for reads. Use `preview_ulp_edit` and `apply_ulp_edit`
   for eligible writes. That plan format does not model insertions or deletions.
   Legacy ULPs and native Fusion API programs require an explicit request to run.
5. Use Fusion's UI when the operation requires a dialog or has no supported
   command path, such as loading a layer stack preset or design rules. The
   `inspect_fusion_commands` catalog lists native UI command IDs. MCP exposes
   no general EAGLE command or `.scr` runner.

## Schematic

| Task | MCP path | EAGLE script commands and checks |
| --- | --- | --- |
| Select or add a sheet | `preview_schematic_edits` or `new_sheet` | `EDIT .sN` selects a sheet. Selecting the next sheet number can create it. Resolve the sheet number before sending the command. |
| Place devices and gates | `add_part`, or `preview_schematic_edits` with `add_part` and `place_unit` | Use `ADD` with the exact device, library, reference and orientation. Use `INVOKE` for an additional gate of an existing device. Check the library variant and all required gates, including power gates. |
| Build or repeat a circuit | `preview_circuit_block` and `apply_schematic_plan` | Use `ADD`/`INVOKE` and `NET` with a stable reference and net name per instance. Check shared ports and private nets separately. |
| Connect pins and branches | `connect_pins` or reviewed `connect` operations | Use `NET` between actual pin tips. Draw branches as separate segments and use `JUNCTION` at intended branch points. `WIRE` draws artwork in this editor. Check actual pin membership and visible continuity. |
| Label across blocks or sheets | Reviewed `label_net` operations or the explicit label style of `connect_pins` | `LABEL` picks a net segment and places its label. Use continuous wiring within a functional block. Check that every label is attached to the intended segment. |
| Rename a net | `rename_net` | Use `NAME` at a point on the net. Fusion may ask about rename scope or merging with another net. Check pin membership on both nets after any intended merge. |
| Set component value or package variant | `set_part_value`, `set_part_variant` | `VALUE` needs a device that accepts user-defined values. Schematic `PACKAGE` selects a device variant. Recheck both linked editors if the package changes. |

Use `get_net` and `review_schematic` or `run_erc` after wiring. Inspect the
drawing for real junctions, connected pin tips and readable routing.

## PCB

| Task | MCP path | EAGLE script commands and checks |
| --- | --- | --- |
| Place, rotate or flip parts | `move_parts` with dry run, then apply the batch | `MOVE` and `ROTATE` work with quoted references. `ROTATE =R90 'R1'` sets absolute rotation, and `=MR90` specifies mirrored orientation. Verify side, rotation, coordinates and nearby parts. |
| Route pads, a net, or remaining airwires | `route_trace`, `route_net`, `route_remaining`; `route_pair` for differential pairs | `ROUTE` follows an existing airwire. For explicit scripted geometry, use `LAYER` and a named-signal `WIRE` with an explicit width and vertices. Use the actual pad centers as endpoints. |
| Add a via or a stitching pattern | `add_via` or `stitch_vias` | Set the drill with `CHANGE DRILL` and place a named-signal `VIA`. Check drill, diameter, signal and layer span. Resolve layer names and legacy via spans from the actual stack. |
| Create or adjust a copper pour | `add_pour`, `set_pour_thermals`, `list_pours` | Select the copper layer, draw a named `POLYGON` and use `RATSNEST` to recalculate. Verify actual fill, clearance, thermal reliefs and continuity. Polygon vertices alone describe the outline. |
| Remove selected routing | `delete_copper` for precise segments or vias, `rip_up` for named nets | `DELETE` picks an object. `RIPUP` converts routing back to airwires and can affect a whole signal or more. Keep the scope explicit and check unrelated copper plus the new airwire count. |
| Draw the outline, legend or mounting holes | `set_board_outline` for rectangles, `add_text` and `add_hole` | `LAYER 20` (Dimension) and `WIRE` draw custom outlines. Use `TEXT` on the actual silkscreen layer and `HOLE` for an unplated drill. Verify a closed contour, text clearance and drill sizes. |
| Set stackup and net rules | `prepare_layer_stack` and `check_layer_stack`; `set_net_class`, `list_net_classes`, `assign_net_class` | Load the prepared preset or rules in Fusion and verify the result. `CHANGE CLASS` picks a wire on the schematic to assign an existing class. Avoid using raw `CLASS` to define rules because it has produced overly broad copper rules in Fusion. |
| Inspect and check the result | `get_board_summary`, `get_net`, `get_layer_stack`, `run_drc` | `SHOW` highlights objects or signals. `RATSNEST` recalculates airwires and pours. Run DRC, verify the intended copper, and check airwires and any applicable length or impedance constraints. |

## Library

Search existing parts with `search_library` and `search_fusion_libraries` before
creating assets. Use the exact datasheet pinout and package drawing to verify a
match. `create_library_part` prepares a local definition. `insert_library_part`
and `add_library_variant` generate and run EAGLE scripts in Fusion.

| Editor and task | EAGLE script commands and checks |
| --- | --- |
| Package geometry | `EDIT 'name.pac'` selects the package. `SMD` places surface-mount pads, `PAD` places plated through-hole pads, and `HOLE` places unplated holes. Use `LAYER` and artwork commands such as `WIRE` on the intended layers. Check pad identifiers, pitch, dimensions, drill or slots, and pin 1 orientation. |
| Schematic symbol | `EDIT 'name.sym'` selects the symbol. Use `PIN` for electrical endpoints and `WIRE` or other artwork for the body. Use `TEXT` for `>NAME` and `>VALUE` on the appropriate layers. Check pin type, position and orientation. |
| Device and variants | `EDIT 'name.dev'` selects the device. Use `ADD` to add symbol gates, `PACKAGE` to select the footprint variant, and `CONNECT` to assign each gate pin to its pads. Put every pad for a pin in one statement, for example `CONNECT 'G$1.A' '1 3'`. A later `CONNECT` for that pin replaces the earlier mapping. Use `get_library_deviceset` to check the mappings. |
| Device metadata and 3D | `PREFIX`, `VALUE`, `TECHNOLOGY` and `ATTRIBUTE` set metadata. Use `attach_3d_model` where supported and verify model alignment separately. |

## Script construction and verification

- Make the script specific to an editor, design and intended change. For a
  schematic, include the target sheet. Capture the starting state for comparison.
- Use explicit units, width, drill, layer and bend behavior as applicable.
  `GRID MM 0.0001` sets a fine metric grid and `GRID LAST` restores the previous
  grid. For traces, prefer explicit 0°, 45° or 90° segments and read back the
  vertices. The current bend setting can otherwise add a jog.
- Quote names such as `'R1'` so they are parsed as references. Validate data used
  to construct commands. For coordinate picks such as `NAME`, `CHANGE` and
  `DELETE`, check the exact object, layer and visibility before writing.
- Use the configured copper layer's command name or verified legacy number.
  Fusion API layer IDs such as Bottom `304` are distinct from CLI selectors.
- Keep one logical operation per batch. Leave saves and unrelated settings out
  of the script. Treat a timeout, unexpected prompt, or ambiguous result as an
  unknown outcome. Read the live design before retrying.
- Compare the result against what the batch was meant to change. A command can
  return without an error and still do nothing. Run ERC or DRC where applicable
  and check the specific connectivity, geometry or pin mapping separately.

## Script examples

**Schematic wire.** On an existing sheet 2, assuming the two coordinates are
the actual pin tips and `SENSE` is the intended net:

```text
EDIT .s2;
GRID MM 0.0001;
NET 'SENSE' (12.7 20.32) (22.86 20.32);
GRID LAST;
```

Check that `SENSE` contains both pins and the drawing shows a continuous wire.

**Stepped PCB outline.** On a disposable board with no existing outline:

```text
GRID MM 0.0001;
LAYER 20;
WIRE 0 (0 0) (30 0) (30 12) (20 12) (20 20) (0 20) (0 0);
GRID LAST;
```

Check that Dimension contains one closed contour with the specified vertices.

**Device pin mapping.** In a disposable library, assume the device and its
default package variant already exist. Gate `G$1` has pin `A`, and package
pads `1` and `3` both belong to it:

```text
EDIT 'EXAMPLE.dev';
CONNECT 'G$1.A' '1 3';
```

Inspect the selected variant first if the device has several. Read the device
back and verify both pads map to `G$1.A` on the intended variant.
