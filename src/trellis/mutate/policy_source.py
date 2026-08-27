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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError as PydanticValidationError

from trellis.errors import ConfigError
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.schemas.policy import Policy

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Filename holding a deployment's governance policies, under ``stores_dir``.
POLICY_FILENAME = "policies.json"


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
    path = resolve_policy_path(stores_dir)
    if path is None or not path.exists():
        return []

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = (
            f"Could not read the Trellis policy file at {path}: {exc}. "
            "Fix permissions, or remove the file to run with no policies."
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

    if not isinstance(data, dict) or not isinstance(data.get("policies", []), list):
        msg = (
            f"Malformed Trellis policy file at {path}: expected a JSON object "
            'with a "policies" list. Fix the file, or remove it to run with '
            "no policies."
        )
        raise ConfigError(msg, setting=POLICY_FILENAME)

    policies: list[Policy] = []
    for index, entry in enumerate(data.get("policies", [])):
        try:
            policies.append(Policy.model_validate(entry))
        except PydanticValidationError as exc:
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
    policies = load_policies(registry.stores_dir)
    if policies:
        logger.info(
            "policy_gate_loaded",
            policy_count=len(policies),
            path=str(resolve_policy_path(registry.stores_dir)),
        )
    return DefaultPolicyGate(policies=policies)
