# _flow-egg-install.md

From panel "Create Server" to first successful serve.

## Sequence

```
Panel: Admin → Nests → Import Egg
  → upload egg-ranger-aide-memoire.json
  → egg now available in the nest

Panel: create new server with this egg
  → allocate one port
  → set variables: REPO, BRANCH, TOKEN, SYNC_INTERVAL, SITE_NAME, SITE_DESCRIPTION
  → start install

--- install phase (installer container) ---

install.sh (bundled in the egg):
  ├─ apt-get update && apt-get install -y python3 python3-pip
  ├─ git clone https://github.com/Gibsx/5-RANGER-MkDocs /mnt/server
  │    (or download zipball if git unavailable)
  └─ chown -R container:container /mnt/server

--- runtime phase (runtime container, first boot) ---

entry.py starts as the foreground process:

  ├─ _bootstrap_deps()
  │    ├─ if /home/container/.pydeps exists: sys.path.insert(0, ...) and continue
  │    ├─ else:
  │    │    TMPDIR = /home/container/.piptmp
  │    │    pip install --target=/home/container/.pydeps -r requirements.txt
  │    │    on failure: rmtree .pydeps, raise (next boot retries)
  │    └─ sys.path.insert(0, /home/container/.pydeps)
  │
  ├─ log banner: "Allocation: http://$SERVER_IP:$SERVER_PORT/"
  │    (operator-facing only; we bind 0.0.0.0:$SERVER_PORT)
  │
  ├─ sync.run_once()  # first pass, full build from scratch
  │    ├─ GET /repos/<REPO>/branches/<BRANCH>
  │    ├─ download /tarball/<sha>
  │    ├─ extract to /home/container/content/
  │    ├─ inject tag frontmatter per section
  │    ├─ render mkdocs.yml + tags.md
  │    ├─ python -m mkdocs build → /home/container/site/
  │    └─ write state.json { "sha": "..." }
  │
  ├─ subprocess.Popen(["python", "-m", "http.server", "--directory", "site",
  │                    str(port), "--bind", "0.0.0.0"])
  │    supervised: a respawn watcher restarts it if it exits non-zero
  │
  └─ main loop:
       while not stop_event.is_set():
         if stop_event.wait(SYNC_INTERVAL): break
         sync.run_once()  # no-ops on unchanged SHA

--- shutdown ---

SIGTERM → stop_event.set() → loop exits Event.wait()
       → terminate http.server subprocess
       → exit 0
```

## First-boot timing

- Install: ~30–60 s (apt + clone).
- First `_bootstrap_deps`: ~60–120 s (pip install of mkdocs + material + yaml).
- First `sync.run_once`: ~5–15 s (tarball download + build).
- `http.server` binds almost immediately.

Panel shows server as "online" when `entry.py` is running (not when the site is served — that's a few seconds later).

## Subsequent boots

- `.pydeps/` already present → `_bootstrap_deps` is instant.
- `state.json` present → `sync.run_once` sees unchanged SHA → no-op.
- Total boot time: < 10 s.

## Failure recovery

- `_bootstrap_deps` failure → `.pydeps/` wiped → next boot retries.
- `sync.run_once` failure → logged, loop continues; last successful `site/` still serves.
- `http.server` crash → respawn watcher restarts it.

## Related

- `_reference-pterodactyl-constraints.md`
- `_flow-poll-and-rebuild.md`
- `_process-debug-sync.md`
