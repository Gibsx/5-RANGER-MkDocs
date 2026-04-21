# CLAUDE.md — 5-RANGER-MkDocs

Pterodactyl egg that polls `RANGER-AIDE-MEMOIRE` and serves it as MkDocs Material. Cross-repo conventions live in the workspace-root `CLAUDE.md` — read that first.

## Rules

### Shape
- Three-file Python project at repo root: `entry.py`, `sync.py`, `_common.py`. No package directory.
- `entry.py` = bootstrap + sync loop + supervised `http.server`. Single foreground process.
- `sync.py` = publish pipeline (poll, download, render, build). `run_once()` is idempotent.
- `_common.py` = leaf. Does not import from `entry` or `sync`.

### Pterodactyl
- Python 3.8 stdlib + syntax. `from __future__ import annotations`.
- Pure-Python wheels only — no compile toolchain in the installer container.
- Only `/home/container` persists. `pip install --target=.pydeps` and bootstrap `sys.path` in `entry.py`.
- `/tmp` is ~64 MB — set `TMPDIR=/home/container/.piptmp` before pip.
- Bind `0.0.0.0:$SERVER_PORT`. `SERVER_IP` is unbindable inside the container.
- Single foreground process; SIGTERM → clean shutdown.

### Git
- No `git` binary in the image. Fetches are GitHub API tarballs via `requests`.
- `egg-ranger-aide-memoire.json` is **gitignored** — eggs are environment-specific.

### MkDocs
- `python -m mkdocs build` (pip --target has no CLI shims).
- **No `--strict`**. Material deprecations blank the wiki.
- Regenerate `mkdocs.yml` from `manifest.yaml` each run. Never hand-edit the generated file.
- Group sections by `group:` into first-occurrence-order collapsible buckets.
- `use_directory_urls: true` → nav points at `.md`, not `<stem>/`.

### Testing
- Smoke-test sync/build logic in the sandbox:
  ```bash
  REPO=Gibsx/RANGER-AIDE-MEMOIRE BRANCH=main GITHUB_TOKEN=$(gh auth token) python3 sync.py
  ```
- The egg itself is tested on a live Pterodactyl panel, not here.

## Index

### Reference
- [`_reference-pterodactyl-constraints.md`](_reference-pterodactyl-constraints.md) — Python/disk/network/process constraints
- [`_reference-egg-variables.md`](_reference-egg-variables.md) — every panel variable + host-local paths
- [`_reference-mkdocs-yml-generation.md`](_reference-mkdocs-yml-generation.md) — how the config is rendered
- [`_reference-theme-features.md`](_reference-theme-features.md) — enabled Material features + rationale

### Process
- [`_process-change-theme.md`](_process-change-theme.md) — tweak palette / features / plugins safely
- [`_process-debug-sync.md`](_process-debug-sync.md) — diagnose a broken sync or build

### Flow
- [`_flow-egg-install.md`](_flow-egg-install.md) — first boot to first serve
- [`_flow-poll-and-rebuild.md`](_flow-poll-and-rebuild.md) — one sync tick
