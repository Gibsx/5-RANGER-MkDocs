# 5-RANGER-MkDocs

Pterodactyl egg that polls
[`Gibsx/RANGER-AIDE-MEMOIRE`](https://github.com/Gibsx/RANGER-AIDE-MEMOIRE)
and serves it as an MkDocs Material wiki. Single foreground process,
one port. `entry.py` self-bootstraps deps, supervises a stdlib
`http.server`, and re-renders on each new content commit.

Deploys via [`egg-ranger-aide-memoire.json`](egg-ranger-aide-memoire.json).

## Configuration

Two surfaces, split by sensitivity:

| Surface | Holds | Lives in | Edit via |
|---|---|---|---|
| **Pterodactyl panel** | **Secrets only** — the GitHub `TOKEN` | server env vars | Pterodactyl UI |
| **`config.yaml`** at repo root | **Everything else** — content repo, branch, poll interval, site branding | this Git repo | GitHub (Claude or directly) |

Precedence: real env vars override `config.yaml` override hardcoded
fallbacks in `sync.py` / `entry.py`. So the panel always wins for the
GitHub `TOKEN`, and editing `config.yaml` on GitHub propagates everything
else.

**To apply a GitHub config change:** push the edit, then click
**Reinstall** on the Pterodactyl server. The install script re-clones
the repo, the next start applies the new defaults. Runtime state
(`.pydeps/`, `state.json`, the built `site/`) is preserved on the
`/home/container` volume.

### Panel variable (secret)

| Variable | Purpose |
|---|---|
| `TOKEN` | Fine-grained GitHub PAT with `Contents: Read` on the content repo. Required even for public repos because the anonymous quota is only 60 calls/hr; authenticated is 5000/hr. |

### `config.yaml` (everything else)

| Key | Default | Purpose |
|---|---|---|
| `REPO` | `Gibsx/RANGER-AIDE-MEMOIRE` | Content repo (`owner/name`). |
| `BRANCH` | `main` | Branch to track. |
| `SYNC_INTERVAL` | `60` | Seconds between SHA polls. 60 s = 60 calls/hr. |
| `SITE_NAME` | `Ranger Aide Memoire` | MkDocs site title shown in the header and browser tab. |
| `SITE_DESCRIPTION` | `5th Battalion, Ranger Regiment — doctrine, SOPs, and field craft.` | HTML meta description / social-share preview. |

See `config.yaml` for the canonical list with inline comments.

### Pterodactyl-provided (auto)

These are set by the panel at container start; don't set manually.

| Variable | Purpose |
|---|---|
| `SERVER_IP` | Allocation host IP (operator-visible only; don't bind). |
| `SERVER_PORT` | Port to bind. Use `0.0.0.0:$SERVER_PORT`. |
| `HOME` | `/home/container`. |

## Deployment

1. Import [`egg-ranger-aide-memoire.json`](egg-ranger-aide-memoire.json) into Pterodactyl (Admin → Nests → Import Egg).
2. Create a server, allocate one port.
3. On the Startup tab, set `TOKEN`.
4. Start.

First boot ~60–120 s (apt + pip bootstrap into `.pydeps/` + initial
build). Subsequent boots <10 s — deps cached, build skipped on
unchanged SHA. Console "online" marker: `Sync loop running every Ns`.

## Architecture

```
entry.py          ┐
  bootstraps deps │  single foreground process
  loads config    │  one allocated port
  starts sync     │  SIGTERM clean
sync.py           ┘

sync.run_once():
  fetch latest SHA → if unchanged, no-op
  else: download tarball → render mkdocs.yml from manifest.yaml
       → mkdocs build → http.server serves new files on next request

http.server subprocess:
  bound 0.0.0.0:$SERVER_PORT, cwd=site/
  supervised by entry.py — respawned if it dies
```

See [`_flow-egg-install.md`](_flow-egg-install.md) for the boot sequence
in detail and [`_flow-poll-and-rebuild.md`](_flow-poll-and-rebuild.md)
for one sync tick.

## Development

Local smoke test outside Pterodactyl:

```bash
REPO=Gibsx/RANGER-AIDE-MEMOIRE BRANCH=main GITHUB_TOKEN=$(gh auth token) \
  python3 sync.py
```

This runs one sync pass and exits. See
[`_process-debug-sync.md`](_process-debug-sync.md) for failure modes.
