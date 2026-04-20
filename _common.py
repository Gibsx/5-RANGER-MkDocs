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


def fetch_tarball(repo: str, sha: str, token: str, dest: Path) -> None:
    """
    Download `sha` as a tarball and extract into `dest`, flattening the
    `<repo>-<sha>/` wrapper so extracted files sit at the root of `dest`.

    In-memory download — content repos are small (few MB); simpler than
    streaming to disk and avoids a temp-file cleanup on partial failure.
    """
    url = f"https://api.github.com/repos/{repo}/tarball/{sha}"
    r = requests.get(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=60,
    )
    r.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        for member in tf.getmembers():
            parts = Path(member.name).parts
            if len(parts) <= 1:
                continue
            member.name = str(Path(*parts[1:]))
            tf.extract(member, dest)  # noqa: S202 — trusted auth'd source


# ── State persistence ───────────────────────────────────────────────────────

def read_last_sha(path: Path) -> str:
    """Last successfully published SHA, or '' on first run / corrupt state."""
    if not path.is_file():
        return ""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("last_sha", "")
    except (json.JSONDecodeError, OSError):
        return ""


def write_last_sha(path: Path, sha: str) -> None:
    """Atomic write via tmp-then-rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_sha": sha}), encoding="utf-8")
    tmp.replace(path)


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
