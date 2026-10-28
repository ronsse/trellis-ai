## Trellis memory

Trellis memory tools are connected. Use them as a loop, not a lookup.

- Before non-trivial work, call get_context(intent=<what you are about to
  do and why>, session_id=<a stable id for this session>). Keep the pack_id
  from the pack header and the item ids in backticks. "No context found" is
  a real answer: proceed.
- After meaningful work, call save_experience with a trace. A step's
  `result` is a JSON object, never a bare string; a failure goes in the
  step's `error`.
- Grade the pack: record_feedback(pack_id=..., rating=0.0-1.0,
  helpful_item_ids=[...], unhelpful_item_ids=[...]). Feedback that names no
  item ids does not reach the learning loop. Skip it only if no pack_id was
  shown.
- This applies to every dispatched subagent. Retrieval and grading are part
  of the task, not extra scope.
