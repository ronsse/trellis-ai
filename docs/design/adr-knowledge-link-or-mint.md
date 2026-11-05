# ADR: Knowledge link-or-mint and extractor alias binding

**Status:** Accepted, behind `TRELLIS_KNOWLEDGE_LINK_OR_MINT` (default off)
**Related:** [`adr-alias-resolution.md`](adr-alias-resolution.md), [`adr-graph-ontology.md`](adr-graph-ontology.md)

## 1. Context

`save_knowledge(properties={"domain": "finance"})` links `appliesTo` to an existing `domain:finance`
node and says so when there is none; only trace extraction mints one. `relates_to` resolves ids,
slug forms and `name` aliases but not a raw id under an external system, because no extractor bound
one: `EntityDraft` had no alias field and `result_to_batch` emitted only creates and links.

## 2. Decision

One write-behaviour knob, `knowledge_link_or_mint`, off. Off is byte-identical to before.

**Mint rules.** The slug is `normalize_slug` (NFKC, case-folded, alphanumeric runs joined by `-`),
the id `domain:<slug>`, the type `Concept`, the name the caller's own string: the id, type and slug
trace extraction uses, so a later trace for the same domain updates the node instead of forking it.
A value with no slug mints nothing. A node that already has the id is never minted over. Two
spellings in one call mint once. The mint is an `ENTITY_CREATE` through the executor, so the policy
gate runs; a deny policy scoped to entity type `Concept` blocks it and the note is still written.

**Alias precedence.** `resolve_referent` first (exact id, slug forms, `name` alias), then the
bounded external set (`dbt`, `openlineage_dataset`, `openlineage_job`, `unity_catalog`), then a
`<system>:<raw>` prefix. One match links; two are ambiguous and link nothing. The name alias is
bound by `ENTITY_CREATE` as before, bind-if-absent and skipped when contested.

**Never rebind.** Every alias written here is `alias.upsert` with `if_absent=True` and no
`stale_owner_name_key`, the one argument that lets the store take an alias over. A second claim on
a bound `(source_system, raw_id)` is a no-op. `require_entity=True` refuses an alias for an entity
whose own create did not land.

**Attach.** A note whose normalised name resolves to a current, semantic, non-namespaced node of the
same type is not duplicated. An attach writes **no caller properties**: a caller who only names a note
must not be able to archive it (`lifecycle`), noise-tag it (`content_tags`, `content_tags_shadow`) or
change what it is (`node_role`), and copying "whatever the node lacks" allowed all of those. The only
*property* write is one `entity.update`: the new evidence document id appended to `document_ids`, plus
`evidence_ref` when the node has none. No node is created and no alias is bound for the note itself;
the caller's `domain` and `relates_to` links still run, so an attach can mint a `domain:` node and add
`appliesTo` / `relates_to` edges from the existing note. `supersedes=` always creates, because it
names its target.

**Unresolved.** One reply line, `Unresolved: N of M references (positions ...)`, with no caller text.

**Extractors.** Drafts carry `AliasDraft`s and stay pure; `result_to_batch(bind_identity=True)`
turns them into alias commands and passes the result's tier on every edge as `extractor_tier`
(upper-case stored spelling). A three-part Unity Catalog id binds under `unity_catalog` with no
special case beyond one key function, `normalize_alias_raw_id`: Unity Catalog identifiers are
case-insensitive and may be backtick-quoted per part, so the key is lower-cased and unquoted, on the
bind side (`EntityRule`, and `alias.upsert` itself for any command under `unity_catalog`) and on the
`relates_to` lookup alike. Space around a dot is not part of a name. A value with an empty part
(`.a.b`, `a..b`, an empty quoted part) is malformed and is returned as given, never rewritten: the
alternative, collapsing an empty quoted part, would give `` ``.a.b `` the key of the verbatim `.a.b`.
`UnityCatalogExtractor` builds its ids through the same function, so the id it mints, the alias it
binds and the key `save_knowledge` probes agree. Every other system's ids stay verbatim.
`AliasDraft`s stay out of the validator-rejection residue, so a knob-off ingest records what it did
before aliases existed.

**Domain mint.** The value is split with `referents.split_namespace`, the rule
`namespace_candidates` uses: a `domain:` prefix is stripped before the slug is taken, and any other
known namespace prefix mints nothing. `ENTITY_CREATE` on an existing id replaces its properties, so
the mint re-reads the id immediately before writing, is an `if_absent` create (an existing node is left
alone and is linked to, not reported as minted) and carries an idempotency key derived from the id. The
key is recorded only after a success, so it protects a retry or a serial save; `if_absent` guards a
writer that holds no key, such as a trace. It is a read before a write in the handler, so the window
is narrowed to that gap, not closed: two truly concurrent saves inside it can both write the same
`minted_by` and possibly another spelling of the name.

## 3. Why the default stays off

Seeding reads only `name` aliases and `{namespace}:{key}` ids. External-system aliases do not change
it; a minted `domain:<slug>` node does, because an intent naming the domain now seeds the graph
axis. Serving is frozen for a holdout, so the knob ships dark and is turned on only after the
readout. Turning it on is a settings change, not a deploy.

## 4. Not done

- Trace aliasing: no trace entity has an external identity its node id does not already encode.
- Canonical edge kinds for dbt `depends_on` and OpenLineage `reads_from` / `writes_to`: renaming them
  would fork lineage history.
- A Unity Catalog extractor. The shape is proven by a test, not built.
- Aliases on the REST wire `EntityDraft`, and on the memory and trace ingest hooks.
