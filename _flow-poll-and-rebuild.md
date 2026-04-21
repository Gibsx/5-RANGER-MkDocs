# _flow-poll-and-rebuild.md

One tick of the sync loop.

## Sequence

```
main loop (in entry.py):
  stop_event.wait(SYNC_INTERVAL)  # default 60 s; break on SIGTERM
  sync.run_once()

sync.run_once():
│
├─ state = load_state(state.json)  # { "sha": "abc..." } or {}
│
├─ resp = GET https://api.github.com/repos/<REPO>/branches/<BRANCH>
│    headers: Authorization: token <TOKEN>
│    → new_sha = resp["commit"]["sha"]
│
├─ if new_sha == state.get("sha"):
│    log "Poll: SHA unchanged"
│    return  # no-op
│
├─ log "Poll: new SHA detected (old: ..., new: ...)"
│
├─ download /tarball/<new_sha> → /home/container/content-tmp/
│    extract → /home/container/content/  (atomic rename)
│
├─ manifest = yaml.safe_load(content/manifest.yaml)
│
├─ for each section:
│    path = content/<section.file>
│    md   = read path
│    md   = inject_tags_frontmatter(md, section.tags)  # idempotent
│    write md back to path
│
├─ ensure_tags_index(content/, all_tags)
│    write content/tags.md with [TAGS] macro
│
├─ mkdocs_yml = render_mkdocs_yml(sections, site_name, site_description)
│    write content/mkdocs.yml
│
├─ run subprocess: python -m mkdocs build
│    cwd = /home/container/content
│    output → /home/container/site/   (destination rebuilt in place)
│
├─ save_state({"sha": new_sha})
│
└─ log "Rebuild complete"

--- in parallel ---

http.server subprocess:
  reads /home/container/site/ per request (no caching)
  → next request after rebuild serves fresh content automatically
  → no restart needed
```

## Why in-place rebuild is safe

`mkdocs build` writes to `site/` via a destination-clean-then-write pattern. `http.server` reads each file per request. A partial `site/` during rebuild could in theory serve half-old/half-new, but:

- `mkdocs build` completes in seconds.
- Concurrent request arriving mid-build may serve a transiently inconsistent page; next refresh is correct.
- No restart, no downtime, no user-visible error page.

If a future MkDocs update makes partial builds worse, switch to a `site-tmp/` + atomic rename pattern.

## Idempotency

- `inject_tags_frontmatter` strips pre-existing frontmatter before injecting, so re-runs produce identical files.
- `mkdocs.yml` is regenerated from scratch each tick; no accumulated mutations.
- `state.json` update is the last step — a crash mid-build doesn't mark SHA as processed, so the next tick retries.

## Failure behaviour

- GitHub 5xx or timeout → exception caught, logged, loop continues. Site stays on last good build.
- Tarball extraction failure → same.
- `mkdocs build` non-zero exit → logged with stderr, `state.json` **not** updated so next tick retries. Site stays on last good build.

## Observability

`grep Poll: /var/log/...` (or the panel console) shows tick cadence. `Rebuild complete` lines mark successful builds. `mkdocs build` stderr is surfaced to the panel console — deprecation warnings included (not errors).

## Related

- `_reference-mkdocs-yml-generation.md` — what `render_mkdocs_yml` produces
- `_flow-egg-install.md` — the first-run version of this flow
- `_process-debug-sync.md` — when this flow is misbehaving
