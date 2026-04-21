# _process-change-theme.md

Changing Material theme settings (palette, features, plugins) safely.

## Steps

1. **Edit `sync.py`.** The theme lives in `_MKDOCS_CONFIG_BASE` — a Python string template rendered into `mkdocs.yml` on every sync. Never hand-edit the generated `mkdocs.yml`; it's overwritten on next tick.

2. **Choose your change carefully.** Reference Material docs: https://squidfunk.github.io/mkdocs-material/setup/

   Common adjustments:
   - Palette → `theme.palette.primary` / `theme.palette.accent`
   - New feature → append to `theme.features`
   - New plugin → append to `plugins` (and add the pip dep to `requirements.txt` if it's external)

3. **Smoke-test in the sandbox.**
   ```bash
   cd /home/claude/5rangerbot/5-RANGER-MkDocs
   REPO=Gibsx/RANGER-AIDE-MEMOIRE BRANCH=main GITHUB_TOKEN=$(gh auth token) \
     python3 sync.py
   ```
   Then browse `site/index.html` locally or `python3 -m http.server --directory site 8080`.

4. **Deploy.**
   - Push `sync.py` to `Gibsx/5-RANGER-MkDocs`.
   - On the Pterodactyl panel, reinstall (or SFTP the file + restart).
   - Delete `state.json` on the container if you want the change to take effect immediately rather than waiting for the next content push.

## Avoid

- **`--strict` build mode.** Material deprecates config keys frequently; a deprecation warning under `--strict` blanks the wiki.
- **Per-page theme overrides** via frontmatter. The manifest is the source of truth; per-page drift breaks the "one manifest → consistent views" invariant.
- **Installing a heavy pip dep.** The Pterodactyl image has no compile toolchain. Pure-Python wheels only. If a plugin needs compilation, find an alternative.

## Rollback

Revert the `sync.py` change, push, reinstall. Next sync tick rebuilds with the old theme. Since the build is fully deterministic from `manifest.yaml` + `sync.py`, no state corruption is possible.

## Related

- `_reference-theme-features.md` — what's enabled and why
- `_reference-mkdocs-yml-generation.md` — how the theme is rendered
- `_reference-pterodactyl-constraints.md` — what limits your choices
