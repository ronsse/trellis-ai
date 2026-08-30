"""Where policies come from — the one home for resolving and loading them.

Stage 2 of the governed pipeline is injected, not hardcoded
(:class:`~trellis.mutate.executor.PolicyGate` is a Protocol). Injection
without a *source* is what left the documented five-stage pipeline running
as four stages: :func:`~trellis.mutate.build_curate_executor` passed no
gate, so no deployment has ever evaluated a policy at Stage 2.

This module is the missing half. It answers one question — *given a
deployment's store directory, which policies is it enforcing?* — and it is
the only place that answers it. Both CRUD surfaces
(``trellis policy`` and ``/api/policies``) resolve their file through
:func:`resolve_policy_path` so the file they write is by construction the
file the executor reads.

Declared location
-----------------
``<stores_dir>/policies.json`` — co-located with the other store state
(``api_keys.db``, ``outcomes.db``, ``parameters.db``), managed by the CRUD
surfaces rather than hand-edited. ``stores_dir`` is ``<data_dir>/stores``,
so with default configuration that is ``~/.trellis/data/stores/policies.json``.

**Legacy path.** ``trellis policy`` previously wrote
``<data_dir>/policies.json`` while the REST API read
``<stores_dir>/policies.json`` — two surfaces, two files, and nothing read
either one. A file at the legacy path is still honoured, with a warning
naming both paths, so an operator who created policies through the CLI does
not silently lose them the moment the gate becomes load-bearing. The
canonical path wins if both exist.

Default posture
---------------
**No policy file means no policies means an empty gate**, and an empty gate
is transparent: :meth:`DefaultPolicyGate.check` returns ``(True, "", [])``
when nothing matches, so every command reaches Stage 3 exactly as it did
when no gate was wired at all. Trellis ships **zero** default policies on
purpose — a shipped default would change the behaviour of every existing
deployment on upgrade, which is the one thing wiring the gate must not do.
That transparency is pinned by test
(``tests/unit/mutate/test_policy_wiring.py``), not merely asserted here.

Failure posture
---------------
Loading is **strict**: a policy file that exists but cannot be parsed or
validated raises :class:`~trellis.errors.ConfigError` rather than degrading
to zero policies. Degrading would mean a corrupt access-control file
silently disables access control — the caller believes it is governed and it
is not. A deployment with *no* policy file is untouched by this: strictness
only applies once an operator has declared something.

**Strictness is necessary and was not sufficient** (#413). It reasons about
the read, and the exposure came from the write: until #413
:class:`~trellis.stores.policy_store.PolicyStore` degraded an unreadable
file to an empty set and then whole-file-rewrote the path, so the next CRUD
write replaced the ruleset with what survived — nothing. This reader then
parsed a perfectly valid file containing zero policies, without complaint,
and the gate allowed everything. Strictness only ever protected against a
file it *could not parse*. That store now refuses to write while degraded,
which is what makes the guarantee here hold; see its module docstring.

The same reader also had an envelope hole of its own, and it did not need a
write to fire. ``data.get("policies", [])`` meant a JSON object with **no**
``policies`` key — ``{}``, a typo'd key, the wrong file at this path, a
future schema — loaded as zero policies *silently*, so a one-character
hand-edit disabled every policy while every surface reported normal. A
missing key is now a ``ConfigError``, alongside the other malformed shapes:
``PolicyStore._save`` always emits ``policies``, so a dict without it is by
construction not a file Trellis produced.

"No file" vs "a file declaring zero policies"
---------------------------------------------
#413 asks whether enforcement should distinguish them. **It deliberately
does not, and the reason is that the fix removed the dangerous member of
the second class rather than labelling it.** Enumerate what can now reach
"zero policies at Stage 2":

* no file — the shipped default, transparent by design (above);
* ``{"policies": []}`` — what ``trellis policy remove`` writes when the
  last policy goes, i.e. an operator *declaring* zero policies;
* anything else — unreadable file, bad JSON, wrong envelope, missing key,
  one invalid row — **raises**, and the pipeline fails closed;
* a file rewritten from a degraded load, or from any other **stale** view
  of it — another process's write landing in between, a file appearing
  after the store was constructed, a duplicate id collapsing the view —
  **no longer reachable**, because the CRUD store refuses those writes
  too. Note the quantity: the hazard is *fewer* policies, not only zero,
  and an enumeration of the ways to reach zero missed all three of those
  until a review pass caught it (#413's review round).

Nothing is left in which zero policies is a surprise, so a distinction here
would do no safety work — and it would cost real signal:
:func:`build_policy_gate` is rebuilt per mutation, so a warning on a benign
declared-empty file would fire on every write on the busiest path in the
system, which is how operators learn to ignore warnings. The distinction
belongs where the question is actually asked, once, by a human:
``trellis policy list`` says whether the empty answer comes from an absent
file or from a file that declares an empty list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, overload

import structlog

from trellis.errors import ConfigError
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.schemas.policy import Policy

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Filename holding a deployment's governance policies, under ``stores_dir``.
POLICY_FILENAME = "policies.json"

#: How many of a malformed envelope's keys to name in the error. Enough to
#: recognise the file (and the typo), short enough to stay one line.
#: :mod:`trellis.stores.policy_store` bounds the same list with its own
#: ``_MAX_REPORTED_ROWS``; they are different quantities that happen to be
#: small, not one constant split in two.
_MAX_REPORTED_KEYS = 5


@overload
def resolve_policy_path(stores_dir: Path) -> Path: ...


@overload
def resolve_policy_path(stores_dir: None) -> None: ...


def resolve_policy_path(stores_dir: Path | None) -> Path | None:
    """Return the policy file a deployment should read and write.

    Prefers the canonical ``<stores_dir>/policies.json``. Falls back to the
    legacy ``<data_dir>/policies.json`` (i.e. ``stores_dir``'s parent) only
    when the canonical file is absent and the legacy file exists, warning
    once with both paths so the operator can migrate.

    Returns ``None`` when ``stores_dir`` is ``None`` — an in-memory or
    programmatically-constructed registry has no directory to read, and
    that is a normal, silent case (it means "no policies"), not an error.
    """
    if stores_dir is None:
        return None

    canonical = Path(stores_dir) / POLICY_FILENAME
    if canonical.exists():
        return canonical

    legacy = Path(stores_dir).parent / POLICY_FILENAME
    if legacy.exists():
        logger.warning(
            "policy_file_at_legacy_path",
            legacy_path=str(legacy),
            canonical_path=str(canonical),
            hint=(
                "Policies were found at the pre-unification CLI path. They are "
                "being honoured, but move the file to the canonical path — a "
                "future release may stop looking at the legacy location."
            ),
        )
        return legacy

    # Neither exists: hand back the canonical path so writers create the
    # right file. Readers must check existence themselves.
    return canonical


def load_policies(stores_dir: Path | None) -> list[Policy]:
    """Load a deployment's policies, or ``[]`` when none are declared.

    Raises:
        ConfigError: the policy file exists but is unreadable, is not valid
            JSON, is not a JSON object with a ``policies`` list, or contains
            an entry that is not a valid :class:`Policy`. Never degrades to
            an empty list — see the module docstring on failure posture.
    """
    return _load_from_path(resolve_policy_path(stores_dir))


def _load_from_path(path: Path | None) -> list[Policy]:
    """Load and validate policies from an already-resolved path.

    Split out so callers that need the path for their own logging resolve
    it once — re-resolving would repeat the legacy-path warning.
    """
    if path is None or not path.exists():
        return []

    try:
        raw_text = path.read_text(encoding="utf-8")
    # UnicodeDecodeError is a ValueError, not an OSError, and is the shape a
    # truncated or partially-binary write actually takes. Uncaught it still
    # failed closed, but as a bare traceback with none of the recovery advice
    # every other malformed shape here carries.
    except (OSError, UnicodeDecodeError) as exc:
        msg = (
            f"Could not read the Trellis policy file at {path}: "
            f"{type(exc).__name__}: {exc}. Fix permissions or the file's "
            "contents, or remove the file to run with no policies."
        )
        raise ConfigError(msg, setting=POLICY_FILENAME) from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        msg = (
            f"Could not parse the Trellis policy file at {path}: {exc}. "
            "Fix the JSON, or remove the file to run with no policies."
        )
        raise ConfigError(msg, setting=POLICY_FILENAME) from exc

    if not isinstance(data, dict):
        msg = (
            f"Malformed Trellis policy file at {path}: expected a JSON object "
            f'with a "policies" list, got {type(data).__name__}. Fix the file, '
            "or remove it to run with no policies."
        )
        raise ConfigError(msg, setting=POLICY_FILENAME)

    # A *missing* key is not an empty ruleset. ``PolicyStore._save`` always
    # emits ``policies``, so a dict without it is by construction not a file
    # Trellis wrote — and reading it as ``[]`` was a silent fail-open that
    # needed no corrupt write to reach it (#413; see the module docstring).
    if "policies" not in data:
        msg = (
            f"Malformed Trellis policy file at {path}: JSON object has no "
            f'"policies" key (keys: {sorted(data)[:_MAX_REPORTED_KEYS]}). '
            "This is not a file Trellis wrote. Fix the key, or remove the "
            "file to run with no "
            "policies — but note that running with no policies means every "
            "mutation is permitted at Stage 2."
        )
        raise ConfigError(msg, setting=POLICY_FILENAME)

    if not isinstance(data["policies"], list):
        msg = (
            f'Malformed Trellis policy file at {path}: expected "policies" to '
            f"be a list, got {type(data['policies']).__name__}. Fix the file, "
            "or remove it to run with no policies."
        )
        raise ConfigError(msg, setting=POLICY_FILENAME)

    policies: list[Policy] = []
    for index, entry in enumerate(data["policies"]):
        try:
            policies.append(Policy.model_validate(entry))
        # Broad, not just ``PydanticValidationError``. Nothing JSON-decoded
        # should raise anything else today, but a future custom validator or
        # a ``Policy`` bug would escape as a bare traceback carrying none of
        # the recovery advice every other malformed shape here provides —
        # the same complaint this module makes about ``UnicodeDecodeError``.
        except Exception as exc:
            msg = (
                f"Invalid policy at index {index} in {path}: {exc}. "
                "Fix the entry, or remove it to run without that policy."
            )
            raise ConfigError(msg, setting=POLICY_FILENAME) from exc

    return policies


def build_policy_gate(registry: StoreRegistry) -> DefaultPolicyGate:
    """Build the Stage 2 gate for a registry's deployment.

    Always returns a gate — never ``None``. A deployment with no declared
    policies gets an *empty* gate, which is behaviourally identical to the
    no-gate configuration that preceded this wiring but leaves Stage 2
    genuinely present and inspectable rather than skipped. See
    :func:`load_policies` for the failure posture.
    """
    # Resolve once: ``resolve_policy_path`` warns on a legacy-path hit, and
    # calling it again just to build the log line would double that warning
    # on every mutation.
    path = resolve_policy_path(registry.stores_dir)
    policies = _load_from_path(path)
    if policies:
        logger.info(
            "policy_gate_loaded",
            policy_count=len(policies),
            path=str(path),
        )
    return DefaultPolicyGate(policies=policies)
