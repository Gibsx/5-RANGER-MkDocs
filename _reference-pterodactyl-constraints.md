# _reference-pterodactyl-constraints.md

The container constraints that shape every change. Violate them and the container won't start on Pterodactyl.

## Python

- **Python 3.8 stdlib + syntax.** No walrus-heavy code, no `match`, no stdlib APIs newer than 3.8.
- `from __future__ import annotations` in every module for 3.9+ typing compat.
- No GPU, no heavy wheels. Stick to pure-Python: `pyyaml`, `requests`, `mkdocs`, `mkdocs-material`. The installer container has no compile toolchain.

## Filesystem

- **Only `/mnt/server` (mapped to `/home/container` at runtime) persists.** System site-packages in the installer image do **not** carry over to the runtime image.
- That's why `pip install --target=/home/container/.pydeps` and `entry.py` bootstraps `.pydeps/` onto `sys.path` at startup.
- `/tmp` is a tiny tmpfs (~64 MB). Pip wheel builds overflow it. `_bootstrap_deps()` sets `TMPDIR=/home/container/.piptmp` before pip.
- Bootstrap failure: `.pydeps/` is `rmtree`'d so the next boot retries cleanly.

## Networking

- **One allocated port**, bind `0.0.0.0`.
- `SERVER_IP` is a host-side allocation address, **not** bindable inside the container's network namespace — binding it gives `EADDRNOTAVAIL`.
- Log the allocation (`Allocation: http://$SERVER_IP:$SERVER_PORT/`) for operator clarity but bind `0.0.0.0`.

## Process model

- **Single foreground process.** No systemd. `entry.py` owns the process tree.
- Sync loop on the main thread; `python -m http.server` is a supervised subprocess with a respawn watcher.
- SIGTERM → break the loop's `Event.wait()` → terminate the subprocess → exit cleanly.

## No git binary

The Pterodactyl image has no `git`. Fetches are GitHub API **tarball** downloads (`/tarball/{ref}`). `requests` + `tarfile` stdlib are enough.

## Egg

- `egg-ranger-aide-memoire.json` is tracked locally but **gitignored**.
- Eggs are environment-specific (docker tag, UID). Regenerate from a known-good panel export rather than version-controlling.

## Related

- `_reference-egg-variables.md` — every variable the egg exposes
- `_flow-egg-install.md` — what the install script does
