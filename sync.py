#!/usr/bin/env python3
"""
sync.py — RANGER-AIDE-MEMOIRE → MkDocs Material puller (Pterodactyl edition)
────────────────────────────────────────────────────────────────────────────

One sync pass:

  1. Ask GitHub for the latest commit SHA on ``$BRANCH`` of ``$REPO``.
  2. Compare against the last-published SHA in ``$STATE_FILE``.
  3. If unchanged → no-op.
  4. If changed → download the branch tarball, stage it, inject tag
     frontmatter, regenerate ``mkdocs.yml`` from the manifest, rsync into
     ``$DOCS_DIR``, run ``mkdocs build`` into ``$SITE_DIR``, persist SHA.

The serving process (``python -m http.server`` supervised by ``entry.py``)
reads files straight off disk per request, so we do **not** need to
restart anything after a build — rewriting ``$SITE_DIR`` is sufficient to
flip the user-visible content over.

Upstream:   env from ``entry.py`` (itself from Pterodactyl allocation / egg vars).
Downstream: static site rooted at ``$SITE_DIR``, served by ``entry.py``.
Shared:     poll/tarball/state/manifest plumbing from sibling ``_common.py``.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

import yaml

# Path-adjacent import: _common.py ships alongside this file.
sys.path.insert(0, str(Path(__file__).parent))
from _common import (  # noqa: E402
    fetch_branch_sha,
    fetch_tarball,
    load_manifest,
    read_last_sha,
    section_url_path,
    write_last_sha,
)

log = logging.getLogger("mkdocs-sync")


# ── Config from env ─────────────────────────────────────────────────────────

def _env(name: str, default: str | None = None) -> str:
    """
    Read a required env var, falling back to ``default`` if provided.
    Raises ``RuntimeError`` if the var is unset and has no default — fail
    loudly at container start rather than sync silently against a
    half-configured repo.
    """
    val = os.environ.get(name, default)
    if val is None or val == "":
        raise RuntimeError(f"Required env var {name} is unset")
    return val


def load_sync_config() -> Dict[str, str]:
    """
    Resolve every path / GitHub param from env. All paths live under
    ``/home/container`` by default, which is the persistent volume root
    Pterodactyl mounts into the container — state and built site both
    survive container restarts without extra mount config.
    """
    home = Path(os.environ.get("HOME_DIR", "/home/container"))
    site_name = os.environ.get("SITE_NAME", "Ranger Aide Memoire")
    site_description = os.environ.get(
        "SITE_DESCRIPTION",
        "5th Battalion, Ranger Regiment — doctrine, SOPs, and field craft.",
    )
    return {
        "repo":       _env("REPO"),
        "branch":     os.environ.get("BRANCH", "main"),
        "token":      _env("TOKEN"),
        "docs_dir":   str(home / "docs"),
        "site_dir":   str(home / "site"),
        "mkdocs_yml": str(home / "mkdocs.yml"),
        "state_file": str(home / "state.json"),
        "site_name":  site_name,
        "site_description": site_description,
    }


# ── mkdocs.yml regeneration ──────────────────────────────────────────────────

def _build_mkdocs_config(site_name: str, site_description: str) -> dict:
    """
    Base MkDocs config. Built fresh per sync (rather than module-level) so
    ``$SITE_NAME`` / ``$SITE_DESCRIPTION`` env changes take effect on the
    next tick without a container restart.
    """
    return {
        "site_name": site_name,
        "site_description": site_description,
        # Material's `tags` plugin renders per-page tag chips from YAML
        # frontmatter AND a browsable index at /tags/. `tags_file` tells it
        # which page to render the index into — we stage tags.md in the
        # docs dir on every sync.
        "plugins": [
            "search",
            {"tags": {"tags_file": "tags.md"}},
        ],
        "theme": {
            "name": "material",
            "features": [
                # sections:  render top-level nav entries as section headers
                # expand:    auto-expand collapsible groups on page load
                # indexes:   lets a group have its own landing page
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


def render_mkdocs_yml(
    sections: List[dict],
    mkdocs_yml: Path,
    site_name: str,
    site_description: str,
) -> None:
    """
    Write a fresh mkdocs.yml whose ``nav:`` exactly reflects manifest.yaml.

    Sections are bucketed by their manifest ``group:`` field. Groups
    appear in first-occurrence order (Python 3.7+ dicts preserve
    insertion order), each as a collapsible sidebar header with nested
    sections. Ungrouped sections sit at the top level alongside group
    headers. A manifest with no ``group:`` fields at all → a flat nav.
    """
    nav: List[dict] = [{"Home": "index.md"}]

    groups: Dict[str, List[dict]] = {}
    ungrouped: List[dict] = []
    for entry in sections:
        # dict-style nav entry: `{"Communications": "01-communications.md"}`.
        # MkDocs renders the key as the sidebar label.
        nav_entry = {str(entry["title"]): str(entry["file"])}
        group = (entry.get("group") or "").strip()
        if group:
            groups.setdefault(group, []).append(nav_entry)
        else:
            ungrouped.append(nav_entry)

    for group_name, children in groups.items():
        nav.append({group_name: children})
    nav.extend(ungrouped)

    # Append a "Tags" entry so the tag index page is reachable from the
    # sidebar. The Material `tags` plugin writes the index into tags.md;
    # without a nav entry the page exists but is invisible.
    nav.append({"Tags": "tags.md"})

    cfg = {**_build_mkdocs_config(site_name, site_description), "nav": nav}
    mkdocs_yml.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


# ── Tag frontmatter injection ───────────────────────────────────────────────

def inject_tags_frontmatter(sections: List[dict], staging: Path) -> None:
    """
    Prepend a YAML frontmatter block with ``tags:`` to each section .md
    that declares tags in the manifest.

    The Material tags plugin reads tags from per-page frontmatter. The
    content repo .md files carry no frontmatter (they're a flat shared
    source — the Discord bot and forum publisher would have to filter it
    out otherwise), so we inject at sync time from the manifest.

    Idempotent: if the file already opens with a ``---\\n`` block we
    replace it, never stack a second one.
    """
    for entry in sections:
        tags = entry.get("tags") or []
        if not tags:
            continue
        md_path = staging / str(entry["file"])
        if not md_path.is_file():
            continue

        body = md_path.read_text(encoding="utf-8")
        if body.startswith("---\n"):
            end = body.find("\n---\n", 4)
            if end != -1:
                body = body[end + 5:]

        tag_lines = "\n".join(f"  - {t}" for t in tags)
        frontmatter = f"---\ntags:\n{tag_lines}\n---\n\n"
        md_path.write_text(frontmatter + body, encoding="utf-8")


def ensure_tags_index(staging: Path) -> None:
    """
    Write a ``tags.md`` placeholder containing the ``[TAGS]`` macro that
    the Material tags plugin replaces with the browsable tag index at
    build time. Without this file the plugin logs a warning and ``/tags/``
    404s.
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
    Synthesise an index.md landing page listing every section. The content
    repo has no index.md of its own — the forum and wiki are both
    downstream views of the same manifest + .md set, so neither owns a
    landing page. Mirrors the forum's pinned Contents thread.
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
    Mirror ``staging`` into ``docs_dir``. ``--delete`` removes files that
    no longer appear in the staging tree so a removed section's .md and
    images don't linger.
    """
    docs_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "rsync", "-rlt", "--delete",
            # Don't ship repo machinery — only the content MkDocs actually
            # needs. manifest.yaml is consumed at sync time to build
            # mkdocs.yml; README.md would collide with our synthesised
            # index.md renderer preference.
            "--exclude=manifest.yaml",
            "--exclude=README.md",
            "--exclude=.git",
            "--exclude=.github",
            f"{staging}/",
            f"{docs_dir}/",
        ],
        check=True,
    )


# ── mkdocs build ────────────────────────────────────────────────────────────

def mkdocs_build(mkdocs_yml: Path, site_dir: Path) -> None:
    """
    Run ``mkdocs build`` with ``-d <site_dir>``. Clean mode erases the
    previous site dir, so a removed section's built HTML is cleaned up
    too (matches rsync --delete on the source side).

    We use build + static-file server rather than ``mkdocs serve`` because
    Pterodactyl containers are single-process: the supervisor is
    ``entry.py``, not systemd, and serve's livereload adds no value
    for a read-only doctrine mirror.
    """
    subprocess.run(
        [
            "mkdocs", "build",
            "--clean",
            "--strict",
            "-f", str(mkdocs_yml),
            "-d", str(site_dir),
        ],
        check=True,
    )


# ── Entry points ────────────────────────────────────────────────────────────

def run_once(cfg: Dict[str, str] | None = None) -> bool:
    """
    Do one sync pass. Returns True if a publish happened, False if the
    content was already up to date. Raises on failure so the caller
    (``entry.py``) can log and decide whether to keep looping.
    """
    if cfg is None:
        cfg = load_sync_config()

    docs_dir   = Path(cfg["docs_dir"])
    site_dir   = Path(cfg["site_dir"])
    mkdocs_yml = Path(cfg["mkdocs_yml"])
    state_file = Path(cfg["state_file"])

    sha = fetch_branch_sha(cfg["repo"], cfg["branch"], cfg["token"])
    last = read_last_sha(state_file)
    if sha == last:
        log.info("No change (sha=%s) — skipping publish.", sha[:7])
        return False

    log.info("New SHA %s (was %s); publishing.", sha[:7], last[:7] or "none")

    with tempfile.TemporaryDirectory(prefix="ram-sync-") as td:
        staging = Path(td)
        fetch_tarball(cfg["repo"], sha, cfg["token"], staging)

        if not (staging / "manifest.yaml").is_file():
            raise RuntimeError(
                "manifest.yaml missing from fetched repo — refusing to publish."
            )

        sections = load_manifest(staging)
        # Frontmatter injection must happen BEFORE rsync: editing inside
        # docs_dir after publish would race any live file watcher and
        # flash un-tagged pages.
        inject_tags_frontmatter(sections, staging)
        ensure_tags_index(staging)
        ensure_home_page(sections, staging)
        render_mkdocs_yml(
            sections, mkdocs_yml,
            cfg["site_name"], cfg["site_description"],
        )
        rsync_publish(staging, docs_dir)
        mkdocs_build(mkdocs_yml, site_dir)

    write_last_sha(state_file, sha)
    log.info(
        "Published %s — %d sections visible.",
        sha[:7], len(list(docs_dir.glob("*.md"))),
    )
    return True


def main() -> int:
    """CLI entry for one-shot testing (``python sync.py``)."""
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
    )
    try:
        run_once()
        return 0
    except Exception as exc:
        log.exception("Sync failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
