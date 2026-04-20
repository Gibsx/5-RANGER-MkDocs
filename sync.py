#!/usr/bin/env python3
"""
/opt/mkdocs-sync/sync.py
────────────────────────
Wiki-side puller for the RANGER-AIDE-MEMOIRE content repo (MkDocs Material).

Runs as a systemd oneshot fired every 60s by `mkdocs-sync.timer`. One pass:

  1. Ask GitHub for the latest commit SHA on `$BRANCH` of `$REPO`.
  2. Compare against the SHA we published last time ($STATE_FILE).
  3. If unchanged, exit 0 immediately — no-op cheap path.
  4. If changed, download the branch tarball, extract to a staging dir.
  5. Regenerate `$MKDOCS_YML` from the extracted `manifest.yaml` so the
     MkDocs nav matches the current section list.
  6. rsync the staging dir into `$DOCS_DIR`.
  7. `systemctl try-restart mkdocs` so yml-level changes (new sections,
     renames) take effect — livereload only watches docs/*, not the yml.
  8. Write the new SHA to $STATE_FILE.

Why a separate wiki-side puller (not driven by the bot):
  The bot and the wiki both need the aide memoire markdown. If the bot
  pushed to the wiki we'd couple two deploys (bot down → wiki stale).
  Having the wiki pull directly from the same content repo makes it
  self-healing and lets the wiki run on a host the bot can't even reach.

Upstream:   GitHub repo `Gibsx/RANGER-AIDE-MEMOIRE`, `main` branch.
Downstream: MkDocs Material serving the site on the wiki host.
Shared:     poll / tarball / state plumbing from sibling `_common.py`.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

import yaml

# Path-adjacent import: /opt/mkdocs-sync/_common.py is shipped alongside.
sys.path.insert(0, str(Path(__file__).parent))
from _common import (  # noqa: E402
    fetch_branch_sha,
    fetch_tarball,
    load_env,
    load_manifest,
    read_last_sha,
    section_url_path,
    write_last_sha,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("mkdocs-sync")


# ── mkdocs.yml regeneration ──────────────────────────────────────────────────

# Template for the output mkdocs.yml. Kept as a Python dict (dumped via PyYAML)
# rather than a string template so the YAML is always well-formed regardless
# of section titles containing quotes / colons / special chars. The `nav:`
# entry is filled in at runtime from the manifest.
_MKDOCS_CONFIG_BASE: dict = {
    "site_name": "Ranger Aide Memoire",
    "site_description": (
        "5th Battalion, Ranger Regiment — doctrine, SOPs, and field craft."
    ),
    # Material's `tags` plugin renders per-page tag chips from YAML
    # frontmatter AND a browsable index at /tags/. `tags_file` tells it
    # which page to render the index into — we stage tags.md in the docs
    # dir on every sync. Without `tags_file` the plugin still renders
    # chips but there's no index page to link from the nav.
    "plugins": [
        "search",
        {"tags": {"tags_file": "tags.md"}},
    ],
    "theme": {
        "name": "material",
        "features": [
            # sections:  render top-level nav entries as section headers
            # expand:    auto-expand collapsible groups on page load so
            #            soldiers see every section without an extra click
            # indexes:   lets a group have its own landing page (unused
            #            today but cheap to enable for future use)
            "navigation.sections",
            "navigation.expand",
            "navigation.indexes",
            "navigation.top",
            "search.highlight",
            "search.suggest",
            "content.code.copy",
        ],
        "palette": [
            {
                "media": "(prefers-color-scheme: light)",
                "scheme": "default",
                "primary": "black",
                "accent": "red",
                "toggle": {
                    "icon": "material/brightness-7",
                    "name": "Switch to dark mode",
                },
            },
            {
                "media": "(prefers-color-scheme: dark)",
                "scheme": "slate",
                "primary": "black",
                "accent": "red",
                "toggle": {
                    "icon": "material/brightness-4",
                    "name": "Switch to light mode",
                },
            },
        ],
    },
    "markdown_extensions": [
        "admonition",
        "pymdownx.details",
        "pymdownx.superfences",
        "pymdownx.tabbed",
        {"toc": {"permalink": True}},
    ],
}


def render_mkdocs_yml(sections: List[dict], mkdocs_yml: Path) -> None:
    """
    Write a fresh mkdocs.yml whose `nav:` exactly reflects manifest.yaml.

    Why regenerate every sync: the manifest is authoritative. Editors add,
    reorder, or rename sections and we want those changes to show up in
    the wiki sidebar without a manual edit. A hand-maintained mkdocs.yml
    would drift. Keep the theme/extensions chrome as a constant here so
    operators only need to touch one place to tune the theme.

    Nav layout: sections are bucketed by their manifest `group:` field.
    Groups appear in first-occurrence order from the manifest, each as a
    collapsible sidebar header with its sections nested beneath. Sections
    lacking a `group:` sit at the top level alongside the group headers.
    A manifest with no `group:` fields at all → a flat nav, identical to
    the pre-grouping behaviour.
    """
    nav: List[dict] = [{"Home": "index.md"}]

    # Preserve first-occurrence order of groups. Python 3.7+ dicts keep
    # insertion order, so this doubles as an ordered set of group names.
    groups: Dict[str, List[dict]] = {}
    ungrouped: List[dict] = []
    for entry in sections:
        # dict-style nav entry: `{"Communications": "01-communications.md"}`.
        # MkDocs renders these with the key as the sidebar label.
        nav_entry = {str(entry["title"]): str(entry["file"])}
        group = (entry.get("group") or "").strip()
        if group:
            groups.setdefault(group, []).append(nav_entry)
        else:
            ungrouped.append(nav_entry)

    for group_name, children in groups.items():
        # `{"Fieldcraft": [{"Communications": "..."}, ...]}` — MkDocs renders
        # the key as a collapsible section header over its child pages.
        nav.append({group_name: children})
    nav.extend(ungrouped)

    # Append a "Tags" entry so the tag index page is reachable from the
    # sidebar. The Material `tags` plugin writes the index into tags.md
    # (see `plugins:` in _MKDOCS_CONFIG_BASE); without a nav entry the
    # page exists but is invisible.
    nav.append({"Tags": "tags.md"})

    cfg = {**_MKDOCS_CONFIG_BASE, "nav": nav}
    mkdocs_yml.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


# ── Tag frontmatter injection ───────────────────────────────────────────────

def inject_tags_frontmatter(sections: List[dict], staging: Path) -> None:
    """
    Prepend a YAML frontmatter block with `tags:` to each section .md that
    declares tags in the manifest.

    Why: the Material `tags` plugin reads tag names from per-page YAML
    frontmatter. The content repo's .md files don't carry frontmatter
    (they're a flat shared source for all five wikis, most of which have
    no notion of tags), so we inject at sync-time from the manifest.

    Idempotent: if the file already opens with a `---\\n` frontmatter
    block we rewrite it in place rather than stacking a second one. This
    matters on a re-sync where a cached staging dir might already be
    annotated.
    """
    for entry in sections:
        tags = entry.get("tags") or []
        if not tags:
            continue
        md_path = staging / str(entry["file"])
        if not md_path.is_file():
            continue

        body = md_path.read_text(encoding="utf-8")
        # Strip any pre-existing frontmatter so we never double-stack.
        if body.startswith("---\n"):
            end = body.find("\n---\n", 4)
            if end != -1:
                body = body[end + 5:]

        # yaml.safe_dump would quote strings inconsistently; hand-roll the
        # tiny block so the output is stable + diff-friendly.
        tag_lines = "\n".join(f"  - {t}" for t in tags)
        frontmatter = f"---\ntags:\n{tag_lines}\n---\n\n"
        md_path.write_text(frontmatter + body, encoding="utf-8")


def ensure_tags_index(staging: Path) -> None:
    """
    Write a `tags.md` placeholder containing the `[TAGS]` macro that the
    Material tags plugin replaces with the browsable tag index at build
    time. Without this file the plugin logs a warning and the `/tags/`
    URL 404s.
    """
    (staging / "tags.md").write_text(
        "# Tags\n\n"
        "Browse sections of the aide memoire by training course or topic.\n\n"
        "[TAGS]\n",
        encoding="utf-8",
    )


# ── Home page ───────────────────────────────────────────────────────────────

def ensure_home_page(sections: List[dict], docs_staging: Path) -> None:
    """
    Write a Home (index.md) landing page listing every section with links.

    The RANGER-AIDE-MEMOIRE content repo has no index.md of its own (the
    forum and wiki are both downstream views of the same manifest+.md set),
    so we synthesise one here. Matches the forum's pinned Contents thread
    so soldiers see the same landing experience in both mirrors.
    """
    lines = [
        "# Ranger Aide Memoire",
        "",
        (
            "Doctrine, SOPs, and field craft for 5th Battalion, the Ranger "
            "Regiment. Use the sidebar or the search box (top right) to "
            "find a section."
        ),
        "",
        "## Sections",
        "",
    ]
    for entry in sections:
        stem, _ = section_url_path(str(entry["file"]))
        # MkDocs renders `<stem>.md` → `/<stem>/`.
        lines.append(f"- [{entry['title']}]({stem}/)")
    lines.append("")

    (docs_staging / "index.md").write_text("\n".join(lines), encoding="utf-8")


# ── Rsync publish ────────────────────────────────────────────────────────────

def rsync_publish(staging: Path, docs_dir: Path) -> None:
    """
    Mirror `staging` into `docs_dir`. `--delete` removes any files that no
    longer exist in the manifest-driven staging tree, so a removed section's
    .md and images don't linger in the served site.

    We rsync (not mv) so the operation is idempotent and near-atomic from
    mkdocs-serve's perspective — livereload sees a short burst of file
    changes, not a wholesale directory swap. `-a` preserves timestamps so
    only genuinely-changed files trigger rerenders.
    """
    docs_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "rsync", "-a", "--delete",
            # Don't ship the raw manifest/README/.git stuff — only content
            # MkDocs actually needs. Everything else is repo machinery.
            "--exclude=manifest.yaml",
            "--exclude=README.md",
            "--exclude=.git",
            "--exclude=.github",
            f"{staging}/",
            f"{docs_dir}/",
        ],
        check=True,
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    env_path = Path(os.environ.get("MKDOCS_SYNC_CONFIG", "/opt/mkdocs-sync/config.env"))
    env = load_env(env_path)
    repo       = env["REPO"]
    branch     = env["BRANCH"]
    token      = env["TOKEN"]
    docs_dir   = Path(env["DOCS_DIR"])
    mkdocs_yml = Path(env["MKDOCS_YML"])
    state_file = Path(env["STATE_FILE"])

    try:
        sha = fetch_branch_sha(repo, branch, token)
    except Exception as exc:
        log.error("Failed to fetch branch SHA: %s", exc)
        return 2

    last = read_last_sha(state_file)
    if sha == last:
        log.info("No change (sha=%s) — skipping publish.", sha[:7])
        return 0

    log.info("New SHA %s (was %s); publishing.", sha[:7], last[:7] or "none")

    with tempfile.TemporaryDirectory(prefix="ram-sync-") as td:
        staging = Path(td)
        try:
            fetch_tarball(repo, sha, token, staging)
        except Exception as exc:
            log.error("Failed to fetch/extract tarball: %s", exc)
            return 3

        if not (staging / "manifest.yaml").is_file():
            log.error("manifest.yaml missing from fetched repo — refusing to publish.")
            return 4

        try:
            sections = load_manifest(staging)
            # Tag frontmatter must be injected BEFORE rsync — editing after
            # publish would race mkdocs-serve's livereload and we'd see a
            # flash of un-tagged pages.
            inject_tags_frontmatter(sections, staging)
            ensure_tags_index(staging)
            ensure_home_page(sections, staging)
            render_mkdocs_yml(sections, mkdocs_yml)
            rsync_publish(staging, docs_dir)
        except subprocess.CalledProcessError as exc:
            log.error("rsync failed: %s", exc)
            return 5
        except Exception as exc:
            log.exception("Publish failed: %s", exc)
            return 6

    # Fix ownership on any new files so mkdocs-serve (running as ranger-wiki)
    # can read them. rsync --chown requires --super; cheaper to chown after.
    try:
        shutil.chown(docs_dir, user="ranger-wiki", group="ranger-wiki")
        for p in docs_dir.rglob("*"):
            shutil.chown(p, user="ranger-wiki", group="ranger-wiki")
        shutil.chown(mkdocs_yml, user="ranger-wiki", group="ranger-wiki")
    except (LookupError, PermissionError) as exc:
        log.warning("chown fixup skipped: %s", exc)

    # Restart mkdocs-serve so a new mkdocs.yml (nav / title / theme changes)
    # is picked up. Livereload only watches docs/* for file changes — it
    # does not re-read mkdocs.yml, so a manifest-driven nav update would
    # otherwise stay invisible until the next manual restart. The restart
    # is a ~2s blip; the site is a read-only doctrine mirror so downtime
    # of that order is fine. `try-restart` is a no-op if the unit isn't
    # active yet (first boot before mkdocs.service came up).
    try:
        subprocess.run(
            ["systemctl", "try-restart", "mkdocs"],
            check=False, timeout=10,
        )
    except Exception as exc:  # defensive; never fail the publish for this
        log.warning("mkdocs restart skipped: %s", exc)

    write_last_sha(state_file, sha)
    log.info("Published %s — %d sections visible.", sha[:7], len(list(docs_dir.glob("*.md"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
