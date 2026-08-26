"""Maintenance workers.

``retention.py`` (``RetentionWorker`` / ``RetentionPolicy`` /
``StalenessDetector``, 266 LOC) was **retired** here, not rehabilitated —
see ``docs/design/adr-retention-prune.md`` §3.3. It had been an orphan since
2026-05-16: referenced nowhere in ``src/``, no CLI, no scheduler.

Two independent reasons it is not the feeder the governed verb needed:

* ``RetentionWorker`` only *marked* old traces in the event log; it never
  deleted, because traces are immutable. The hard rule now does that job by
  construction, so the marking mode had no remaining purpose.
* ``StalenessDetector.check()`` returned exactly one thing — document ids
  whose ``updated_at`` is older than ``staleness_days`` — a pure age test.
  Retention's candidate rules exclude items on precisely that ground: age
  alone is not a value signal in a memory system whose oldest confirmed
  facts are often its most valuable. Its entire output was non-candidates.

Retention now lives in the governed pipeline as ``retention.prune``
(:class:`~trellis.mutate.handlers.RetentionPruneHandler`), which resolves
criteria against the document and graph stores directly.
"""
