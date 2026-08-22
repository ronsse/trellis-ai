"""Prompt templates used by the LLM-tier extractors."""

from __future__ import annotations

from trellis.extract.prompts.base import PromptTemplate

_ENTITY_EXTRACTION_SYSTEM = """\
You are an entity and relationship extractor. Given unstructured text,
identify entities (people, systems, concepts, artifacts) and the
relationships between them.

Output ONLY valid JSON — no markdown fences, no commentary. If no
entities are found, return {"entities": [], "edges": []}.

Schema:
{
  "entities": [
    {
      "entity_id": "<stable-slug-or-null>",
      "entity_type": "<type>",
      "name": "<display name>",
      "properties": {},
      "confidence": <number between 0.0 and 1.0>
    }
  ],
  "edges": [
    {
      "source_id": "<entity_id or name from entities above>",
      "target_id": "<entity_id or name from entities above>",
      "edge_kind": "<kind>",
      "confidence": <number between 0.0 and 1.0>
    }
  ]
}

Rules:
- Prefer a stable slug for entity_id when you can infer one (e.g. a
  canonical name); otherwise use null and the downstream resolver
  will assign an ID.
- Every edge's source_id / target_id must reference an entity you
  produced in the same response (by entity_id or name).
- confidence: 0.9+ for unambiguous explicit mentions, 0.5-0.8 for
  inferred from context, 0.5 default.
"""

_ENTITY_EXTRACTION_USER = """\
{domain_line}
{source_line}
{type_hints}
{edge_hints}

Text:
{text}
"""


ENTITY_EXTRACTION_V1 = PromptTemplate(
    name="entity_extraction",
    version="1.0",
    system=_ENTITY_EXTRACTION_SYSTEM,
    user_template=_ENTITY_EXTRACTION_USER,
)


_MEMORY_EXTRACTION_SYSTEM = """\
You extract entity mentions from short natural-language memories (1-3
sentences, notes, observations). Your job: identify which entities
this memory references, so the memory can be linked to them in a
knowledge graph.

Output ONLY valid JSON — no markdown fences, no commentary. If no
entities are mentioned, return {"entities": [], "edges": []}.

Schema:
{
  "entities": [
    {
      "entity_id": null,
      "entity_type": "<type>",
      "name": "<display name>",
      "properties": {},
      "confidence": <number between 0.0 and 1.0>
    }
  ],
  "edges": []
}

Skip discipline — most operational noise references nothing worth
linking. Extract NOTHING (return {"entities": [], "edges": []}) when
the memory records only:
- a status check that found nothing notable;
- a dependency install or build that completed cleanly;
- a bare file or directory listing;
- a restatement of a finding the text says is already recorded;
- research or a search that found nothing.
If skipping, return {"entities": [], "edges": []} and nothing else —
never explain the skip in prose. Output that is not the JSON schema is
discarded, so a prose explanation is a wasted response, not a record.

Rules:
- Extract what the memory says was learned, built, or fixed — NEVER
  what you or the recording process are doing. "This analysis", "this
  extraction run", "this session" are not entities; "Analyzed the text
  and stored findings" is not a finding. A memory whose SUBJECT is an
  extraction or capture pipeline is ordinary subject matter — extract
  it normally.
- Focus on ENTITIES mentioned — people, systems, datasets, projects —
  not actions or events.
- Use short display names; prefer proper nouns as-written in the text.
- Leave entity_id as null; the downstream resolver assigns or matches
  existing entities.
- Do not produce edges in this mode; the caller wires mentions via a
  separate mechanism.
- NEVER extract the text's speakers or participants as entities. In a
  conversation, the turn labels (e.g. "You", "Claude", the author's
  name) are the frame of the document, not its subject matter — extract
  only what the text is ABOUT.
- You are recording MENTIONS, nothing stronger. A text that evaluates,
  compares, or considers something mentions it; that is not evidence
  the author owns it, uses it, or chose it. Confidence scores how
  clearly the entity is named, never how the author relates to it.
- confidence: 0.9 for explicit named mentions, 0.6 for implied ones.
"""

_MEMORY_EXTRACTION_USER = """\
{domain_line}
{type_hints}

Memory:
{text}
"""


MEMORY_EXTRACTION_V1 = PromptTemplate(
    name="memory_extraction",
    version="1.2",
    system=_MEMORY_EXTRACTION_SYSTEM,
    user_template=_MEMORY_EXTRACTION_USER,
)
