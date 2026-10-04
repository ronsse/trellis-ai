"""Run a rival command inside a handler's read, without threads or sleeps.

A handler that rewrites a graph node reads it first and writes afterwards.
:func:`rival_inside_first_read` wraps the registry's ``get_node`` so the
first read of one id fetches the row, then executes a rival command through
the real executor, and only then hands the row back. The handler is left
holding a version that a committed write has already replaced or purged:
the window between its read and its write, made deterministic.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from trellis.mutate.commands import Command, CommandResult, Operation

if TYPE_CHECKING:
    from collections.abc import Iterator

    from trellis.mutate.executor import MutationExecutor
    from trellis.stores.registry import StoreRegistry


def redaction(node_id: str) -> Command:
    """A ``redaction.apply`` on ``node_id``: a hard purge of every version."""
    return Command(
        operation=Operation.REDACTION_APPLY,
        args={"target_id": node_id, "reason": "purge mid-command"},
    )


@contextmanager
def rival_inside_first_read(
    registry: StoreRegistry,
    executor: MutationExecutor,
    node_id: str,
    rival: Command,
) -> Iterator[list[CommandResult]]:
    """Execute ``rival`` between the first ``get_node(node_id)`` and its return.

    Yields a list that holds the rival's result once it has run. Every
    later read, the rival's own included, goes to the store unpatched.
    """
    graph = registry.knowledge.graph_store
    original = graph.get_node
    results: list[CommandResult] = []

    def read_then_rival(nid: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        row = original(nid, *args, **kwargs)
        if nid == node_id and not results:
            graph.get_node = original
            results.append(executor.execute(rival))
        return row

    graph.get_node = read_then_rival
    try:
        yield results
    finally:
        graph.get_node = original
