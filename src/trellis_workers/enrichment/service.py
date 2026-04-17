"""Enrichment worker — auto-tags, classification, importance scoring."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.llm import LLMClient, LLMResponse, Message, TokenUsage

logger = structlog.get_logger(__name__)

DEFAULT_CLASSIFICATIONS = [
    "meeting",
    "architecture",
    "reference",
    "journal",
    "project",
    "brainstorm",
    "documentation",
    "task-list",
    "research",
    "notes",
]


class EnrichmentResult(TrellisModel):
    """Result of LLM enrichment."""

    auto_tags: list[str] = Field(default_factory=list)
    auto_class: str | None = None
    auto_summary: str | None = None
    auto_importance: float = 0.0
    tag_confidence: float = 0.0
    class_confidence: float = 0.0
    raw_response: str | None = None
    usage: TokenUsage | None = None
    success: bool = True
    error: str | None = None


ENRICHMENT_SYSTEM_PROMPT = """\
You are an expert at analyzing content to suggest metadata.

Your task is to analyze the content and suggest:
1. **Tags**: 2-5 relevant topic tags (lowercase, hyphenated)
2. **Classification**: A single category that best describes the content type
3. **Summary**: A concise 1-2 sentence summary
4. **Importance**: A score from 0.0 to 1.0 indicating how foundational or critical \
this content is

Available classifications: {classifications}

IMPORTANT RULES:
- Tags should be general topics, not specific to the content
- Classification must be from the provided list
- Summary should be semantic-rich: include key concepts, entities, and relationships
- Importance reflects how foundational the content is (not recency):
  - 0.9-1.0: Core architecture decisions, key project specs
  - 0.6-0.8: Active project notes, important discussions
  - 0.3-0.5: General notes, routine updates
  - 0.0-0.2: Ephemeral content, scratch notes

Respond in JSON format:
{{
  "tags": ["tag1", "tag2"],
  "class": "classification",
  "summary": "Concise summary.",
  "importance": 0.5,
  "tag_confidence": 0.85,
  "class_confidence": 0.90
}}
"""


ENRICHMENT_USER_PROMPT = """Analyze this content and suggest metadata:

---
Title: {title}
{existing_tags_section}
---

{content}

---

Respond with JSON only, no markdown formatting.{summary_instruction}
"""


class EnrichmentService:
    """Service for enriching content with LLM-generated metadata."""

    def __init__(
        self,
        llm: LLMClient,
        classifications: list[str] | None = None,
        max_content_length: int = 4000,
        temperature: float = 0.3,
        model: str | None = None,
    ) -> None:
        self._llm = llm
        self.classifications = classifications or list(DEFAULT_CLASSIFICATIONS)
        self.max_content_length = max_content_length
        self.temperature = temperature
        self.model = model

    async def enrich(
        self,
        content: str,
        title: str = "",
        existing_tags: list[str] | None = None,
        include_summary: bool = True,
    ) -> EnrichmentResult:
        """Enrich content with LLM-generated metadata."""
        try:
            truncated = content[: self.max_content_length]
            if len(content) > self.max_content_length:
                truncated += "\n\n[Content truncated...]"

            system_prompt = ENRICHMENT_SYSTEM_PROMPT.format(
                classifications=", ".join(self.classifications),
            )

            tags_section = ""
            if existing_tags:
                tags_section = f"Existing tags: {', '.join(existing_tags)}"

            summary_instruction = (
                " Summary is REQUIRED."
                if include_summary
                else " Do NOT include a summary."
            )

            user_prompt = ENRICHMENT_USER_PROMPT.format(
                title=title or "Untitled",
                existing_tags_section=tags_section,
                content=truncated,
                summary_instruction=summary_instruction,
            )

            response: LLMResponse = await self._llm.generate(
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ],
                temperature=self.temperature,
                max_tokens=500,
                model=self.model,
            )
        except Exception as e:
            logger.exception("enrichment_failed", title=title)
            return EnrichmentResult(success=False, error=str(e))
        else:
            result = self._parse_response(response.content)
            result.raw_response = response.content
            result.usage = response.usage
            return result

    async def batch_enrich(
        self,
        items: list[dict[str, Any]],
        concurrency: int = 3,
        include_summary: bool = True,
    ) -> list[EnrichmentResult]:
        """Enrich multiple items in parallel with semaphore."""
        semaphore = asyncio.Semaphore(concurrency)

        async def _with_sem(item: dict[str, Any]) -> EnrichmentResult:
            async with semaphore:
                return await self.enrich(
                    content=item.get("content", ""),
                    title=item.get("title", ""),
                    existing_tags=item.get("tags", []),
                    include_summary=item.get("include_summary", include_summary),
                )

        results = await asyncio.gather(
            *[_with_sem(item) for item in items],
            return_exceptions=True,
        )

        return [
            r
            if isinstance(r, EnrichmentResult)
            else EnrichmentResult(success=False, error=str(r))
            for r in results
        ]

    def _parse_response(self, response: str) -> EnrichmentResult:
        """Parse LLM JSON response."""
        text = response.strip()

        # Strip markdown code fences
        if text.startswith("```"):
            first_nl = text.find("\n")
            if first_nl > 0:
                text = text[first_nl + 1 :]
            text = text.removesuffix("```").strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return EnrichmentResult(
                        success=False,
                        error=f"Invalid JSON: {e}",
                        raw_response=response,
                    )
            else:
                return EnrichmentResult(
                    success=False,
                    error=f"No JSON found: {e}",
                    raw_response=response,
                )

        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [normalize_tag(t) for t in tags if isinstance(t, str)]

        auto_class = data.get("class")
        if auto_class and auto_class not in self.classifications:
            auto_class = None

        summary = data.get("summary")
        if summary in {"null", ""}:
            summary = None

        importance = float(data.get("importance", 0.0))
        tag_conf = float(data.get("tag_confidence", 0.8))
        class_conf = float(data.get("class_confidence", 0.8))

        return EnrichmentResult(
            auto_tags=tags,
            auto_class=auto_class,
            auto_summary=summary,
            auto_importance=min(max(importance, 0.0), 1.0),
            tag_confidence=min(max(tag_conf, 0.0), 1.0),
            class_confidence=min(max(class_conf, 0.0), 1.0),
        )


def normalize_tag(tag: str) -> str:
    """Normalize a tag to lowercase hyphenated format."""
    tag = tag.lower().strip()
    tag = re.sub(r"[\s_]+", "-", tag)
    tag = re.sub(r"[^a-z0-9\-/]", "", tag)
    tag = re.sub(r"-+", "-", tag)
    return tag.strip("-")
