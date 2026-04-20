# 5-RANGER-MkDocs

Puller + publisher that mirrors a GitHub markdown content repo into an
[MkDocs Material](https://squidfunk.github.io/mkdocs-material/) site.

Originally built for [5 RANGER](https://github.com/Gibsx)'s `RANGER-AIDE-MEMOIRE`
— doctrine, SOPs, and field craft for 5th Battalion the Ranger Regiment
(Arma 3 milsim). Split out of the bot repo into its own public repo so
the Pterodactyl egg can reference asset URLs without a PAT, and so
anyone with a `manifest.yaml + NN-<slug>.md` shaped content repo can
reuse it without pulling in unrelated Discord bot code.

## What it does

Runs as a 60-second oneshot (systemd timer or in-container loop). Each
tick:

1. **Poll** GitHub for the content branch's head SHA.
2. **Skip** if it matches the last-published SHA (`state.json`) — cheap
   no-op path, typical steady-state cost is one `GET /git/ref` per minute.
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
7. **Restart the `mkdocs` serve unit** so `mkdocs.yml` changes (new
   sections, renames, nav-group changes) take effect — livereload only
   watches `docs/*`, not the yml.
8. **Persist the new SHA** to `state.json`.

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

## Deploy — systemd (VPS, LXC, bare-metal)

Tested on Debian 12 / Ubuntu 22.04 with Python 3.10+.

```bash
# 1. System packages
apt-get install -y python3 python3-pip rsync
pip3 install mkdocs mkdocs-material pymdown-extensions pyyaml requests

# 2. User + dirs
useradd -m -s /bin/bash ranger-wiki
mkdir -p /opt/mkdocs/docs /opt/mkdocs-sync
chown -R ranger-wiki:ranger-wiki /opt/mkdocs

# 3. Drop the sync script + plumbing next to each other
cp sync.py _common.py /opt/mkdocs-sync/
chmod 755 /opt/mkdocs-sync/sync.py
cp systemd/mkdocs-sync.service systemd/mkdocs-sync.timer /etc/systemd/system/

# 4. Config — fill in TOKEN from your password manager
cp config.env.example /opt/mkdocs-sync/config.env
$EDITOR /opt/mkdocs-sync/config.env
chmod 600 /opt/mkdocs-sync/config.env

# 5. MkDocs serve unit — run-as ranger-wiki, port 803 (adjust to taste)
cat > /etc/systemd/system/mkdocs.service <<EOF
[Unit]
Description=MkDocs — Ranger Aide Memoire
After=network.target

[Service]
Type=simple
User=ranger-wiki
Group=ranger-wiki
WorkingDirectory=/opt/mkdocs
ExecStart=/usr/local/bin/mkdocs serve -a 0.0.0.0:803
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 6. Enable everything
systemctl daemon-reload
systemctl enable --now mkdocs.service
systemctl enable --now mkdocs-sync.timer
systemctl start mkdocs-sync.service   # force one pass now
```

Browse to `http://<host>:803/`. Sidebar shows the manifest's groups
collapsible; `/tags/` shows the tag index.

## Deploy — Pterodactyl egg

*Coming soon* — see [`pterodactyl/`](./pterodactyl/) when populated.
Design outline:

- **Base image**: `ghcr.io/pterodactyl/yolks:python_3.11`
- **Runtime shape**: one foreground Python process (`entry.py`) with a
  sync-loop thread and an `mkdocs serve` subprocess supervisor. No
  systemd (containers have none); 60s `time.sleep()` loop replaces
  the timer, and SIGTERM → child kill → supervisor respawn replaces
  `systemctl try-restart`.
- **Env vars**: `REPO`, `BRANCH`, `TOKEN` (PAT), `SYNC_INTERVAL`,
  `SITE_NAME`, `SITE_DESCRIPTION`. `SERVER_PORT` comes from the
  Pterodactyl allocation.
- **State**: `state.json` persists across container restarts via the
  container's own filesystem (Pterodactyl preserves it).

The egg JSON will reference `raw.githubusercontent.com/Gibsx/5-RANGER-MkDocs/main/…`
for its install-script assets — that's the main reason this repo is
public.

## Operations

| Task | Command |
|---|---|
| Watch sync ticks | `journalctl -u mkdocs-sync -f` |
| Watch the serve process | `journalctl -u mkdocs -f` |
| Force a republish | `rm /opt/mkdocs-sync/state.json && systemctl start mkdocs-sync` |
| Rotate PAT | edit `/opt/mkdocs-sync/config.env` — read every tick, no restart |
| Tweak theme / extensions | edit `_MKDOCS_CONFIG_BASE` in `sync.py` — never hand-edit `/opt/mkdocs/mkdocs.yml`, it's regenerated every publish |

## Troubleshooting

- **`Failed to fetch branch SHA: 401`** — the PAT expired or lost its
  scope. Re-issue with `Contents: Read` on the content repo.
- **`manifest.yaml missing from fetched repo — refusing to publish`** —
  the repo layout changed. Restore the manifest at the repo root.
- **Section 404 after rename** — slugs drive the URL. Renames must be
  reflected in `manifest.yaml`'s `slug:` field.
- **`Unrecognised configuration name: plugins.tags`** — MkDocs Material
  is older than 8.2 (tags plugin was added then). `pip install -U mkdocs-material`.

## Design notes

- **Why a puller, not a pusher.** Keeping the wiki host pulling directly
  from GitHub decouples it from the bot that reads the same repo. The
  wiki stays up when the bot is down, and can live on a host the bot
  can't reach.
- **Why regenerate `mkdocs.yml` every tick.** The manifest is the
  authoritative section order. A hand-maintained `mkdocs.yml` would
  drift. Operator tuning of theme/extensions lives in `sync.py`'s
  `_MKDOCS_CONFIG_BASE` instead — one place to edit, survives every
  regen.
- **Why tag frontmatter is injected, not committed to the content repo.**
  The content repo is shared across multiple downstream views (bot,
  Discord forum, any future mirror). Keeping it free of MkDocs-specific
  YAML frontmatter means those other consumers don't have to filter it
  out. Injection happens at stage time so the source stays clean.

## License

MIT. Fork, adapt, reuse. If you ship a fork with meaningful changes, a
link back is appreciated but not required.
