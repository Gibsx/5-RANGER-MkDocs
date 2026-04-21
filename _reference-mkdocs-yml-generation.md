# _reference-mkdocs-yml-generation.md

How `sync.py` renders `mkdocs.yml` from `manifest.yaml`. The manifest is the single source of truth — never hand-edit the generated file.

## Pipeline

```
manifest.yaml (content repo)
     │
     ├─ parse YAML → list of section dicts
     │    each: {file, slug, title, group?, tags?, banner?}
     │
     ├─ for each section file:
     │    read .md, inject tags: frontmatter at top (strip any pre-existing
     │    frontmatter for idempotency)
     │
     ├─ render_mkdocs_yml(sections) →
     │    nav: grouped by `group:` field, first-occurrence order
     │    sections without group sit at top level
     │    plus tags.md entry for the Material tags plugin
     │
     └─ write mkdocs.yml; write tags.md with [TAGS] macro
```

## Nav shape

MkDocs Material uses `nav:` as a list-of-dicts. Groups become collapsible sidebar headers:

```yaml
nav:
  - Home: index.md
  - Fieldcraft:
    - 01-communications.md
    - 02-movement.md
  - Combat:
    - 03-contact-drills.md
    - 04-assault-drills.md
  - Tags: tags.md
```

Groups bucket **in first-occurrence order** — whichever group a section lists first sets that group's position. Sections without `group:` sit at the top level (above or below the groups depending on manifest order).

## Tags

Two hooks:

1. **Per-page frontmatter** — `inject_tags_frontmatter(md, tags)` rewrites the file with `---\ntags:\n  - foo\n  - bar\n---\n` at top. Pre-existing frontmatter is stripped first so re-runs don't stack.
2. **Tag index** — `ensure_tags_index()` writes `tags.md` with a `[TAGS]` macro. The Material tags plugin (`plugins: [{tags: {tags_file: tags.md}}]`) renders both the chips per page and the browsable index.

## Theme config

`_MKDOCS_CONFIG_BASE` template includes:

```yaml
theme:
  name: material
  features:
    - navigation.expand
    - navigation.indexes
    # ... palette, search, etc.
plugins:
  - search
  - tags:
      tags_file: tags.md
```

## MkDocs invocation

Always `python -m mkdocs build` (not `mkdocs`). `pip --target` doesn't create CLI shims, so the console script isn't on `PATH`.

**No `--strict`.** MkDocs Material keeps deprecating config keys; a deprecation warning with `--strict` aborts the build and blanks the wiki. Warnings in the log are preferable to a dead site.

## Pretty URLs gotcha

`use_directory_urls: true` produces pretty URLs (`communications/`) but the internal link validator still expects **source** paths. When generating cross-section links from the manifest, point at `entry['file']` (the `.md`), not `<stem>/`.

## Idempotency

- Frontmatter injection strips pre-existing frontmatter, so running `sync.run_once()` twice on the same content produces identical files.
- `mkdocs.yml` is rewritten from scratch each run; not mutated in place.
- Only the source files (`*.md`, `mkdocs.yml`, `tags.md`) are touched — the `site/` output is rebuilt by `mkdocs build` and served read-fresh-per-request by `http.server`.

## Related

- `_reference-theme-features.md` — enabled Material features
- `_flow-poll-and-rebuild.md` — what triggers a rebuild
- `_process-debug-sync.md` — diagnosing a broken build
