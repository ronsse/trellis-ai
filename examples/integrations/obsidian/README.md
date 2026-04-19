# Trellis — Obsidian Reference Template

> **Status: preview — reference template, not a published package.** Copy [`vault.py`](vault.py) and [`indexer.py`](indexer.py) into your own project rather than importing from this repo. The SDK and stores are being reshaped in parallel; expect signatures to shift before the next minor release.

Index an Obsidian vault into Trellis so notes become first-class evidence in retrieval. Wiki-links become graph edges, frontmatter tags carry through to retrieval filters, and content lands in the document store for keyword + semantic search.

## What this gives you

- Every note appears in `trellis retrieve search` results.
- Wiki-link graph (`[[other-note]]`) becomes typed `wiki_link` edges in the GraphStore.
- Re-indexing is content-hash aware — unchanged notes are skipped.
- Notes are usable from any agent: MCP tool calls, the SDK, the REST API.

## Prerequisites

- Python 3.11+
- `trellis-ai` installed (`pip install trellis-ai` or `pip install -e ".[dev]"`)
- An Obsidian vault on disk
- Stores initialized (`trellis admin init`)

## Quick start

```python
from trellis.stores.registry import StoreRegistry

# After copying vault.py + indexer.py into your own package:
from myproject.obsidian.vault import ObsidianVault
from myproject.obsidian.indexer import VaultIndexer

registry = StoreRegistry.from_config_dir()

vault = ObsidianVault("/path/to/your/Vault")
indexer = VaultIndexer(
    vault,
    document_store=registry.document_store,
    graph_store=registry.graph_store,
)

summary = indexer.index_vault()
print(f"indexed={summary.indexed} updated={summary.updated} unchanged={summary.unchanged}")
```

After indexing, the notes are searchable through every Trellis interface:

```bash
trellis retrieve search "rate limiting" --format json
```

## Re-indexing

`VaultIndexer` keeps an in-memory hash cache, so calling `index_vault()` twice in the same process is cheap. Across runs, the document-store `content_hash` metadata is the source of truth — pass `force=True` to rebuild from scratch:

```python
indexer.index_vault(force=True)
```

## Single-note workflow

```python
result = indexer.index_note("Daily Notes/2026-04-17.md")
print(result.action)   # "created" | "updated" | "unchanged" | "error"
```

## How notes are stored

| Field | Where it lands |
|---|---|
| `note.content` | DocumentStore (`doc_id="obsidian:<note_id>"`) |
| `note.title`, `note.path`, `note.tags` | DocumentStore metadata + GraphStore node properties |
| Wiki-links `[[Other Note]]` | GraphStore edges (`edge_type="wiki_link"`) |
| Frontmatter | parsed via `_parse_frontmatter`; available via `ObsidianVault.read_note` |

## Use as a document source for retrieval

Once indexed, notes participate in every `assemble_pack` / `get_context` call:

```python
from trellis_sdk import TrellisClient

client = TrellisClient()
pack = client.assemble_pack(
    intent="how does our team handle on-call rotations",
    max_tokens=2000,
)
for item in pack["items"]:
    print(item["item_id"], item["content"][:100])
```

## Limitations

- Only `.md` files are indexed (Obsidian canvas, attachments, and audio are skipped).
- Wiki-link target ids use the literal link text — they don't currently resolve aliases.
- This integration ships the indexer as a library; there's no daemon / file-watcher yet. Re-run `index_vault()` on whatever cadence fits (cron, file-watch, post-commit hook).

## See also

- [docs/agent-guide/operations.md](../../../docs/agent-guide/operations.md) — full retrieval reference.
- [docs/agent-guide/schemas.md](../../../docs/agent-guide/schemas.md) — DocumentStore + GraphStore shapes.
