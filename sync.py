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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

# Path-adjacent import: _common.py ships alongside this file.
sys.path.insert(0, str(Path(__file__).parent))
from _common import (  # noqa: E402
    fetch_branch_sha,
    fetch_tarball,
    load_manifest,
    read_state,
    write_state,
)

log = logging.getLogger("mkdocs-sync")


# Bump this whenever the render pipeline changes in a way that requires a
# republish even when the content-repo SHA hasn't moved — e.g. mkdocs.yml
# plugin config, tags.md body, home-page structure, theme features. On the
# next run after an egg update, run_once() sees state.json's stored
# version differ from this constant and forces a rebuild.
#
# Versions:
#   1 — original (bare `tags` plugin, no grouped home page, no nav tabs)
#   2 — `tags_file: tags.md` plugin config, tags.md body has no [TAGS]
#       macro, home page grouped by manifest `group:`, navigation.tabs
#       feature, UNGROUPED ("General") bucket always renders last.
#   3 — dropped navigation.tabs (the top-header tab strip demoted Home
#       and cluttered the header at small widths). Tags entry stays in
#       the sidebar.
PUBLISHER_VERSION = "3"


# ── Config from env ─────────────────────────────────────────────────────────

def _env(name: str, default: Optional[str] = None) -> str:
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
        # `tags` is configured with an explicit `tags_file` so the Material
        # Community Edition appends a browsable tag index onto tags.md at
        # build time. The bare `[TAGS]` macro we once relied on is an
        # Insiders-only shorthand — on CE it rendered as literal text and
        # the Tags page showed no chips. `tags_file:` is the documented CE
        # API and produces the index reliably; the deprecation warning
        # mentioned in MkDocs Material 9.6 release notes is accepted
        # because we don't run with --strict (see mkdocs_build).
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
                # (navigation.tabs was tried but demoted Home into the
                # top-header tab strip, which readers found confusing.
                # The sidebar alone carries nav now; the Tags entry
                # still sits in the sidebar via render_mkdocs_yml.)
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

    # Bucket by manifest `group:` with a "General" fallback for
    # ungrouped sections. Named groups render in first-occurrence
    # order; the ungrouped bucket ALWAYS renders last, as its own
    # collapsible "General" section, regardless of where its first
    # member appears in the manifest. This keeps the sidebar, the
    # home page (ensure_home_page below), and the Discord forum
    # Contents pin (forum.py:_render_contents_body) in lockstep:
    # all three show named groups first, General last, same order.
    UNGROUPED = "General"
    group_order: List[str] = []
    buckets: Dict[str, List[dict]] = {}
    for entry in sections:
        nav_entry = {str(entry["title"]): str(entry["file"])}
        key = (entry.get("group") or "").strip() or UNGROUPED
        if key not in buckets:
            buckets[key] = []
            group_order.append(key)
        buckets[key].append(nav_entry)

    ordered = [g for g in group_order if g != UNGROUPED]
    if UNGROUPED in buckets:
        ordered.append(UNGROUPED)
    for group_name in ordered:
        nav.append({group_name: buckets[group_name]})

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
        raw_tags = entry.get("tags") or []
        # Accept `tags: foo` (scalar) as well as `tags: [foo, bar]`. A
        # scalar coerces to a single-element list; without this coercion
        # `for t in tags` would iterate the string character-by-character
        # and emit `- f`, `- o`, `- o` as frontmatter, silently corrupting
        # the built site. The Discord-Apps aide-memoire loader
        # (content.py) does the same normalisation — keep these two in
        # lockstep when adding manifest fields.
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
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
    # No `[TAGS]` macro: with `tags_file: tags.md` in the plugin config
    # the Material Community tags plugin appends the browsable index
    # after the page body automatically. Leaving `[TAGS]` here would
    # render as literal text on CE (it's an Insiders-only shorthand)
    # and confuse readers.
    (staging / "tags.md").write_text(
        "# Tags\n\n"
        "Browse sections of the aide memoire by training course or topic.\n",
        encoding="utf-8",
    )


# ── Home page ───────────────────────────────────────────────────────────────

def ensure_home_page(sections: List[dict], docs_staging: Path) -> None:
    """
    Synthesise an index.md landing page listing every section, bucketed
    by the manifest `group:` field.

    The content repo has no index.md of its own — the forum and wiki are
    both downstream views of the same manifest + .md set, so neither owns
    a landing page. The home page mirrors the sidebar structure (which
    render_mkdocs_yml also derives from `group:`) so readers see the
    same organisation whether they're navigating via the tabs/sidebar or
    scanning the landing page.

    Groups appear in manifest first-occurrence order (Python 3.7+ dicts
    preserve insertion order). Sections without a `group:` render under a
    "General" header so the index never mixes bare links with grouped
    ones inconsistently — same convention as the Discord Contents pin in
    `common/ranger_aide_memoire/forum.py:_render_contents_body`.
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

    # Bucket by group. `UNGROUPED` mirrors the Discord Contents pin's
    # "General" label so the two landing indexes match. Named groups
    # render in first-occurrence order; the UNGROUPED bucket always
    # renders LAST (same invariant as the sidebar nav in
    # render_mkdocs_yml — keeps sidebar / home page / Discord Contents
    # pin in lockstep even when an ungrouped section happens to
    # appear first in the manifest).
    UNGROUPED = "General"
    group_order: List[str] = []
    buckets: Dict[str, List[dict]] = {}
    for entry in sections:
        key = (entry.get("group") or "").strip() or UNGROUPED
        if key not in buckets:
            buckets[key] = []
            group_order.append(key)
        buckets[key].append(entry)

    ordered = [g for g in group_order if g != UNGROUPED]
    if UNGROUPED in buckets:
        ordered.append(UNGROUPED)
    for group_name in ordered:
        lines.append(f"### {group_name}")
        lines.append("")
        for entry in buckets[group_name]:
            # Link to the .md source filename, not the rendered /<slug>/ URL.
            # MkDocs' build-time link checker resolves source paths (it's
            # verifying the file exists) and emits an "unrecognized relative
            # link" warning if you hand it the pretty URL. With the .md
            # filename the resolver is happy AND the output HTML still
            # rewrites the href to /<slug>/ at build time (use_directory_urls
            # defaults to true), so users see the pretty URL in the browser.
            lines.append(f"- [{entry['title']}]({entry['file']})")
        lines.append("")

    (docs_staging / "index.md").write_text("\n".join(lines), encoding="utf-8")


# ── Rsync publish ────────────────────────────────────────────────────────────

# Files/dirs we never ship into docs_dir. manifest.yaml is consumed at
# sync time to build mkdocs.yml; README.md would collide with our
# synthesised index.md; .git* is repo machinery.
_PUBLISH_EXCLUDES = {"manifest.yaml", "README.md", ".git", ".github"}


def publish_to_docs(staging: Path, docs_dir: Path) -> None:
    """
    Mirror ``staging`` into ``docs_dir``, deleting any files that no
    longer appear in the staging tree.

    Stdlib-only (no rsync binary) because the Pterodactyl egg's
    ``python_3.11`` image is slim — it ships Python and not much else.
    The previous rsync-subprocess approach worked on the VPS deploy but
    fails in the container with an opaque FileNotFoundError on the
    rsync exec.

    Semantics:
      - Every file under staging (minus excludes) is copied into the
        matching relative path under docs_dir.
      - Any file under docs_dir not present in staging is deleted.
      - Empty dirs left behind after delete are pruned so stale
        section dirs don't clutter the served site.
    """
    docs_dir.mkdir(parents=True, exist_ok=True)

    # Build the set of staging-relative paths we intend to publish.
    wanted: Set[Path] = set()
    for src in staging.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(staging)
        # Top-level exclude: the first path segment matches an excluded
        # name. Matches both files (manifest.yaml) and dirs (.git/*).
        if rel.parts and rel.parts[0] in _PUBLISH_EXCLUDES:
            continue
        wanted.add(rel)
        dst = docs_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        # copy2 preserves mtime so MkDocs' incremental checks and any
        # caching proxy don't see spurious changes on a no-op content
        # republish where the file bytes happen to match.
        shutil.copy2(src, dst)

    # Delete files that previously existed but are no longer in the
    # manifest-driven staging set. Mirrors rsync --delete.
    for dst in list(docs_dir.rglob("*")):
        if dst.is_dir():
            continue
        rel = dst.relative_to(docs_dir)
        if rel not in wanted:
            try:
                dst.unlink()
            except FileNotFoundError:
                pass

    # Prune any dirs left empty by the deletion pass. Walk bottom-up so
    # we only try to rmdir a parent once its children are gone.
    for d in sorted(
        (p for p in docs_dir.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            d.rmdir()  # only succeeds if empty; that's what we want
        except OSError:
            pass


# ── mkdocs build ────────────────────────────────────────────────────────────

def mkdocs_build(mkdocs_yml: Path, site_dir: Path) -> None:
    """
    Run ``mkdocs build`` into a sibling temp dir, then atomically swap it
    into ``site_dir``. Two reasons we don't build in place:

      1. ``--clean`` erases the previous site dir up front. If the build
         then fails (bad YAML, Jinja error, plugin crash) the site is left
         blank — soldiers hitting the wiki see a 404 for every page until
         the next successful build. Building into a temp dir means the
         previous good site keeps serving until the new one is ready.

      2. Renaming a dir into place is effectively atomic; an HTTP request
         arriving mid-swap either sees the old tree or the new one, never
         a half-written mix.

    We still use build + static-file server rather than ``mkdocs serve``
    because Pterodactyl containers are single-process: the supervisor is
    ``entry.py``, not systemd, and serve's livereload adds no value for a
    read-only doctrine mirror.
    """
    # Invoke MkDocs via `python -m mkdocs` rather than the `mkdocs` CLI
    # shim. `pip install --target=` (our Pterodactyl install shape)
    # doesn't create entry-point scripts, so the `mkdocs` binary isn't
    # on $PATH inside the container. `python -m` resolves the package
    # off sys.path regardless of where it was installed.
    #
    # Subprocesses inherit env but NOT sys.path, so we must thread the
    # --target pydeps dir through PYTHONPATH explicitly — otherwise
    # `import mkdocs` fails in the child even though it works in this
    # process (entry.py's self-bootstrap only affects this process's
    # sys.path).
    pydeps = Path(os.environ.get("HOME_DIR", "/home/container")) / ".pydeps"
    env = os.environ.copy()
    if pydeps.is_dir():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(pydeps) + (os.pathsep + existing if existing else "")

    # Build into a staging dir next to the live site so the atomic rename
    # stays on the same filesystem (rename across filesystems falls back
    # to copy+delete, which is neither atomic nor crash-safe).
    build_staging = site_dir.parent / (site_dir.name + ".build")
    if build_staging.exists():
        shutil.rmtree(build_staging)
    build_staging.mkdir(parents=True, exist_ok=True)

    # Deliberately NOT --strict: MkDocs promotes deprecation notices to
    # config warnings, and --strict would turn any future Material
    # upstream deprecation into a fatal build failure that bricks the
    # wiki. For a read-only doctrine mirror we'd rather ship a working
    # site with a yellow warning in the logs than a 500 on the next
    # Material release. Genuine errors (missing file, bad YAML) still
    # fail the build without --strict.
    proc = subprocess.run(
        [
            sys.executable, "-m", "mkdocs", "build",
            "--clean",
            "-f", str(mkdocs_yml),
            "-d", str(build_staging),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Surface stderr so build failures are diagnosable from the
        # Pterodactyl console. Without this, check=True raises a bare
        # CalledProcessError with no output and ops are left guessing.
        shutil.rmtree(build_staging, ignore_errors=True)
        log.error("mkdocs build failed (rc=%s):\n%s", proc.returncode, proc.stderr)
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, proc.stdout, proc.stderr,
        )

    # Atomic swap: move the old site out of the way, the new one in,
    # then drop the old. os.replace is atomic on POSIX when source and
    # destination live on the same filesystem.
    old_backup = site_dir.parent / (site_dir.name + ".old")
    if old_backup.exists():
        shutil.rmtree(old_backup)
    if site_dir.exists():
        os.replace(site_dir, old_backup)
    os.replace(build_staging, site_dir)
    if old_backup.exists():
        shutil.rmtree(old_backup, ignore_errors=True)


# ── Entry points ────────────────────────────────────────────────────────────

def run_once(cfg: Optional[Dict[str, str]] = None) -> bool:
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
    persisted = read_state(state_file)
    last = persisted["last_sha"]
    last_version = persisted["publisher_version"]
    # Two short-circuit conditions must BOTH be satisfied: same content
    # SHA AND same publisher version. A code update that changed render
    # logic (new mkdocs.yml, new tags.md layout, etc.) bumps
    # PUBLISHER_VERSION; the mismatch forces a republish on the next
    # tick even though the content repo hasn't moved. Without this the
    # old generated files (old mkdocs.yml, old tags.md with [TAGS]
    # macro) would linger indefinitely until someone pushed an
    # unrelated content change.
    if sha == last and last_version == PUBLISHER_VERSION:
        log.info("No change (sha=%s, publisher=%s) — skipping publish.",
                 sha[:7], PUBLISHER_VERSION)
        return False

    if sha != last:
        log.info("New SHA %s (was %s); publishing.", sha[:7], last[:7] or "none")
    else:
        log.info(
            "Publisher version changed (%s → %s) at sha=%s; republishing.",
            last_version or "none", PUBLISHER_VERSION, sha[:7],
        )

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
        publish_to_docs(staging, docs_dir)
        mkdocs_build(mkdocs_yml, site_dir)

    write_state(state_file, last_sha=sha, publisher_version=PUBLISHER_VERSION)
    # Count section pages only (exclude generated index.md + tags.md).
    # Previously `len(list(docs_dir.glob("*.md")))` over-reported by 2
    # because the home page and tags index live alongside section files.
    log.info(
        "Published %s — %d sections visible.",
        sha[:7], len(sections),
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
