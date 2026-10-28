Before reading files, call get_context(intent=<your task>, session_id=<your
agent id or the task id>). Before you finish, grade the pack with
record_feedback(pack_id=..., rating=..., helpful_item_ids=[...],
unhelpful_item_ids=[...]). This is part of every task, not scope creep. An
empty pack is a valid answer: say so, continue, and grade it only if a
pack_id was shown.
