# 5-RANGER-MkDocs

Pterodactyl egg that mirrors a GitHub markdown content repo into an
[MkDocs Material](https://squidfunk.github.io/mkdocs-material/) site and
serves it on the container's allocated port.

Originally built for [5 RANGER](https://github.com/Gibsx)'s `RANGER-AIDE-MEMOIRE`
— doctrine, SOPs, and field craft for 5th Battalion the Ranger Regiment
(Arma 3 milsim). Split out of the bot repo into its own public repo so
the Pterodactyl egg's install script can reference asset URLs over
`raw.githubusercontent.com` without a PAT, and so anyone with a
`manifest.yaml + NN-<slug>.md` shaped content repo can reuse it without
pulling in unrelated Discord bot code.

## What it does

Runs as a single foreground Python process (`entry.py`) inside a
Pterodactyl container. Every `SYNC_INTERVAL` seconds (default 60s):

1. **Poll** GitHub for the content branch's head SHA.
2. **Skip** if it matches the last-published SHA (`state.json`) — cheap
   no-op path, typical steady-state cost is one `GET /git/ref` per tick.
3. **Otherwise** download the branch tarball via the GitHub API, extract
   to a staging dir.
4. **Inject tag frontmatter** into each section's `.md` from the
   manifest's `tags:` field, so Material's tags plugin renders chips
   and a browsable `/tags/` index without polluting the source repo
   with MkDocs-specific YAML headers.
5. **Regenerate `mkdocs.yml`** from the manifest. Manifest sections
   sharing a `group:` value are bucketed into collapsible sidebar
   sections (first-occurrence order preserved); ungrouped sections
   land flat.
6. **Rsync** the staged content (sections + images) into the MkDocs
   docs dir.
7. **Run `mkdocs build`** to produce a fresh static site in `site/`.
8. **Persist the new SHA** to `state.json`.

A second thread supervises a `python -m http.server` subprocess bound to
`0.0.0.0:${SERVER_PORT}` with cwd set to `site/`. The server reads
files fresh per request, so in-place rebuilds flip content over without
any restart or livereload step.

## Expected content-repo shape

Point this at any GitHub repo with a root-level `manifest.yaml` of this
shape:

```yaml
version: 1
sections:
  - file: 01-communications.md
    title: Communications
    slug: communications
    group: Fieldcraft            # optional — omit for ungrouped
    tags: [Soldier 101]          # optional — drives Material tag chips
  - file: 02-movement.md
    title: Movement
    slug: movement
    group: Fieldcraft
    tags: [Soldier 101]
  # …
```

Images live alongside the markdown files at the repo root, named
`<NN>-<section-slug>-<M>-<short-name>.<ext>` so they sort under their
owning section in `ls`. Reference them from markdown with a plain
relative path: `![](01-communications-1-prc152.png)`.

See the original [RANGER-AIDE-MEMOIRE](https://github.com/Gibsx/RANGER-AIDE-MEMOIRE)
repo for a worked example (private — ask for access if you need to
inspect the live shape).

## Deploy — Pterodactyl egg

1. In the Pterodactyl admin panel: **Nests → Import Egg**, upload
   [`pterodactyl/egg-ranger-aide-memoire.json`](./pterodactyl/egg-ranger-aide-memoire.json).
2. Create a new server using the imported egg. Allocate one port — the
   HTTP server binds `0.0.0.0:${SERVER_PORT}` automatically.
3. Fill in the variables:

   | Variable | Required | Example |
   |---|---|---|
   | `REPO` | yes | `Gibsx/RANGER-AIDE-MEMOIRE` |
   | `BRANCH` | yes | `main` |
   | `TOKEN` | yes | PAT with `Contents: Read` on the content repo |
   | `SYNC_INTERVAL` | no (60) | `60` |
   | `SITE_NAME` | no | `Ranger Aide Memoire` |
   | `SITE_DESCRIPTION` | no | `5th Battalion, Ranger Regiment …` |

4. **Start** the server. On first boot the install script fetches
   `entry.py`, `sync.py`, and `_common.py` from this repo's `main`
   branch, `pip install`s MkDocs Material, and populates
   `/home/container` with the runtime.
5. Browse to `http://<panel-host>:<allocated-port>/`.

### Runtime layout inside the container

```
/home/container/
├─ entry.py          # PID 1 — sync loop + http.server supervisor
├─ sync.py           # publish pipeline (poll → stage → build)
├─ _common.py        # GitHub / manifest plumbing
├─ mkdocs.yml        # regenerated every publish from manifest
├─ docs/             # staging → MkDocs source tree
├─ site/             # mkdocs build output — this is what's served
└─ state.json        # {"last_sha": "…"} — persists across restarts
```

## Operations

| Task | How |
|---|---|
| Watch sync ticks | Pterodactyl console — every tick logs `No change` or `Published …` |
| Force a republish | Stop → delete `state.json` from the file manager → Start |
| Rotate PAT | Edit the `TOKEN` variable in the panel → **Restart** (env vars are read at process start) |
| Tweak theme / extensions | Fork this repo, edit `_build_mkdocs_config` in `sync.py`, point the egg's install script at your fork |
| Pin a specific puller version | Replace `main` in the install script's `BASE=` URL with a tag (e.g. `v1.0.0`) |

## Troubleshooting

- **`Failed to fetch branch SHA: 401`** — the PAT expired or lost its
  scope. Re-issue with `Contents: Read` on the content repo and update
  the `TOKEN` variable.
- **`manifest.yaml missing from fetched repo`** — the content repo
  layout changed. Restore `manifest.yaml` at the repo root.
- **Section 404 after rename** — slugs drive the URL. Renames must be
  reflected in `manifest.yaml`'s `slug:` field (and the filename
  prefix, if the slug is part of it).
- **Container starts but site is blank / "Initial sync failed"** —
  check the console for the traceback. `entry.py` writes a placeholder
  `index.html` on first-boot failure so the server is reachable even
  when the sync can't complete; the reason is shown on the page.
- **Port already bound** — something else on the allocation is using
  the port. Stop that server or reallocate.

## Design notes

- **Why a puller, not a pusher.** Keeping the wiki host pulling directly
  from GitHub decouples it from the bot that reads the same repo. The
  wiki stays up when the bot is down, and can live on a host the bot
  can't reach.
- **Why `mkdocs build` + `http.server` instead of `mkdocs serve`.**
  `mkdocs serve` exists for authors iterating locally; it adds
  livereload (websockets, file watchers) which is wasted on a read-only
  doctrine mirror. Build-once-then-serve-static is simpler, uses less
  memory, and means the sync loop doesn't need to signal the server on
  content changes — `http.server` reads files fresh per request.
- **Why regenerate `mkdocs.yml` every tick.** The manifest is the
  authoritative section order. A hand-maintained `mkdocs.yml` would
  drift. Operator tuning of theme/extensions lives in `sync.py`'s
  `_build_mkdocs_config` instead — one place to edit, survives every
  regen.
- **Why tag frontmatter is injected, not committed to the content repo.**
  The content repo is shared across multiple downstream views (Discord
  bot, forum publisher, any future mirror). Keeping it free of
  MkDocs-specific YAML frontmatter means those other consumers don't
  have to filter it out. Injection happens at stage time so the source
  stays clean.
- **Why this repo is public.** The Pterodactyl egg's install script
  bootstraps `entry.py` / `sync.py` / `_common.py` from
  `raw.githubusercontent.com`. A private puller repo would require
  baking a second PAT into the egg; keeping the puller public means the
  only secret the container holds is the content-repo PAT.

## License

MIT. Fork, adapt, reuse. If you ship a fork with meaningful changes, a
link back is appreciated but not required.
