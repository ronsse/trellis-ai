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

Rules:
- Focus on ENTITIES mentioned — people, systems, datasets, projects —
  not actions or events.
- Use short display names; prefer proper nouns as-written in the text.
- Leave entity_id as null; the downstream resolver assigns or matches
  existing entities.
- Do not produce edges in this mode; the caller wires mentions via a
  separate mechanism.
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
    version="1.0",
    system=_MEMORY_EXTRACTION_SYSTEM,
    user_template=_MEMORY_EXTRACTION_USER,
)
