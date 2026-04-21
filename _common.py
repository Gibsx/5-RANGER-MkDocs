"""
_common.py — plumbing shared by entry.py and sync.py
────────────────────────────────────────────────────

Tiny utilities for polling GitHub, fetching tarballs, persisting the
last-published SHA, and parsing ``manifest.yaml``. Kept stdlib-only
except for ``requests`` + ``PyYAML``, both of which the MkDocs Material
stack already pulls in.

The sync script imports:

    from _common import (
        fetch_branch_sha, fetch_tarball,
        read_last_sha, write_last_sha,
        load_manifest, section_url_path,
    )

No filesystem config loader lives here — Pterodactyl provides all config
as env vars (see ``sync.py:load_sync_config``).
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Dict, List, Tuple

import requests
import yaml


# ── GitHub fetch ─────────────────────────────────────────────────────────────

def fetch_branch_sha(repo: str, branch: str, token: str) -> str:
    """Head commit SHA of `branch` on `repo`. Raises on network / auth failure."""
    url = f"https://api.github.com/repos/{repo}/git/ref/heads/{branch}"
    r = requests.get(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["object"]["sha"]


# Hard ceiling on compressed tarball size. The content repo is ~a few MB of
# markdown + PNGs; anything over 100 MB either means a junk file slipped in
# or a malicious response. Loading multi-hundred-MB tarballs into RAM on
# Pterodactyl's constrained memory would OOM-kill the container before the
# next poll, so we refuse-and-retry rather than extract.
_MAX_TARBALL_BYTES = 100 * 1024 * 1024

# Ceiling on total uncompressed bytes to extract. Independent from the
# compressed ceiling because gzip amplifies ~5–10× on text — a tiny zip
# bomb could still inflate to gigabytes. 500 MB is ~10× the expected repo
# size and still comfortably under Pterodactyl's disk quota.
_MAX_EXTRACTED_BYTES = 500 * 1024 * 1024


def fetch_tarball(repo: str, sha: str, token: str, dest: Path) -> None:
    """
    Download `sha` as a tarball and extract into `dest`, flattening the
    `<repo>-<sha>/` wrapper so extracted files sit at the root of `dest`.

    Stream the download to bound RAM — the whole tarball is not held in
    memory at once. Each tar member is size-checked and path-validated
    before extraction; absolute paths, parent-dir escapes, and symlinks
    are refused to prevent a malicious tarball from writing outside
    `dest` or overwriting arbitrary files on the container. See the
    ceilings above for the size thresholds.
    """
    url = f"https://api.github.com/repos/{repo}/tarball/{sha}"
    r = requests.get(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=60,
        stream=True,
    )
    r.raise_for_status()

    # Accumulate into a BytesIO but stop hard if we exceed the compressed cap.
    buf = io.BytesIO()
    total = 0
    for chunk in r.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_TARBALL_BYTES:
            raise RuntimeError(
                f"Tarball for {repo}@{sha[:8]} exceeded "
                f"{_MAX_TARBALL_BYTES} bytes — refusing to extract."
            )
        buf.write(chunk)
    buf.seek(0)

    dest_resolved = dest.resolve()
    extracted = 0
    with tarfile.open(fileobj=buf, mode="r:gz") as tf:
        for member in tf.getmembers():
            # Reject anything that isn't a plain file or directory. Symlinks
            # and hardlinks in a tar can target paths outside `dest` even
            # after we rewrite `.name` below — the safe policy is to drop them.
            if not (member.isfile() or member.isdir()):
                continue

            parts = Path(member.name).parts
            if len(parts) <= 1:
                continue  # the `<repo>-<sha>/` top-level entry itself

            rewritten = Path(*parts[1:])

            # Absolute paths or parent-dir escapes → refuse. `..` anywhere
            # in the rewritten path would let the tar write outside `dest`.
            if rewritten.is_absolute() or ".." in rewritten.parts:
                continue
            target = (dest_resolved / rewritten).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError:
                # Resolves outside dest — refuse.
                continue

            if member.isfile():
                extracted += member.size
                if extracted > _MAX_EXTRACTED_BYTES:
                    raise RuntimeError(
                        f"Extracted size for {repo}@{sha[:8]} exceeded "
                        f"{_MAX_EXTRACTED_BYTES} bytes — possible zip bomb."
                    )

            member.name = str(rewritten)
            tf.extract(member, dest)  # noqa: S202 — validated above


# ── State persistence ───────────────────────────────────────────────────────
#
# Two fields:
#   - last_sha: the content-repo commit we last published. Short-circuit
#               the poll if the remote main SHA matches.
#   - publisher_version: bumped in sync.py when the render pipeline itself
#               (mkdocs.yml generation, tags.md body, home-page structure)
#               changes. If this differs from what's on disk, we force a
#               republish even when last_sha matches — otherwise an egg
#               update that changed render logic would never materialise
#               because the content repo hadn't moved.

def read_state(path: Path) -> Dict[str, str]:
    """
    Return {"last_sha": ..., "publisher_version": ...}. Missing / corrupt
    state reads as empty strings so the caller triggers a full publish.
    """
    if not path.is_file():
        return {"last_sha": "", "publisher_version": ""}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"last_sha": "", "publisher_version": ""}
    return {
        "last_sha": str(raw.get("last_sha", "")),
        "publisher_version": str(raw.get("publisher_version", "")),
    }


def write_state(path: Path, *, last_sha: str, publisher_version: str) -> None:
    """Atomic write via tmp-then-rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"last_sha": last_sha, "publisher_version": publisher_version}),
        encoding="utf-8",
    )
    tmp.replace(path)


# Back-compat shims. Older sync.py revisions imported these names directly;
# keeping them pointed at the new reader/writer means a staggered egg
# rollout (new _common.py, old sync.py) still boots cleanly.
def read_last_sha(path: Path) -> str:
    return read_state(path)["last_sha"]


def write_last_sha(path: Path, sha: str) -> None:
    # Preserve any existing publisher_version when the old signature is used.
    existing = read_state(path)
    write_state(path, last_sha=sha, publisher_version=existing["publisher_version"])


# ── Manifest ────────────────────────────────────────────────────────────────

def load_manifest(staging: Path) -> List[Dict[str, str]]:
    """
    Return the manifest's `sections:` list as dicts (slug/title/file/tags).

    Every platform sync uses this to drive its own nav / sidebar / ToC so
    the authoritative order lives in one place: `manifest.yaml`.
    """
    with (staging / "manifest.yaml").open("r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("sections") or []


def section_url_path(file: str) -> Tuple[str, str]:
    """
    Split a section filename into (basename_without_ext, ext).

    e.g. "01-communications.md" → ("01-communications", ".md"). Used by
    platforms that key pages on the basename (MkDocs, HonKit, Gollum).
    """
    p = Path(file)
    return (p.stem, p.suffix)
