"""Parameter reachability — can a proposed snapshot ever be read?

A tuner proposes a :class:`ParameterSet` at a :class:`ParameterScope`.  A
component later resolves its parameters at a scope of its own.  Nothing has
ever checked that the two can meet, and on the reference deployment they
mostly cannot: of 161 outcome cells a tuner can aggregate over, **160 carry
an ``intent_family`` or ``tool_name``** — axes no reader in this tree
supplies — so a snapshot written at one of them is never a candidate in any
resolution chain.  It is stored, listed, and approvable, and it is dead.

The mechanism is :meth:`ParameterStore.resolve`, which builds its five
candidate scopes **out of the query scope's own axis values**::

    (c, d, i, t) -> (c, d, i) -> (c, d) -> (c, i) -> (c)

Every candidate inherits its axis values from the querying component.  So a
reader that never supplies ``intent_family`` leaves it ``None`` in all five,
``_exact_scope_clauses`` emits ``intent_family IS NULL``, and a snapshot that
sets it cannot match any of them.  Reachability is therefore decidable from
the reader alone:

    reachable(S, Q) <=> every axis S sets, Q sets to the same value

which has two failure directions, and this module names both.

**Excess axes.**  The snapshot carries an axis the reader never supplies.
This is the #617 scope mismatch: the tuner aggregates outcomes over four
axes and emits at all four, while every reader resolves at most two.

**A gated key.**  The snapshot is scope-reachable, but the key it sets is
only *used* behind a guard on an axis the scope leaves ``None``.
``domain_match_boost`` is resolved unconditionally and then applied under
``if request_domain and ...`` — so a proposal for it at a domainless scope
resolves fine and multiplies nothing.  Reachable to the store, inert in the
code.

Unknown component: no claim
---------------------------

A ``component_id`` absent from :data:`READ_POINTS` yields **no reasons**,
never a refusal.  ``ParameterScope.component_id`` is an open string and
:class:`ParameterRegistry` is a public facade, so a component living outside
this tree is a supported deployment, not a defect — absence from a scan of
``src/`` is not absence of a reader.  The screen refuses only on positive
evidence that a proposal cannot land, which is the same posture the demotion
evidence gate takes: demotion requires evidence of unhelpfulness, never
absence of evidence of helpfulness.

The no-claim is *recorded* rather than implied.  ``ReachabilityScreen``
carries ``unchecked`` beside ``admitted`` and ``refused``, because "checked
and fine" and "never checked" call for different actions and a shared
silence sends an operator to the wrong one — the same rule
``stamp_staleness`` and ``path_is_present`` keep.

Why the declaration is cross-checked
------------------------------------

:data:`READ_POINTS` is a hand-written claim about code that can drift out
from under it, and a declaration nothing cross-checks is the failure this
repo keeps producing one layer down.
``tests/unit/test_parameter_reachability_rule.py`` derives all three halves
from the AST of ``src/`` — the axes each reader really supplies, the guard
each declared gated key really sits behind, and that no read point is
missing from the roster.  Drift in the direction that costs (a widened
reader still declared narrow, so the screen refuses proposals that would now
land) fails that rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from trellis.schemas.outcome import (
    GRAPH_SEARCH_COMPONENT_ID,
    KEYWORD_SEARCH_COMPONENT_ID,
    MMR_RERANKER_COMPONENT_ID,
    RRF_RERANKER_COMPONENT_ID,
    SEMANTIC_SEARCH_COMPONENT_ID,
)

if TYPE_CHECKING:
    from trellis.schemas.parameters import ParameterProposal, ParameterScope

logger = structlog.get_logger(__name__)


#: The optional learning axes, in precedence-chain order.  ``component_id``
#: is not here: it is mandatory and is what :data:`READ_POINTS` is keyed on.
LEARNING_AXES: tuple[str, ...] = ("domain", "intent_family", "tool_name")


@dataclass(frozen=True)
class ReadPoint:
    """What one component actually asks the parameter store for.

    ``resolvable_axes`` is the set of axes the reader supplies to its
    :class:`ParameterScope`.  An axis outside it is left ``None`` in every
    candidate the resolver builds, so a snapshot setting it is unreachable
    at this component — see the module docstring.

    ``gated_keys`` maps a parameter key to the axis its *use* is guarded on.
    A key absent from this mapping is applied unconditionally once resolved.
    The distinction is not cosmetic: resolution and use are separate steps,
    and a key can clear the first and be inert at the second.
    """

    component_id: str
    reader_module: str
    resolvable_axes: frozenset[str]
    gated_keys: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = sorted(self.resolvable_axes - set(LEARNING_AXES))
        if unknown:
            msg = (
                f"{self.component_id}: unknown resolvable axes {unknown}; "
                f"expected a subset of {list(LEARNING_AXES)}"
            )
            raise ValueError(msg)
        for key, axis in self.gated_keys.items():
            if axis not in LEARNING_AXES:
                msg = (
                    f"{self.component_id}: key {key!r} declares gating axis "
                    f"{axis!r}, which is not a learning axis"
                )
                raise ValueError(msg)


#: Every in-tree component that resolves parameters, and the scope it
#: resolves at.  Derived from the 12 read points in ``src/`` and pinned
#: against them by ``test_parameter_reachability_rule.py``.
#:
#: Note how narrow these are.  Eleven of the twelve read points resolve at
#: ``(component_id)`` alone; one adds ``domain``.  **No reader anywhere
#: supplies ``intent_family`` or ``tool_name``**, which is why a tuner
#: emitting at those axes writes to a scope nothing queries.
_SHARED_STRATEGY_READER = "trellis.retrieve.strategies"

READ_POINTS: Mapping[str, ReadPoint] = {
    rp.component_id: rp
    for rp in (
        # ``_resolve_param`` builds ``ParameterScope(component_id, domain)``
        # for all three search strategies.  ``domain_match_boost`` is
        # resolved unconditionally and applied only under
        # ``if request_domain and props.get("domain") == request_domain``,
        # so at a domainless scope it multiplies nothing.  Its siblings
        # ``curated_boost`` and ``description_boost`` are guarded too, but on
        # node properties rather than on a learning axis, so they are not
        # gated in this sense and are absent here.
        ReadPoint(
            component_id=GRAPH_SEARCH_COMPONENT_ID,
            reader_module=_SHARED_STRATEGY_READER,
            resolvable_axes=frozenset({"domain"}),
            gated_keys={"domain_match_boost": "domain"},
        ),
        ReadPoint(
            component_id=KEYWORD_SEARCH_COMPONENT_ID,
            reader_module=_SHARED_STRATEGY_READER,
            resolvable_axes=frozenset({"domain"}),
        ),
        ReadPoint(
            component_id=SEMANTIC_SEARCH_COMPONENT_ID,
            reader_module=_SHARED_STRATEGY_READER,
            resolvable_axes=frozenset({"domain"}),
        ),
        ReadPoint(
            component_id=RRF_RERANKER_COMPONENT_ID,
            reader_module="trellis.retrieve.rerankers.rrf",
            resolvable_axes=frozenset(),
        ),
        ReadPoint(
            component_id=MMR_RERANKER_COMPONENT_ID,
            reader_module="trellis.retrieve.rerankers.mmr",
            resolvable_axes=frozenset(),
        ),
        ReadPoint(
            component_id="retrieve.effectiveness.items",
            reader_module="trellis.retrieve.effectiveness",
            resolvable_axes=frozenset(),
        ),
        ReadPoint(
            component_id="retrieve.effectiveness.advisory",
            reader_module="trellis.retrieve.effectiveness",
            resolvable_axes=frozenset(),
        ),
        ReadPoint(
            component_id="learning.scoring",
            reader_module="trellis.learning.scoring",
            resolvable_axes=frozenset(),
        ),
        ReadPoint(
            component_id="learning.domain_normalization",
            reader_module="trellis.learning.domain_normalization",
            resolvable_axes=frozenset(),
        ),
        ReadPoint(
            component_id="learning.schema_evolution",
            reader_module="trellis.learning.schema_evolution",
            resolvable_axes=frozenset(),
        ),
        ReadPoint(
            component_id="learning.tag_evolution",
            reader_module="trellis.learning.tag_evolution",
            resolvable_axes=frozenset(),
        ),
    )
}


@dataclass(frozen=True)
class UnreachableReason:
    """One reason a proposal cannot alter behaviour.

    ``kind`` is ``"unsupplied_axis"`` (the snapshot sets an axis the reader
    never supplies, so it is never a resolution candidate) or
    ``"gated_key"`` (the snapshot resolves, but the key's use site is
    guarded on an axis this scope leaves ``None``).
    """

    kind: str
    axis: str
    detail: str
    key: str | None = None


def reachability_reasons(
    scope: ParameterScope,
    keys: Sequence[str] = (),
) -> list[UnreachableReason]:
    """Return every reason a snapshot at ``scope`` setting ``keys`` is inert.

    An empty list means "no reason found", which for a component outside
    :data:`READ_POINTS` means *not checked* rather than *checked and fine* —
    callers that need to tell those apart should use :func:`screen_proposals`,
    which reports them separately.
    """
    read_point = READ_POINTS.get(scope.component_id)
    if read_point is None:
        return []

    reasons: list[UnreachableReason] = []

    for axis in LEARNING_AXES:
        value = getattr(scope, axis, None)
        if value is None or axis in read_point.resolvable_axes:
            continue
        supplies = sorted(read_point.resolvable_axes)
        reasons.append(
            UnreachableReason(
                kind="unsupplied_axis",
                axis=axis,
                detail=(
                    f"scope sets {axis}={value!r}, but {read_point.reader_module} "
                    f"resolves {scope.component_id} at "
                    f"(component_id{''.join(', ' + a for a in supplies)}). "
                    "ParameterStore.resolve builds its candidate chain from the "
                    f"querying scope, where {axis} is always None, so this "
                    "snapshot is never a candidate."
                ),
            )
        )

    for key in keys:
        gating_axis = read_point.gated_keys.get(key)
        if gating_axis is None or getattr(scope, gating_axis, None) is not None:
            continue
        axis = gating_axis
        reasons.append(
            UnreachableReason(
                kind="gated_key",
                axis=axis,
                key=key,
                detail=(
                    f"{key!r} resolves at this scope but its use site in "
                    f"{read_point.reader_module} is guarded on {axis}, which "
                    "this scope leaves unset — the resolved value is never "
                    "applied."
                ),
            )
        )

    return reasons


@dataclass(frozen=True)
class RefusedProposal:
    """A proposal the screen judged unable to alter behaviour."""

    proposal: ParameterProposal
    reasons: tuple[UnreachableReason, ...]

    def summary(self) -> str:
        """Return a one-line operator-facing reason string."""
        return "; ".join(r.detail for r in self.reasons)


@dataclass(frozen=True)
class ReachabilityScreen:
    """The verdict, reported separately from the proposal that produced it.

    ``admitted + refused + unchecked`` partitions the input.  The three are
    kept apart rather than collapsed into a filtered list because a screen
    that removes most of what a rule proposes is a fact *about the rule*,
    and folding it into the output is how that fact stops being visible —
    the same reason ``EffectivenessReport`` reports ``noise_candidates`` and
    ``demotion_screen.admitted`` side by side.
    """

    admitted: tuple[ParameterProposal, ...] = ()
    refused: tuple[RefusedProposal, ...] = ()
    unchecked: tuple[ParameterProposal, ...] = ()

    @property
    def total(self) -> int:
        """Return the number of proposals screened."""
        return len(self.admitted) + len(self.refused) + len(self.unchecked)

    def servable(self) -> list[ParameterProposal]:
        """Return the proposals that should be persisted for approval.

        Unchecked proposals are served: no reader was found to reason
        about, and refusing on that basis would suppress every
        out-of-tree component.
        """
        return [*self.admitted, *self.unchecked]


def screen_proposals(
    proposals: Sequence[ParameterProposal],
) -> ReachabilityScreen:
    """Partition ``proposals`` into admitted / refused / unchecked.

    Pure: it reads no store and writes nothing.  Screening is a decision and
    is kept out of the write, so a deliberate operator-authored snapshot at
    an exotic scope is unaffected by it.
    """
    admitted: list[ParameterProposal] = []
    refused: list[RefusedProposal] = []
    unchecked: list[ParameterProposal] = []

    for proposal in proposals:
        if proposal.scope.component_id not in READ_POINTS:
            unchecked.append(proposal)
            continue
        reasons = reachability_reasons(proposal.scope, tuple(proposal.proposed_values))
        if reasons:
            refused.append(RefusedProposal(proposal, tuple(reasons)))
        else:
            admitted.append(proposal)

    return ReachabilityScreen(
        admitted=tuple(admitted),
        refused=tuple(refused),
        unchecked=tuple(unchecked),
    )
