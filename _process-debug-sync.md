# _process-debug-sync.md

When the wiki isn't updating or `mkdocs build` is failing.

## Quick checks

1. **Is the panel showing the allocation line?**
   ```
   Allocation: http://$SERVER_IP:$SERVER_PORT/
   ```
   If missing, `entry.py` never reached the serve stage. Check the install log.

2. **Is the sync loop running?** Look for repeating `Poll: SHA unchanged` or `Poll: new SHA detected` lines. If absent, the loop crashed — check the stack trace above.

3. **Is the site serving but stale?**
   - Delete `state.json` on the container via file manager.
   - Restart. Next tick re-downloads the tarball and rebuilds from scratch.

## Common failures

### `pip install` fails on boot

Symptoms: `_bootstrap_deps` logs a failure, `.pydeps/` is wiped.

Causes and fixes:
- **`/tmp` overflow** — confirm `TMPDIR=/home/container/.piptmp` is set before pip is invoked in `entry.py`. `/tmp` is ~64 MB and wheel builds overflow it.
- **No compile toolchain** — a dep is pulling in a non-pure-Python wheel. Check `requirements.txt`; replace with a pure-Python alternative.
- **Transient network** — Pterodactyl sometimes has a slow first-boot network window. Restart once.

### `mkdocs build` fails

- **Deprecation warning abort** — `--strict` should **not** be set. Check `sync.py` — the invocation is `python -m mkdocs build`, no `--strict`.
- **Unknown config key** — Material deprecated a key. Check the warning, update `_MKDOCS_CONFIG_BASE` in `sync.py`.
- **Markdown parse error in a section** — the content repo has a malformed file. Check the line number in the build log. Fix in `RANGER-AIDE-MEMOIRE` and push; the fix propagates to Robinson and the MkDocs egg within `SYNC_INTERVAL` seconds.

### Tarball download fails

- **401 Unauthorized** — `TOKEN` is wrong or missing the **Contents: Read** scope on the content repo.
- **404 Not Found** — `REPO` or `BRANCH` wrong.
- **403 rate-limited** — unauthenticated quota is 60/hr. Confirm `TOKEN` is being sent in the `Authorization: token …` header (check `sync.py`).

### Serve works but shows "Not Found" for a section

- The section's slug doesn't match its filename. `use_directory_urls: true` means `01-communications.md` serves at `01-communications/`. Manifest `slug:` is used only for forum publishing (Robinson), not for wiki URLs.
- Check `nav:` in the generated `mkdocs.yml` for the section.

### Images missing on the wiki

- Verify the image lives in the content repo alongside the `.md`.
- Verify the markdown uses the bare filename: `![alt](01-communications-1-prc152.png)`.
- If images show in the Discord forum but not MkDocs, confirm the extraction to `/home/container/content/` preserved them (check the file manager).

## Reset path

Full reset if everything's wedged:
1. Stop the server in the panel.
2. Delete `state.json`, `.pydeps/`, `site/`, `content/` via the file manager.
3. Start. `entry.py` re-bootstraps pip → next tick downloads tarball → `mkdocs build` runs.

Nothing in the container is irreplaceable; all state comes from the content repo.

## Related

- `_reference-pterodactyl-constraints.md` — why the container is finicky
- `_flow-egg-install.md` — what `install.sh` and `entry.py` do
- `_flow-poll-and-rebuild.md` — sync loop sequence
