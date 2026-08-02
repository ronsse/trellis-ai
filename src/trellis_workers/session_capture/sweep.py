"""One capture sweep — the code path behind every front door.

``python -m trellis_workers.session_capture``, the ``trellis-session-capture``
console script and ``trellis worker capture-sessions`` all call
:func:`run_sweep`. It builds the store registry from the operator's Trellis
config (or takes one from the caller), builds the local distillation model
client from that config, and runs one sweep. The env vars it reads and the
systemd timer that schedules it are documented in
``docs/agent-guide/session-auto-capture.md``.

This lives beside ``__main__`` rather than inside it so the ``trellis worker``
front door can ``import`` it normally: importing ``…session_capture.__main__``
from another module loads a *second* copy under ``python -m``, where the same
file is already running as ``__main__`` — enough to make
:class:`CaptureJudgeUnavailableError` fail an ``except`` clause by identity.

**The judge is not optional.** ``distill_session`` fail-closes on a missing
client, so a sweep without one triggers nothing, writes nothing, and advances
no watermark — a perfectly clean-looking no-op. The missing-client case
therefore raises (:class:`CaptureJudgeUnavailableError`) instead of returning
an empty report, and the per-session case is counted by
:func:`judge_unavailable_sessions` rather than left as a log warning.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from trellis.stores.registry import StoreRegistry
from trellis_workers.session_capture.capture import (
    DEFAULT_SAMPLE_DENOMINATOR,
    DEFAULT_SOURCE_SYSTEM,
    run_capture,
)
from trellis_workers.session_capture.distill import DEFAULT_DISTILL_MODEL

if TYPE_CHECKING:
    from trellis.llm import LLMClient
    from trellis_workers.session_capture.models import CaptureReport

logger = structlog.get_logger(__name__)

ENV_ROOT = "TRELLIS_CAPTURE_TRANSCRIPTS_ROOT"
ENV_WATERMARK = "TRELLIS_CAPTURE_WATERMARK"
ENV_SAMPLE = "TRELLIS_CAPTURE_SAMPLE_DENOMINATOR"
ENV_SOURCE_SYSTEM = "TRELLIS_CAPTURE_SOURCE_SYSTEM"
ENV_MODEL = "TRELLIS_DISTILL_MODEL"
ENV_STRICT = "TRELLIS_CAPTURE_STRICT"

#: Actionable remediation appended to every judge-unavailable message.
JUDGE_REMEDIATION = (
    "Configure an 'llm:' block in config.yaml (provider, api_key_env, model, "
    "base_url for a local endpoint) and install the matching extra "
    "([llm-openai] / [llm-anthropic])."
)


class CaptureJudgeUnavailableError(RuntimeError):
    """No distillation judge could be built, so the sweep would be a no-op."""


def _config_dir() -> Path:
    return Path(os.environ.get("TRELLIS_CONFIG_DIR", str(Path.home() / ".trellis")))


def _default_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _default_watermark() -> Path:
    return _config_dir() / "capture-watermark.json"


def _sample_denominator() -> int:
    raw = os.environ.get(ENV_SAMPLE, "").strip()
    if not raw:
        return DEFAULT_SAMPLE_DENOMINATOR
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SAMPLE_DENOMINATOR
    return value if value >= 1 else DEFAULT_SAMPLE_DENOMINATOR


def strict_mode() -> bool:
    """Whether a *partial* judge outage should fail the run.

    Default ``True``: a sweep that left sessions unjudged did not do the job
    it was scheduled to do, and a green systemd unit would hide that. Set
    ``TRELLIS_CAPTURE_STRICT=0`` to go back to the pre-existing softer
    semantics — those sessions stay un-watermarked and are retried on the next
    sweep, so an operator who considers a single transient model timeout in a
    forty-session run self-healing rather than a failure can opt out. The
    *total* no-op (no judge at all) always fails, strict or not: nothing is
    retried because nothing ran.
    """
    raw = os.environ.get(ENV_STRICT, "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def build_judge_client(registry: StoreRegistry) -> LLMClient:
    """Build the local judge client, or raise loudly.

    This used to swallow every failure and return ``None``, which combined
    with ``distill_session``'s fail-closed contract to turn a misconfigured
    ``llm:`` block into a silent no-op sweep. Raising keeps the fail-closed
    behaviour (nothing is captured without a judge) while making the cause
    reach the operator.

    Raises:
        CaptureJudgeUnavailableError: the client could not be built, or the
            registry has no ``llm:`` configuration at all.
    """
    try:
        client = registry.build_llm_client()
    except Exception as exc:
        msg = f"could not build the distillation judge: {exc}. {JUDGE_REMEDIATION}"
        raise CaptureJudgeUnavailableError(msg) from exc
    if client is None:
        msg = f"no distillation judge is configured. {JUDGE_REMEDIATION}"
        raise CaptureJudgeUnavailableError(msg)
    return client


def judge_unavailable_sessions(report: CaptureReport) -> int:
    """Sessions the judge could not adjudicate during a sweep.

    These are left un-watermarked for a later retry, so the run is a partial
    failure — an explicit count rather than a warning buried in the log.
    """
    return sum(
        1 for warning in report.warnings if warning.get("kind") == "distill_unavailable"
    )


def run_sweep(
    *,
    registry: StoreRegistry | None = None,
    dry_run: bool = False,
) -> CaptureReport:
    """Run one capture sweep against the operator's configured stores.

    Args:
        registry: Store registry to sweep into. ``None`` builds one from the
            config dir — the standalone entry point's behaviour. Callers that
            already own a registry (the ``trellis worker`` front door) pass
            theirs so both paths honour the same config resolution.
        dry_run: Plan the sweep without writing memories or advancing the
            watermark.

    Raises:
        CaptureJudgeUnavailableError: no distillation judge could be built.
    """
    root = Path(os.environ.get(ENV_ROOT, str(_default_root())))
    watermark = Path(os.environ.get(ENV_WATERMARK, str(_default_watermark())))
    source_system = os.environ.get(ENV_SOURCE_SYSTEM, DEFAULT_SOURCE_SYSTEM)
    model_id = os.environ.get(ENV_MODEL, DEFAULT_DISTILL_MODEL)

    store_registry = registry
    if store_registry is None:
        store_registry = StoreRegistry.from_config_dir()
    client = build_judge_client(store_registry)

    return run_capture(
        store_registry,
        transcripts_root=root,
        watermark_path=watermark,
        llm_client=client,
        source_system=source_system,
        sample_denominator=_sample_denominator(),
        distill_model_id=model_id,
        dry_run=dry_run,
    )
