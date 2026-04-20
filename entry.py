#!/usr/bin/env python3
"""
entry.py — Pterodactyl container entry point
─────────────────────────────────────────────

Single foreground process. Wears two hats:

  1. **Sync loop.** Runs the main thread. Calls ``sync.run_once()`` every
     ``SYNC_INTERVAL`` seconds (default 60s). On SIGTERM the loop's
     ``Event.wait()`` returns immediately and we tear down cleanly.

  2. **Static server supervisor.** Spawns ``python -m http.server`` in a
     subprocess, bound to ``0.0.0.0:${SERVER_PORT}`` with cwd set to the
     built site dir. A daemon thread watches the subprocess and respawns
     it if it dies (shouldn't happen under normal load, but the
     supervisor exists so a transient crash doesn't blank the wiki until
     the next manual restart).

Why this shape (not ``mkdocs serve`` + timer in separate units):
  Pterodactyl containers have no systemd, a single allocated port, and a
  single foreground process. ``mkdocs build`` → ``http.server`` is the
  cleanest fit: the server reads files fresh per request, so in-place
  rebuilds of ``site/`` flip content over without a reload/restart.

Upstream:   Pterodactyl egg supplies REPO / BRANCH / TOKEN / SERVER_PORT
            (and optional SYNC_INTERVAL / SITE_NAME / SITE_DESCRIPTION).
Downstream: sync.py (publish pipeline) + http.server subprocess.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Make sibling modules importable when launched as `python entry.py`.
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

# Self-bootstrap the pip --target dir onto sys.path. Belt-and-braces
# with the `PYTHONPATH=` the egg's startup command sets: existing
# Pterodactyl servers keep their original startup string even after
# the egg is re-imported, so we can't rely on an env-var prefix being
# present. Adding it here means a server that boots via the original
# `python3 /home/container/entry.py` startup still finds yaml /
# requests / mkdocs.
_PYDEPS = _HERE / ".pydeps"


def _bootstrap_deps() -> None:
    """
    Ensure ``.pydeps/`` exists and contains our runtime deps.

    Reason this lives in entry.py rather than only the egg's install
    script: Pterodactyl only runs the install script on initial
    provisioning and on explicit Reinstall. An egg update (or a dep
    rev) doesn't re-trigger it. By checking at startup we make the
    container self-heal on first boot after a dep change — no admin
    action needed beyond a Restart.

    Cheap idempotency check: probe for ``yaml`` inside .pydeps. If it's
    already there we skip the pip shell-out (steady-state boot is a
    single directory stat). If it's missing we pip install into the
    target dir and carry on.
    """
    probe = _PYDEPS / "yaml" / "__init__.py"
    if not probe.is_file():
        print(f"[entry] bootstrapping deps into {_PYDEPS} …", flush=True)
        import subprocess as _sp
        _PYDEPS.mkdir(parents=True, exist_ok=True)
        _sp.check_call([
            sys.executable, "-m", "pip", "install",
            "--no-cache-dir",
            "--target", str(_PYDEPS),
            "mkdocs",
            "mkdocs-material",
            "pymdown-extensions",
            "pyyaml",
            "requests",
        ])
    if str(_PYDEPS) not in sys.path:
        sys.path.insert(0, str(_PYDEPS))


_bootstrap_deps()

import sync  # noqa: E402 — after sys.path mutation + bootstrap

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("entry")

_shutdown = threading.Event()


def _on_signal(signum: int, _frame) -> None:
    """
    SIGTERM from Pterodactyl ("Stop" button) or SIGINT (Ctrl-C in a
    tester shell) → flip the shutdown flag. The main loop's
    ``Event.wait()`` returns immediately; the supervisor thread sees the
    flag and stops respawning the HTTP server.
    """
    log.info("Received signal %d — shutting down.", signum)
    _shutdown.set()


# ── HTTP server supervisor ──────────────────────────────────────────────────

def _spawn_http_server(site_dir: Path, port: int) -> subprocess.Popen:
    """
    Launch Python's stdlib http.server bound to ``0.0.0.0:<port>`` with
    ``site_dir`` as its working directory. Stdout/stderr inherit so
    access logs reach Pterodactyl's console.
    """
    log.info("Starting http.server on 0.0.0.0:%d (cwd=%s)", port, site_dir)
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "http.server", str(port), "--bind", "0.0.0.0"],
        cwd=str(site_dir),
    )


def _supervise_http_server(site_dir: Path, port: int) -> None:
    """
    Keep an http.server subprocess alive until shutdown is requested.
    If the child exits non-zero outside shutdown, wait a second to avoid
    a hot restart loop (e.g. port already bound) then respawn.
    """
    proc: subprocess.Popen | None = None
    while not _shutdown.is_set():
        proc = _spawn_http_server(site_dir, port)
        # Inner loop: poll for child death without blocking shutdown. A
        # 1-second wait_timeout keeps us responsive to SIGTERM (we'll exit
        # within ~1s of the flag flipping) without busy-spinning.
        while not _shutdown.is_set():
            try:
                ret = proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                continue
            # Child exited on its own. Back-off before respawn to avoid a
            # hot loop on permanent errors (port already bound, etc.).
            log.warning("http.server exited with code %s", ret)
            if not _shutdown.is_set():
                log.info("Respawning in 1s.")
                time.sleep(1.0)
            break
    # Shutdown path: kill the subprocess if it's still alive.
    if proc is not None and proc.poll() is None:
        log.info("Stopping http.server.")
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Pterodactyl injects SERVER_PORT from the allocation. Default 8000
    # lets someone run entry.py locally for smoke tests without setting
    # env vars by hand.
    port = int(os.environ.get("SERVER_PORT", "8000"))
    interval = int(os.environ.get("SYNC_INTERVAL", "60"))

    cfg = sync.load_sync_config()
    site_dir = Path(cfg["site_dir"])
    site_dir.mkdir(parents=True, exist_ok=True)

    # Initial sync BEFORE starting the server. If the repo fetch fails on
    # first boot we still want the server up (serving a placeholder page)
    # so operators can see the container is reachable and check logs.
    try:
        sync.run_once(cfg)
    except Exception as exc:
        log.exception("Initial sync failed — serving whatever is in %s: %s", site_dir, exc)
        _ensure_placeholder(site_dir, str(exc))

    # Supervisor thread is daemon=False so we can join it on shutdown.
    # _shutdown gates its outer loop, so it exits promptly when signaled.
    supervisor = threading.Thread(
        target=_supervise_http_server,
        args=(site_dir, port),
        name="http-supervisor",
        daemon=False,
    )
    supervisor.start()

    # Main sync loop. Event.wait() is interruptible by _on_signal, so
    # SIGTERM exits within milliseconds rather than waiting out the
    # SYNC_INTERVAL sleep.
    log.info("Sync loop running every %ds. Press Ctrl-C to exit.", interval)
    while not _shutdown.is_set():
        if _shutdown.wait(interval):
            break
        try:
            sync.run_once(cfg)
        except Exception as exc:
            # Log and keep looping — a transient GitHub 502 shouldn't
            # tear the server down; next tick will try again.
            log.exception("Sync tick failed: %s", exc)

    log.info("Main loop exited; waiting for http.server supervisor.")
    supervisor.join(timeout=10.0)
    log.info("Bye.")
    return 0


def _ensure_placeholder(site_dir: Path, reason: str) -> None:
    """
    If the first sync fails we still want the server to have *something*
    to serve so operators see a non-blank page. Only write the placeholder
    if the site dir is empty — never clobber a previous good build
    inherited from a prior container restart.
    """
    if any(site_dir.iterdir()):
        return
    (site_dir / "index.html").write_text(
        "<!doctype html><title>Ranger Aide Memoire</title>"
        "<h1>Ranger Aide Memoire</h1>"
        f"<p>Initial sync failed. Reason: <code>{reason}</code>.</p>"
        "<p>Check container logs; the next tick will retry.</p>",
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
