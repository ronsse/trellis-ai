"""Classify overrides schema — one applied tag/domain rule.

A :class:`ClassifyOverrideRow` is one operator-approved rule written by
``trellis classify apply-rule`` or ``POST /api/v1/classify/rules``
(:mod:`trellis.mutate.handlers`'s ``ClassifyRuleApplyHandler``), persisted
in ``classify_overrides.json``
(:class:`trellis.stores.classify_overrides_store.ClassifyOverridesStore`).
It is the audited, revocable analogue of hand-pasting YAML into
``classify.domain_keywords`` / ``classify.domain_aliases`` in
``config.yaml`` — see that module's docstring for the full precedence
story (default < ``config.yaml`` < this overlay).

Two kinds share one row shape rather than two schemas, because both are
"one key maps to one value, applied once, by someone, from some candidate
event":

* ``kind="tag_keyword"``: ``key`` is the mined keyword, ``target`` is the
  ``domain`` it is promoted into
  (:data:`trellis.learning.tag_evolution.FACETS_WITH_WRITE_TARGET` — only
  ``domain`` is ever promotable this way).
* ``kind="domain_alias"``: ``key`` is the alias, ``target`` is the
  canonical domain it normalises to.

``key`` is unique within a ``kind`` — a keyword can be promoted into only
one domain at a time, and an alias can normalise to only one canonical —
so :meth:`~trellis.stores.classify_overrides_store.ClassifyOverridesStore._row_id`
keys the store by ``f"{kind}:{key}"``. Applying the same ``key`` again
under the same ``kind`` replaces the row (last-one-wins), carrying a fresh
``applied_at``/``applied_by``/``source_event_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from trellis.core.base import TrellisModel

#: The two rule families this overlay can hold. Matches the candidate
#: event kinds (``TAG_KEYWORD_CANDIDATE`` / ``DOMAIN_ALIAS_CANDIDATE``) so
#: a row's ``kind`` and its originating event's kind read the same way
#: across the CLI, REST and UI.
ClassifyOverrideKind = Literal["tag_keyword", "domain_alias"]


class ClassifyOverrideRow(TrellisModel):
    """One applied rule, as stored in ``classify_overrides.json``.

    ``applied_by`` is the API key id or CLI actor that approved the rule
    -- never a secret, and never the bare request body's claimed name (the
    handler derives it from the authenticated principal, the same way
    ``SettingsUpdateHandler`` derives ``changed_by``). ``source_event_id``
    is the id of the ``TAG_KEYWORD_CANDIDATE`` / ``DOMAIN_ALIAS_CANDIDATE``
    event the rule was mined from, so a row can always be traced back to
    the support/precision/lift it was approved on.
    """

    kind: ClassifyOverrideKind
    key: str
    target: str
    applied_at: datetime
    applied_by: str
    source_event_id: str
