"""No-op store backends for knowledge-plane-only deployments.

A consumer that only needs governed Knowledge-Plane mutations (graph /
vector writes) but runs no Operational-Plane persistence can wire the
``null`` ``event_log`` backend so mutation-event emission becomes a
documented no-op instead of a downstream monkey patch. See
:class:`~trellis.stores.null.event_log.NullEventLog` and issue #196.
"""
