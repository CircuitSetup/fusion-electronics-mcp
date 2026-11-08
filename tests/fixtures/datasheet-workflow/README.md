# Synthetic regulator layout fixture

`regulator.brd` represents a synthetic eight-pad regulator with input/output capacitors, inductor, feedback divider and exposed-pad ground connections on a four-layer stack. `requirements.json` checks pad-specific supply/feedback proximity, switch-path length, power width/clearance and thermal-via connectivity. Engineering proximity bounds are explicitly assumptions. Implemented checks pass; the separate reference/return-path requirement deliberately remains unverified. This is not a fabrication-ready or electrically simulated converter.

`synthetic-evidence.json` is a test double, not manufacturer evidence or human approval. It cannot be bound as acquired evidence: there are no PDF manifests. Negative tests mutate evidence and geometry; production acquisition is documented in `docs/datasheet-workflow.md`.
