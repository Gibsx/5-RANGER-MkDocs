# _reference-egg-variables.md

Egg variables exposed in the Pterodactyl panel (**Startup** tab). Map to env vars read by `entry.py` / `sync.py`.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `REPO` | ✅ | — | `owner/name` of the content repo (e.g. `Gibsx/RANGER-AIDE-MEMOIRE`) |
| `BRANCH` | | `main` | Content branch to track |
| `TOKEN` | ✅ | — | Fine-grained GitHub PAT with **Contents: Read** on the content repo |
| `SYNC_INTERVAL` | | `60` | Seconds between SHA polls. Authenticated quota is 5000/hr — 60 s = 60/hr per puller, well under |
| `SITE_NAME` | | `Ranger Aide Memoire` | MkDocs `site_name` — shown in the header |
| `SITE_DESCRIPTION` | | `5th Battalion, Ranger Regiment — doctrine, SOPs, and field craft.` | MkDocs `site_description` — meta |

## Pterodactyl-provided (auto)

Set by the panel at container start; don't set manually.

- `SERVER_IP` — allocation host IP (operator-visible only; don't bind).
- `SERVER_PORT` — the allocated port. Bind `0.0.0.0:$SERVER_PORT`.
- `HOME` — `/home/container`.

## Host-local paths

Not egg variables — derived inside the container.

| Path | Contents |
|---|---|
| `/home/container/.pydeps/` | Pip target dir. Survives container restarts, recreated on dep bump. |
| `/home/container/.piptmp/` | `TMPDIR` for pip (avoids `/tmp` overflow). |
| `/home/container/content/` | Extracted tarball of the content repo. |
| `/home/container/site/` | MkDocs build output served by `http.server`. |
| `/home/container/state.json` | Last-published SHA. Delete to force full republish. |

## Related

- `_reference-pterodactyl-constraints.md`
- `_process-debug-sync.md` — what to check when sync isn't working
- `_flow-egg-install.md`
