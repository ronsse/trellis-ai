# Pack Observability Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan in this session.

**Goal:** Extend `trellis-ai` packs so retrieval decisions are observable enough to support future curator learning and agent-specific context behavior.

**Architecture:** Keep the first slice additive. Enrich `Pack` and `PackItem` with fields that explain why an item was selected and how it fit into the budget, then have `PackBuilder` populate those fields deterministically. Avoid store migrations or new persistence objects in this slice.

**Tech Stack:** Python, Pydantic, pytest

### Task 1: Add schema tests for richer pack fields

**Files:**
- Modify: `tests/unit/schemas/test_pack.py`
- Test: `tests/unit/schemas/test_pack.py`

**Step 1: Write the failing test**

Add tests asserting:
- `PackItem` accepts `included`, `rank`, `selection_reason`, `score_breakdown`, and `estimated_tokens`
- `Pack` accepts `skill_id` and `target_entity_ids`

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/schemas/test_pack.py -q`
Expected: FAIL because the new fields are not yet present in the schemas.

**Step 3: Write minimal implementation**

Update `src/trellis/schemas/pack.py` to add only those additive fields with safe defaults.

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/schemas/test_pack.py -q`
Expected: PASS

### Task 2: Add builder tests for deterministic observability fields

**Files:**
- Modify: `tests/unit/retrieve/test_pack_builder.py`
- Test: `tests/unit/retrieve/test_pack_builder.py`

**Step 1: Write the failing test**

Add tests asserting that `PackBuilder.build()`:
- assigns descending `rank` to included items
- computes `estimated_tokens` from each item excerpt
- marks included items with `included=True`
- sets a default `selection_reason`
- emits `score_breakdown` containing the final relevance score

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/retrieve/test_pack_builder.py -q`
Expected: FAIL because builder output does not yet populate the new fields.

**Step 3: Write minimal implementation**

Update `src/trellis/retrieve/pack_builder.py` to annotate selected items after sorting and budgeting.

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/retrieve/test_pack_builder.py -q`
Expected: PASS

### Task 3: Update schema exports and docs

**Files:**
- Modify: `docs/agent-guide/schemas.md`
- Modify: `src/trellis/schemas/__init__.py`

**Step 1: Write the failing check**

Review the schema reference and exported names to confirm the new fields are undocumented.

**Step 2: Write minimal implementation**

Document the added pack fields in `docs/agent-guide/schemas.md`. Update exports only if needed.

**Step 3: Run targeted verification**

Run: `python -m pytest tests/unit/schemas/test_pack.py tests/unit/retrieve/test_pack_builder.py -q`
Expected: PASS

### Task 4: Verify the slice stays additive

**Files:**
- Modify: `src/trellis/retrieve/pack_builder.py`
- Modify: `src/trellis/schemas/pack.py`

**Step 1: Run focused verification**

Run: `python -m pytest tests/unit/schemas/test_pack.py tests/unit/retrieve/test_pack_builder.py -q`
Expected: PASS

**Step 2: Run a broader retrieval smoke check**

Run: `python -m pytest tests/unit/retrieve/test_effectiveness.py tests/unit/retrieve/test_pack_builder.py tests/unit/schemas/test_pack.py -q`
Expected: PASS

**Step 3: Commit**

```bash
git add docs/plans/2026-03-16-pack-observability-implementation.md \
  docs/agent-guide/schemas.md \
  src/trellis/schemas/pack.py \
  src/trellis/retrieve/pack_builder.py \
  tests/unit/schemas/test_pack.py \
  tests/unit/retrieve/test_pack_builder.py
git commit -m "feat: add pack observability fields"
```
