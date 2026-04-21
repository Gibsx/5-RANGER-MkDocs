# _reference-theme-features.md

MkDocs Material theme features enabled in `_MKDOCS_CONFIG_BASE`, with rationale.

## Enabled features

- **`navigation.expand`** — all sidebar groups open by default.
  Rationale: the aide memoire isn't deep; soldiers shouldn't have to click to see what's available.
- **`navigation.indexes`** — section-level index pages. Lets a `group:` header link to an overview.
- **`navigation.top`** — "back to top" button on scroll.
- **`search.suggest`** / **`search.highlight`** — sensible defaults from Material.
- **`content.code.copy`** — copy button on code blocks (markdown callouts and snippets).

## Plugins

```yaml
plugins:
  - search
  - tags:
      tags_file: tags.md
```

- **`search`** — built-in, lunr-backed, runs client-side. Adequate for ~20 sections.
- **`tags`** — renders per-page tag chips from frontmatter and a browsable `/tags/` index page.

## Palette

Default Material palette (no override currently). To customise:

```yaml
theme:
  palette:
    - scheme: default
      primary: green     # 5 RANGER branding candidate
      accent: lime
```

Editing this belongs in `_process-change-theme.md`.

## What's intentionally *not* enabled

- **`navigation.tabs`** — would replicate the top-level group header as a tab row. Redundant with `navigation.expand`.
- **`navigation.sections`** — non-collapsible section groupings. We want groups collapsible.
- **`content.action.edit`** — GitHub "edit this page" links. Not wired because the content repo is private.
- **`--strict` build mode** — see `_reference-mkdocs-yml-generation.md` for why.

## Mermaid

Mermaid diagrams are supported via Material's built-in `pymdownx.superfences` + `mermaid` custom fence. Not currently enabled in config. Add with:

```yaml
markdown_extensions:
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
```

## Related

- `_reference-mkdocs-yml-generation.md` — the full rendered config shape
- `_process-change-theme.md` — how to tweak safely
