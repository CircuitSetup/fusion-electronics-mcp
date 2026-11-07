# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use
**Report a vulnerability** on this repository's Security tab (GitHub private
vulnerability reporting). Include what you found, how to reproduce it, and the
versions of fusion-electronics-mcp and Fusion you used.

We aim to reply within a week and to fix confirmed problems in the next release.

## What the server can reach

- The server talks to Fusion through the bundled add-in on 127.0.0.1, behind a
  per-session token stored in your user folder. General EAGLE commands use a fixed
  list (`ALLOWED_VERBS`); arbitrary `RUN`, `SCRIPT`, `WRITE` and shell commands are
  blocked there. Separate operations execute the reviewed bundled probe or an
  explicitly registered, hash-pinned ULP source. Local inputs also include library
  files, reviewed plans and STEP models imported into your Fusion project.
- Registered read/edit ULP source checks are conservative validation, not a security
  sandbox. Register only reviewed, trusted, self-contained code. Edit plans require
  explicit authorization, stale-state checks and exported-state verification.
- Legacy ULPs require an explicit request for each run and an exact source hash.
  They may edit/save designs, access files/network or launch processes with Fusion's
  permissions. Snapshots/diffs do not capture those external effects, guarantee
  completion or provide rollback. Interactive dialogs are left for the user.
- Native script/add-in run and stop tools also require an explicit user request.
  Runs check exact folder, script ID and reviewed folder hash; stop checks identity.
  These programs and their stop handlers run with Fusion's permissions, without
  sandboxing, effect verification or rollback. External dependencies and previously
  loaded modules are not pinned by the folder hash. Dialogs are left to the user.
  The independent reload helper has a fixed MCP target, checks the previous bridge
  instance and a short deadline, and exposes status without authentication tokens.
  Event logs are bounded in-memory metadata, not a complete security audit.
- Outside legacy ULPs and native scripts, `check_jlc_orientation` with `fetch=true` downloads footprints
  from easyeda.com; Fusion itself may communicate with Autodesk services.
- MCP tools act with your permissions in Fusion: an agent connected to this
  server can edit any design you have open. Connect agents you trust, and review
  changes before you save.
