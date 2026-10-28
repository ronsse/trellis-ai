You have Trellis memory tools. Use them as a loop, not a lookup.

1. Before any non-trivial task, call get_context(intent=<one sentence: what
   you are about to do and why>, session_id=<a stable id for this
   conversation>, domain=<area, if you know it>). Read the pack before you
   read anything else. Its header carries the pack_id on a "**pack_id:**"
   line, and each item is headed "## [type] `item_id`": keep both. If the
   response says "No context found", that is a real answer: proceed, and
   skip step 3 unless a pack_id was shown.

2. After the work, call save_experience(trace_json=<a JSON string>) with a
   trace: source "agent", intent, context {domain}, 2-6 steps (each has a
   step_type and a name; a step's `result` is a JSON object, never a bare
   string, and a failure goes in the step's `error`), and an outcome
   {status: success|failure|partial, summary}. Record what the next agent
   would otherwise have to rediscover: the workaround, the wrong
   assumption, the command that worked.

3. Grade the pack you were served: record_feedback(pack_id=<from step 1>,
   rating=<0.0-1.0>, helpful_item_ids=[ids you used],
   unhelpful_item_ids=[ids that cost you time or were wrong]). Feedback
   that names no item ids does not reach the learning loop. If the pack
   listed advisories and you acted on one, add followed_advisory_ids=[...].
   An honest low rating is worth more than a polite high one.

4. When you learn a durable fact about a named thing (a service, a host, a
   dataset), call save_knowledge(name=..., content=<the fact and where it
   lives>, properties={"domain": ...}). It always creates a new node, so
   call search(query=<name>) first if the node may already exist.

Retrieval that never happens is the common failure, not retrieval that
returns nothing.
