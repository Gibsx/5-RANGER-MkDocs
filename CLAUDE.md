# CLAUDE.md — 5-RANGER-MkDocs

Conventions for the Pterodactyl egg that polls the `RANGER-AIDE-MEMOIRE`
content repo and serves it as an MkDocs Material wiki.

Cross-repo conventions (Pterodactyl target, code commenting, GitHub push
pattern, holistic ARCHITECTURE/DOCUMENTATION) live in the workspace-root
`CLAUDE.md` one directory up — read that first.

---

## Shape

Three-file Python project, all at repo root. No package directory.

| File | Role |
|---|---|
| `entry.py` | Container entry point. Bootstraps `.pydeps/`, runs the sync loop + `python -m http.server` supervisor. Single foreground process. |
| `sync.py` | Publish pipeline: poll `main` SHA, tarball-download changed revisions, regenerate `mkdocs.yml` from `manifest.yaml`, inject tag frontmatter, run `python -m mkdocs build`. |
| `_common.py` | Small shared helpers (logging, manifest parsing) used by both entry and sync. Leaf — does not import from either. |

The egg JSON is tracked locally at `egg-ranger-aide-memoire.json` but
**gitignored**. Eggs are environment-specific (docker tag, UID) and should
not be version-controlled; regenerate from a known-good panel export.

---

## Pterodactyl container constraints

These constraints shape every change — violate them and the container
won't start on Pterodactyl:

- **Python 3.8 stdlib + syntax.** No walrus-heavy code, no `match`, no
  stdlib APIs newer than 3.8. Use `from __future__ import annotations`.
- **No GPU, no heavy wheels.** The installer container has no compile
  toolchain; stick to pure-Python wheels (`pyyaml`, `requests`, `mkdocs`,
  `mkdocs-material`).
- **Only `/mnt/server` (mapped to `/home/container` at runtime) persists.**
  System site-packages in the installer image **do not** carry over to
  the runtime image. That's why we `pip install --target=/home/container/.pydeps`
  and bootstrap `.pydeps` onto `sys.path` in `entry.py`.
- **`/tmp` is a tiny tmpfs (~64 MB).** pip wheel builds overflow it, so
  `_bootstrap_deps()` sets `TMPDIR=/home/container/.piptmp` before calling
  pip. If a bootstrap fails, `.pydeps` is `rmtree`'d so the next boot
  retries cleanly.
- **One allocated port, bind `0.0.0.0`.** `SERVER_IP` is a host-side
  allocation address, **not** bindable inside the container's network
  namespace — binding it gives `EADDRNOTAVAIL`. Log the allocation
  (`Allocation: http://$SERVER_IP:$SERVER_PORT/`) for operator clarity
  but bind `0.0.0.0`.
- **Single foreground process.** No systemd. `entry.py` owns the process
  tree: sync loop on the main thread, `http.server` as a supervised
  subprocess with a respawn watcher. SIGTERM → break the loop's
  `Event.wait()`, terminate the subprocess, exit cleanly.

---

## MkDocs build

- Invoke as `python -m mkdocs build` (not `mkdocs`). `pip --target` does
  not create CLI shims, so the console script isn't on `PATH`.
- **No `--strict`.** MkDocs Material keeps deprecating config keys (e.g.
  `tags_file`); a deprecation warning with `--strict` aborts the build
  and blanks the wiki. Warnings in the log are preferable to a dead site.
- Regenerate `mkdocs.yml` each run from `manifest.yaml`. The manifest is
  the single source of truth — never hand-edit the generated `mkdocs.yml`.
- Group sections by `group:` field into collapsible sidebar buckets, in
  first-occurrence order. Sections without a `group:` sit at the top level.
- Inject `tags:` frontmatter per page so the Material `tags` plugin
  renders tag chips and a `/tags/` index.

Relative-link resolver note: MkDocs `use_directory_urls: true` produces
pretty URLs (`communications/`) but the internal link validator still
expects **source** paths. When generating cross-section links from the
manifest, point at `entry['file']` (the `.md`), not `<stem>/`.

---

## Sync loop

`sync.run_once()` is idempotent — it no-ops if the remote SHA matches
the stored one in `state.json`. The outer loop in `entry.py` calls it
every `SYNC_INTERVAL` seconds (default 60). Host-local `state.json`
stores just the last-published SHA — delete it (or `rm` + restart
container) to force a full republish from scratch.

The fetch is a GitHub API **tarball** download (`/tarball/{ref}`), not a
`git clone` — the Pterodactyl image has no `git` binary. `requests` +
`tarfile` stdlib are enough.

---

## Testing

The Claude Code sandbox is the primary testbed, but **only for sync/build
logic** — don't try to stand up the full container here. To smoke-test:

```bash
cd /home/claude/5rangerbot/5-RANGER-MkDocs
REPO=Gibsx/RANGER-AIDE-MEMOIRE BRANCH=main GITHUB_TOKEN=$(gh auth token) \
  python3 sync.py  # one-shot publish
```

Note that `entry.py`'s self-bootstrap writes to `.pydeps/` — gitignored,
but clean it up if you want a fresh test. The egg JSON / Pterodactyl
deploy itself gets tested on the live panel, not here.
