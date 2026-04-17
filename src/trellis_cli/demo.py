"""Demo data generation for Trellis — populates stores with realistic sample data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console

from trellis.core.ids import generate_ulid
from trellis.schemas.enums import (
    EdgeKind,
    EntityType,
    EvidenceType,
    OutcomeStatus,
    TraceSource,
)
from trellis.schemas.evidence import AttachmentRef, Evidence
from trellis.schemas.trace import (
    ArtifactRef,
    EvidenceRef,
    Feedback,
    Outcome,
    Trace,
    TraceContext,
    TraceStep,
)
from trellis.stores.base.event_log import EventType
from trellis_cli.stores import (
    _get_registry,
    get_document_store,
    get_graph_store,
    get_trace_store,
)

demo_app = typer.Typer(no_args_is_help=True)
console = Console()


def _ts(days_ago: int, hours: int = 10) -> datetime:
    """Return a UTC datetime N days ago at the given hour."""
    return datetime.now(tz=UTC).replace(
        hour=hours, minute=0, second=0, microsecond=0
    ) - timedelta(days=days_ago)


# ---------------------------------------------------------------------------
#  Entity IDs (stable so edges can reference them)
# ---------------------------------------------------------------------------
_IDS: dict[str, str] = {}


def _id(name: str) -> str:
    if name not in _IDS:
        _IDS[name] = generate_ulid()
    return _IDS[name]


# ---------------------------------------------------------------------------
#  Entities
# ---------------------------------------------------------------------------
def _build_entities() -> list[tuple[str, str, str, dict]]:
    """Return (id, type, name, properties) tuples."""
    return [
        # People
        (
            _id("alice"),
            EntityType.PERSON,
            "Alice Chen",
            {
                "role": "Senior Backend Engineer",
                "team": "platform",
            },
        ),
        (
            _id("bob"),
            EntityType.PERSON,
            "Bob Martinez",
            {
                "role": "ML Engineer",
                "team": "ml-ops",
            },
        ),
        (
            _id("carol"),
            EntityType.PERSON,
            "Carol Okafor",
            {
                "role": "SRE Lead",
                "team": "infra",
            },
        ),
        # Teams
        (
            _id("team-platform"),
            EntityType.TEAM,
            "Platform Team",
            {
                "focus": "Core APIs and data pipelines",
                "members": ["Alice Chen", "Dave Park"],
            },
        ),
        (
            _id("team-mlops"),
            EntityType.TEAM,
            "ML-Ops Team",
            {
                "focus": "Model training and serving infrastructure",
                "members": ["Bob Martinez", "Eve Liu"],
            },
        ),
        (
            _id("team-infra"),
            EntityType.TEAM,
            "Infrastructure Team",
            {
                "focus": "Cloud infrastructure, monitoring, incident response",
                "members": ["Carol Okafor", "Frank Reyes"],
            },
        ),
        # Services
        (
            _id("svc-api"),
            EntityType.SERVICE,
            "user-api",
            {
                "language": "Python",
                "framework": "FastAPI",
                "repo": "acme/user-api",
                "tier": "critical",
            },
        ),
        (
            _id("svc-ml"),
            EntityType.SERVICE,
            "recommendation-engine",
            {
                "language": "Python",
                "framework": "Ray Serve",
                "repo": "acme/rec-engine",
                "tier": "high",
            },
        ),
        (
            _id("svc-gateway"),
            EntityType.SERVICE,
            "api-gateway",
            {
                "language": "Go",
                "framework": "Envoy",
                "repo": "acme/gateway",
                "tier": "critical",
            },
        ),
        (
            _id("svc-events"),
            EntityType.SERVICE,
            "event-bus",
            {
                "language": "Java",
                "framework": "Kafka Streams",
                "repo": "acme/event-bus",
                "tier": "critical",
            },
        ),
        (
            _id("svc-auth"),
            EntityType.SERVICE,
            "auth-service",
            {
                "language": "Rust",
                "framework": "Axum",
                "repo": "acme/auth-svc",
                "tier": "critical",
            },
        ),
        # Systems
        (
            _id("sys-postgres"),
            EntityType.SYSTEM,
            "PostgreSQL",
            {
                "version": "16.2",
                "purpose": "primary OLTP database",
            },
        ),
        (
            _id("sys-redis"),
            EntityType.SYSTEM,
            "Redis",
            {
                "version": "7.2",
                "purpose": "caching and rate limiting",
            },
        ),
        (
            _id("sys-k8s"),
            EntityType.SYSTEM,
            "Kubernetes",
            {
                "version": "1.29",
                "provider": "AWS EKS",
            },
        ),
        # Tools
        (
            _id("tool-gh-actions"),
            EntityType.TOOL,
            "GitHub Actions",
            {
                "purpose": "CI/CD",
            },
        ),
        (
            _id("tool-datadog"),
            EntityType.TOOL,
            "Datadog",
            {
                "purpose": "observability and alerting",
            },
        ),
        (
            _id("tool-terraform"),
            EntityType.TOOL,
            "Terraform",
            {
                "purpose": "infrastructure as code",
            },
        ),
        # Projects
        (
            _id("proj-v2-migration"),
            EntityType.PROJECT,
            "API v2 Migration",
            {
                "status": "in-progress",
                "target": "Q2 2026",
                "description": "Migrate user-api from REST to gRPC",
            },
        ),
        (
            _id("proj-ml-platform"),
            EntityType.PROJECT,
            "ML Platform Buildout",
            {
                "status": "planning",
                "target": "Q3 2026",
                "description": "Centralized model registry and feature store",
            },
        ),
        # Domains
        (
            _id("dom-backend"),
            EntityType.DOMAIN,
            "backend",
            {
                "description": "Server-side services and APIs",
            },
        ),
        (
            _id("dom-infra"),
            EntityType.DOMAIN,
            "infrastructure",
            {
                "description": "Cloud infrastructure and deployment",
            },
        ),
        (
            _id("dom-ml"),
            EntityType.DOMAIN,
            "machine-learning",
            {
                "description": "ML models and serving infrastructure",
            },
        ),
        # Documents / Concepts
        (
            _id("doc-runbook"),
            EntityType.DOCUMENT,
            "Incident Runbook: user-api",
            {
                "format": "markdown",
                "last_updated": "2026-03-15",
            },
        ),
        (
            _id("concept-circuit-breaker"),
            EntityType.CONCEPT,
            "Circuit Breaker Pattern",
            {
                "description": "Prevents cascading failures by failing fast",
            },
        ),
        (
            _id("concept-feature-flag"),
            EntityType.CONCEPT,
            "Feature Flags",
            {
                "description": "Runtime toggles for safe incremental rollouts",
            },
        ),
    ]


# ---------------------------------------------------------------------------
#  Edges
# ---------------------------------------------------------------------------
def _build_edges() -> list[tuple[str, str, str, dict]]:
    """Return (source_id, target_id, edge_kind, properties) tuples."""
    return [
        # Team membership
        (
            _id("alice"),
            _id("team-platform"),
            EdgeKind.ENTITY_PART_OF,
            {"role": "tech lead"},
        ),
        (
            _id("bob"),
            _id("team-mlops"),
            EdgeKind.ENTITY_PART_OF,
            {"role": "senior engineer"},
        ),
        (
            _id("carol"),
            _id("team-infra"),
            EdgeKind.ENTITY_PART_OF,
            {"role": "team lead"},
        ),
        # Service ownership
        (
            _id("svc-api"),
            _id("team-platform"),
            EdgeKind.ENTITY_PART_OF,
            {"relation": "owned-by"},
        ),
        (
            _id("svc-ml"),
            _id("team-mlops"),
            EdgeKind.ENTITY_PART_OF,
            {"relation": "owned-by"},
        ),
        (
            _id("svc-gateway"),
            _id("team-infra"),
            EdgeKind.ENTITY_PART_OF,
            {"relation": "owned-by"},
        ),
        (
            _id("svc-events"),
            _id("team-platform"),
            EdgeKind.ENTITY_PART_OF,
            {"relation": "owned-by"},
        ),
        (
            _id("svc-auth"),
            _id("team-infra"),
            EdgeKind.ENTITY_PART_OF,
            {"relation": "owned-by"},
        ),
        # Service dependencies
        (
            _id("svc-api"),
            _id("sys-postgres"),
            EdgeKind.ENTITY_DEPENDS_ON,
            {"type": "database"},
        ),
        (
            _id("svc-api"),
            _id("sys-redis"),
            EdgeKind.ENTITY_DEPENDS_ON,
            {"type": "cache"},
        ),
        (
            _id("svc-api"),
            _id("svc-auth"),
            EdgeKind.ENTITY_DEPENDS_ON,
            {"type": "authentication"},
        ),
        (
            _id("svc-gateway"),
            _id("svc-api"),
            EdgeKind.ENTITY_DEPENDS_ON,
            {"type": "routes-to"},
        ),
        (
            _id("svc-gateway"),
            _id("svc-ml"),
            EdgeKind.ENTITY_DEPENDS_ON,
            {"type": "routes-to"},
        ),
        (
            _id("svc-ml"),
            _id("sys-redis"),
            EdgeKind.ENTITY_DEPENDS_ON,
            {"type": "feature-cache"},
        ),
        (
            _id("svc-ml"),
            _id("svc-events"),
            EdgeKind.ENTITY_DEPENDS_ON,
            {"type": "consumes-events"},
        ),
        (
            _id("svc-events"),
            _id("sys-postgres"),
            EdgeKind.ENTITY_DEPENDS_ON,
            {"type": "event-store"},
        ),
        # Project relationships
        (
            _id("proj-v2-migration"),
            _id("svc-api"),
            EdgeKind.ENTITY_RELATED_TO,
            {"relation": "migrating"},
        ),
        (
            _id("proj-v2-migration"),
            _id("svc-gateway"),
            EdgeKind.ENTITY_RELATED_TO,
            {"relation": "affected"},
        ),
        (
            _id("proj-ml-platform"),
            _id("svc-ml"),
            EdgeKind.ENTITY_RELATED_TO,
            {"relation": "replaces"},
        ),
        # Concept links
        (
            _id("svc-gateway"),
            _id("concept-circuit-breaker"),
            EdgeKind.ENTITY_RELATED_TO,
            {"relation": "implements"},
        ),
        (
            _id("svc-api"),
            _id("concept-feature-flag"),
            EdgeKind.ENTITY_RELATED_TO,
            {"relation": "uses"},
        ),
        # Domain associations
        (
            _id("svc-api"),
            _id("dom-backend"),
            EdgeKind.ENTITY_RELATED_TO,
            {"relation": "belongs-to"},
        ),
        (
            _id("svc-ml"),
            _id("dom-ml"),
            EdgeKind.ENTITY_RELATED_TO,
            {"relation": "belongs-to"},
        ),
        (
            _id("sys-k8s"),
            _id("dom-infra"),
            EdgeKind.ENTITY_RELATED_TO,
            {"relation": "belongs-to"},
        ),
        # Document attachments
        (
            _id("doc-runbook"),
            _id("svc-api"),
            EdgeKind.EVIDENCE_ATTACHED_TO,
            {"relation": "documents"},
        ),
    ]


# ---------------------------------------------------------------------------
#  Traces
# ---------------------------------------------------------------------------
def _build_traces() -> list[Trace]:
    """Build realistic agent work traces."""
    traces = []

    # --- Trace 1: Successful deployment ---
    t1_id = _id("trace-deploy")
    traces.append(
        Trace(
            trace_id=t1_id,
            source=TraceSource.AGENT,
            intent="Deploy user-api v2.4.1 to production",
            context=TraceContext(
                agent_id="deploy-bot",
                team="platform",
                domain="backend",
                started_at=_ts(5, 9),
                ended_at=_ts(5, 9) + timedelta(minutes=23),
            ),
            steps=[
                TraceStep(
                    step_type="action",
                    name="run_tests",
                    args={"suite": "integration", "target": "user-api"},
                    result={"passed": 342, "failed": 0, "skipped": 3},
                    duration_ms=45000,
                    started_at=_ts(5, 9),
                ),
                TraceStep(
                    step_type="action",
                    name="build_container",
                    args={"image": "acme/user-api", "tag": "v2.4.1"},
                    result={"image_size_mb": 187, "layers": 12},
                    duration_ms=120000,
                    started_at=_ts(5, 9) + timedelta(minutes=1),
                ),
                TraceStep(
                    step_type="action",
                    name="deploy_canary",
                    args={"cluster": "prod-east", "percentage": 10},
                    result={"status": "healthy", "p99_latency_ms": 45},
                    duration_ms=300000,
                    started_at=_ts(5, 9) + timedelta(minutes=3),
                ),
                TraceStep(
                    step_type="action",
                    name="promote_to_full",
                    args={"cluster": "prod-east", "percentage": 100},
                    result={"status": "complete", "rollback_available": True},
                    duration_ms=60000,
                    started_at=_ts(5, 9) + timedelta(minutes=8),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                summary="Deployed user-api v2.4.1. Canary passed with p99 <50ms.",
                metrics={"deploy_time_min": 23, "canary_error_rate": 0.001},
            ),
            artifacts_produced=[
                ArtifactRef(artifact_id=_id("art-deploy-log"), artifact_type="log"),
            ],
            metadata={"version": "2.4.1", "trigger": "merge to main"},
        )
    )

    # --- Trace 2: Debugging a production incident ---
    t2_id = _id("trace-incident")
    traces.append(
        Trace(
            trace_id=t2_id,
            source=TraceSource.AGENT,
            intent="Investigate spike in 5xx errors on user-api",
            context=TraceContext(
                agent_id="incident-bot",
                team="infra",
                domain="backend",
                started_at=_ts(3, 2),
                ended_at=_ts(3, 2) + timedelta(minutes=47),
            ),
            steps=[
                TraceStep(
                    step_type="observation",
                    name="check_metrics",
                    args={"service": "user-api", "window": "30m"},
                    result={
                        "error_rate": 0.12,
                        "p99_latency_ms": 2300,
                        "anomaly": "connection pool exhaustion",
                    },
                    duration_ms=15000,
                    started_at=_ts(3, 2),
                ),
                TraceStep(
                    step_type="observation",
                    name="check_logs",
                    args={"service": "user-api", "level": "error", "limit": 50},
                    result={
                        "top_error": (
                            "psycopg.OperationalError: connection pool exhausted"
                        ),
                        "count": 847,
                        "first_seen": _ts(3, 1).isoformat(),
                    },
                    duration_ms=8000,
                    started_at=_ts(3, 2) + timedelta(minutes=1),
                ),
                TraceStep(
                    step_type="reasoning",
                    name="root_cause_analysis",
                    args={},
                    result={
                        "hypothesis": "Long-running query from new analytics job "
                        "holding connections",
                        "evidence": "New cron job added 2 days ago runs "
                        "unindexed aggregation query",
                    },
                    duration_ms=5000,
                    started_at=_ts(3, 2) + timedelta(minutes=5),
                ),
                TraceStep(
                    step_type="action",
                    name="apply_fix",
                    args={
                        "action": "add_index",
                        "table": "user_events",
                        "column": "created_at",
                    },
                    result={"index_created": True, "migration_time_s": 12},
                    duration_ms=15000,
                    started_at=_ts(3, 2) + timedelta(minutes=10),
                ),
                TraceStep(
                    step_type="action",
                    name="increase_pool_size",
                    args={"service": "user-api", "from": 20, "to": 50},
                    result={"config_updated": True, "restart_required": False},
                    duration_ms=3000,
                    started_at=_ts(3, 2) + timedelta(minutes=15),
                ),
                TraceStep(
                    step_type="observation",
                    name="verify_fix",
                    args={"service": "user-api", "wait_minutes": 10},
                    result={"error_rate": 0.002, "p99_latency_ms": 48},
                    duration_ms=600000,
                    started_at=_ts(3, 2) + timedelta(minutes=20),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                summary="Root cause: unindexed analytics query exhausting "
                "connection pool. Fixed by adding index and increasing pool size.",
                metrics={
                    "time_to_detect_min": 5,
                    "time_to_fix_min": 47,
                    "error_rate_before": 0.12,
                    "error_rate_after": 0.002,
                },
            ),
            evidence_used=[
                EvidenceRef(evidence_id=_id("ev-runbook"), role="reference"),
            ],
            feedback=[
                Feedback(
                    rating=1.0,
                    label="resolved",
                    comment="Clean root cause analysis. "
                    "Should add connection pool alerting.",
                    given_by="carol",
                ),
            ],
            metadata={"incident_id": "INC-2026-0342", "severity": "P1"},
        )
    )

    # --- Trace 3: Code review assistance ---
    t3_id = _id("trace-review")
    traces.append(
        Trace(
            trace_id=t3_id,
            source=TraceSource.AGENT,
            intent="Review PR #487: Add rate limiting to api-gateway",
            context=TraceContext(
                agent_id="review-bot",
                team="platform",
                domain="backend",
                started_at=_ts(2, 14),
                ended_at=_ts(2, 14) + timedelta(minutes=12),
            ),
            steps=[
                TraceStep(
                    step_type="observation",
                    name="read_diff",
                    args={"repo": "acme/gateway", "pr": 487, "files": 7},
                    result={"additions": 234, "deletions": 18},
                    duration_ms=5000,
                    started_at=_ts(2, 14),
                ),
                TraceStep(
                    step_type="reasoning",
                    name="analyze_changes",
                    args={},
                    result={
                        "findings": [
                            "Rate limiter uses fixed window — sliding window "
                            "would be more accurate",
                            "Missing Redis fallback — will block all traffic "
                            "if Redis is down",
                            "Good: per-endpoint configuration via envoy filter",
                        ],
                    },
                    duration_ms=8000,
                    started_at=_ts(2, 14) + timedelta(minutes=1),
                ),
                TraceStep(
                    step_type="action",
                    name="post_review",
                    args={"repo": "acme/gateway", "pr": 487},
                    result={
                        "comments_posted": 3,
                        "verdict": "request_changes",
                    },
                    duration_ms=3000,
                    started_at=_ts(2, 14) + timedelta(minutes=5),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                summary="Reviewed PR #487. Requested changes: "
                "sliding window rate limiter and Redis fallback.",
                metrics={"files_reviewed": 7, "comments": 3},
            ),
            metadata={"pr_url": "https://github.com/acme/gateway/pull/487"},
        )
    )

    # --- Trace 4: Failed ML model deployment ---
    t4_id = _id("trace-ml-fail")
    traces.append(
        Trace(
            trace_id=t4_id,
            source=TraceSource.AGENT,
            intent="Deploy recommendation-engine model v3.1",
            context=TraceContext(
                agent_id="ml-deploy-bot",
                team="ml-ops",
                domain="machine-learning",
                started_at=_ts(1, 11),
                ended_at=_ts(1, 11) + timedelta(minutes=35),
            ),
            steps=[
                TraceStep(
                    step_type="action",
                    name="validate_model",
                    args={"model": "rec-v3.1", "metrics": ["ndcg@10", "mrr"]},
                    result={"ndcg_10": 0.42, "mrr": 0.38, "threshold_met": True},
                    duration_ms=30000,
                    started_at=_ts(1, 11),
                ),
                TraceStep(
                    step_type="action",
                    name="deploy_shadow",
                    args={"cluster": "prod-east", "traffic": "shadow"},
                    result={"status": "running", "shadow_latency_p99_ms": 120},
                    duration_ms=180000,
                    started_at=_ts(1, 11) + timedelta(minutes=1),
                ),
                TraceStep(
                    step_type="observation",
                    name="compare_metrics",
                    args={"baseline": "rec-v3.0", "candidate": "rec-v3.1"},
                    result={
                        "ndcg_delta": 0.03,
                        "latency_delta_ms": 45,
                        "memory_increase_pct": 28,
                    },
                    duration_ms=10000,
                    started_at=_ts(1, 11) + timedelta(minutes=5),
                ),
                TraceStep(
                    step_type="action",
                    name="promote_canary",
                    args={"cluster": "prod-east", "percentage": 5},
                    result={"status": "degraded", "oom_kills": 3},
                    error="OOMKilled: container exceeded 4Gi memory limit",
                    duration_ms=600000,
                    started_at=_ts(1, 11) + timedelta(minutes=10),
                ),
                TraceStep(
                    step_type="action",
                    name="rollback",
                    args={"cluster": "prod-east", "to_version": "rec-v3.0"},
                    result={"status": "rolled_back", "downtime_s": 0},
                    duration_ms=30000,
                    started_at=_ts(1, 11) + timedelta(minutes=25),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.FAILURE,
                summary="Model v3.1 OOMKilled in canary. Memory usage 28% higher "
                "than v3.0. Rolled back with zero downtime.",
                metrics={
                    "oom_kills": 3,
                    "memory_increase_pct": 28,
                    "rollback_time_s": 30,
                },
            ),
            feedback=[
                Feedback(
                    rating=0.7,
                    label="good-rollback",
                    comment="Fast rollback was good. Need memory profiling "
                    "in staging before prod deploys.",
                    given_by="bob",
                ),
            ],
            metadata={"model_version": "3.1", "rollback_to": "3.0"},
        )
    )

    # --- Trace 5: Infrastructure change ---
    t5_id = _id("trace-terraform")
    traces.append(
        Trace(
            trace_id=t5_id,
            source=TraceSource.AGENT,
            intent="Scale Kubernetes node pool for ML workloads",
            context=TraceContext(
                agent_id="infra-bot",
                team="infra",
                domain="infrastructure",
                started_at=_ts(4, 8),
                ended_at=_ts(4, 8) + timedelta(minutes=18),
            ),
            steps=[
                TraceStep(
                    step_type="reasoning",
                    name="capacity_analysis",
                    args={"cluster": "prod-east", "namespace": "ml"},
                    result={
                        "current_nodes": 6,
                        "cpu_utilization": 0.87,
                        "memory_utilization": 0.91,
                        "recommendation": "Add 3 GPU nodes (g5.2xlarge)",
                    },
                    duration_ms=10000,
                    started_at=_ts(4, 8),
                ),
                TraceStep(
                    step_type="action",
                    name="terraform_plan",
                    args={"module": "eks-ml-nodes", "action": "scale"},
                    result={"resources_added": 3, "resources_changed": 1},
                    duration_ms=25000,
                    started_at=_ts(4, 8) + timedelta(minutes=2),
                ),
                TraceStep(
                    step_type="action",
                    name="terraform_apply",
                    args={"module": "eks-ml-nodes", "auto_approve": False},
                    result={
                        "applied": True,
                        "new_node_count": 9,
                        "cost_delta_monthly": 2340,
                    },
                    duration_ms=480000,
                    started_at=_ts(4, 8) + timedelta(minutes=5),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                summary="Scaled ML node pool from 6 to 9 nodes. "
                "Cost increase: $2,340/mo.",
                metrics={
                    "nodes_added": 3,
                    "cost_delta": 2340,
                    "time_min": 18,
                },
            ),
            metadata={"terraform_workspace": "prod", "change_request": "CR-1847"},
        )
    )

    # --- Trace 6: Documentation update ---
    traces.append(
        Trace(
            trace_id=_id("trace-docs"),
            source=TraceSource.AGENT,
            intent="Update API documentation for v2 endpoints",
            context=TraceContext(
                agent_id="docs-bot",
                team="platform",
                domain="backend",
                started_at=_ts(1, 15),
                ended_at=_ts(1, 15) + timedelta(minutes=8),
            ),
            steps=[
                TraceStep(
                    step_type="observation",
                    name="scan_openapi_diff",
                    args={"old": "v1.9", "new": "v2.0"},
                    result={"new_endpoints": 4, "changed": 7, "deprecated": 2},
                    duration_ms=3000,
                    started_at=_ts(1, 15),
                ),
                TraceStep(
                    step_type="action",
                    name="generate_docs",
                    args={"format": "markdown", "include_examples": True},
                    result={"pages_updated": 11, "examples_added": 8},
                    duration_ms=15000,
                    started_at=_ts(1, 15) + timedelta(minutes=1),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                summary="Updated 11 documentation pages with 8 new examples "
                "for v2 endpoints.",
            ),
            metadata={},
        )
    )

    return traces


# ---------------------------------------------------------------------------
#  Evidence
# ---------------------------------------------------------------------------
def _build_evidence() -> list[Evidence]:
    return [
        Evidence(
            evidence_id=_id("ev-runbook"),
            evidence_type=EvidenceType.DOCUMENT,
            content="# Incident Runbook: user-api\n\n"
            "## Connection Pool Exhaustion\n"
            "1. Check Datadog dashboard: user-api > Connections\n"
            "2. Identify long-running queries: "
            "`SELECT * FROM pg_stat_activity WHERE state = 'active'`\n"
            "3. If pool > 80% utilized, increase pool_size in config\n"
            "4. Check for missing indexes on frequently queried columns\n\n"
            "## High Latency\n"
            "1. Check Redis cache hit rate\n"
            "2. Review slow query log\n"
            "3. Check for N+1 queries in recent deployments\n",
            source_origin="manual",
            attached_to=[
                AttachmentRef(
                    target_id=_id("svc-api"),
                    target_type="entity",
                ),
            ],
            metadata={"author": "carol", "last_reviewed": "2026-03-15"},
        ),
        Evidence(
            evidence_id=_id("ev-postmortem"),
            evidence_type=EvidenceType.DOCUMENT,
            content="# Post-Mortem: INC-2026-0342\n\n"
            "**Date:** 2026-03-30\n"
            "**Duration:** 47 minutes\n"
            "**Impact:** 12% error rate on user-api\n\n"
            "## Root Cause\n"
            "Unindexed aggregation query from new analytics cron job "
            "exhausted the PostgreSQL connection pool.\n\n"
            "## Action Items\n"
            "- [x] Add index on user_events.created_at\n"
            "- [x] Increase connection pool size 20 → 50\n"
            "- [ ] Add connection pool utilization alerting\n"
            "- [ ] Require query plan review for new cron jobs\n",
            source_origin="trace",
            source_trace_id=_id("trace-incident"),
            attached_to=[
                AttachmentRef(
                    target_id=_id("trace-incident"),
                    target_type="trace",
                ),
            ],
            metadata={"severity": "P1", "status": "closed"},
        ),
        Evidence(
            evidence_id=_id("ev-arch-decision"),
            evidence_type=EvidenceType.DOCUMENT,
            content="# ADR-012: Circuit Breaker for api-gateway\n\n"
            "## Decision\n"
            "Implement circuit breaker pattern in api-gateway using "
            "Envoy's built-in outlier detection.\n\n"
            "## Context\n"
            "Cascading failures from downstream service outages have "
            "caused 3 incidents in Q1 2026.\n\n"
            "## Consequences\n"
            "- Reduced blast radius of downstream failures\n"
            "- 5% latency increase due to health checking overhead\n"
            "- Need to tune thresholds per-service\n",
            source_origin="manual",
            attached_to=[
                AttachmentRef(
                    target_id=_id("svc-gateway"),
                    target_type="entity",
                ),
            ],
            metadata={"adr_number": 12, "status": "accepted"},
        ),
    ]


# ---------------------------------------------------------------------------
#  Documents (for full-text search)
# ---------------------------------------------------------------------------
def _build_documents() -> list[tuple[str, str, dict]]:
    """Return (doc_id, content, metadata) tuples."""
    return [
        (
            _id("doc-deploy-guide"),
            "# Deployment Guide\n\n"
            "All services use canary deployments via Kubernetes. "
            "The deploy-bot agent handles the standard flow:\n\n"
            "1. Run integration tests\n"
            "2. Build container image\n"
            "3. Deploy to 10% canary\n"
            "4. Monitor for 5 minutes\n"
            "5. Promote to 100%\n\n"
            "Rollback is automatic if error rate exceeds 1%.",
            {"domain": "backend", "type": "guide", "author": "alice"},
        ),
        (
            _id("doc-ml-serving"),
            "# ML Model Serving Architecture\n\n"
            "Models are served via Ray Serve behind the api-gateway. "
            "Key considerations:\n\n"
            "- Memory limits: 4Gi per pod (increase for large models)\n"
            "- Shadow mode testing before canary promotion\n"
            "- Feature cache in Redis (TTL: 5 minutes)\n"
            "- Fallback to previous model version on failure\n\n"
            "## Lessons Learned\n"
            "- Always run memory profiling in staging\n"
            "- Monitor OOMKill events in Datadog",
            {"domain": "machine-learning", "type": "architecture", "author": "bob"},
        ),
        (
            _id("doc-oncall-handbook"),
            "# On-Call Handbook\n\n"
            "## Priority Levels\n"
            "- **P1**: User-facing service down. Page immediately.\n"
            "- **P2**: Degraded performance. Respond within 30 min.\n"
            "- **P3**: Non-urgent. Handle next business day.\n\n"
            "## First Steps\n"
            "1. Check Datadog service dashboard\n"
            "2. Review recent deployments in deploy-bot traces\n"
            "3. Check the Trellis knowledge graph for related incidents\n"
            "4. Consult service runbooks in the document store",
            {"domain": "infrastructure", "type": "handbook", "author": "carol"},
        ),
        (
            _id("doc-api-v2-spec"),
            "# API v2 Specification\n\n"
            "## Breaking Changes from v1\n"
            "- Authentication moved from query params to Bearer tokens\n"
            "- Pagination uses cursor-based instead of offset\n"
            "- Rate limiting: 1000 req/min per API key\n\n"
            "## New Endpoints\n"
            "- POST /v2/users/batch — bulk user creation\n"
            "- GET /v2/users/search — full-text search\n"
            "- POST /v2/events/stream — server-sent events\n"
            "- GET /v2/health/detailed — component-level health",
            {"domain": "backend", "type": "specification", "author": "alice"},
        ),
    ]


# ---------------------------------------------------------------------------
#  Precedents (promoted traces → reusable knowledge)
# ---------------------------------------------------------------------------
def _build_precedents() -> list[
    tuple[str, str, str, str, list[str], list[str], float, dict]
]:
    """Return precedent field tuples for graph store insertion."""
    return [
        (
            _id("prec-conn-pool"),
            "Connection Pool Exhaustion Resolution Pattern",
            "When PostgreSQL connection pool is exhausted: "
            "1) Check pg_stat_activity for long-running queries, "
            "2) Identify missing indexes, "
            "3) Add indexes and increase pool size, "
            "4) Set up connection utilization alerting.",
            "carol",
            [_id("trace-incident")],
            ["backend", "database", "incident-response"],
            0.92,
            {"domain": "backend", "incident_type": "connection-pool"},
        ),
        (
            _id("prec-canary-deploy"),
            "Safe Canary Deployment Checklist",
            "Before promoting canary to full production: "
            "1) Verify error rate < 1%, "
            "2) Check p99 latency within 2x baseline, "
            "3) Monitor memory and CPU for 5 minutes, "
            "4) Ensure rollback is tested and ready. "
            "If any metric degrades, auto-rollback immediately.",
            "alice",
            [_id("trace-deploy"), _id("trace-ml-fail")],
            ["deployment", "backend", "machine-learning"],
            0.88,
            {"domain": "backend", "pattern_type": "deployment"},
        ),
        (
            _id("prec-memory-profiling"),
            "ML Model Memory Profiling Before Production",
            "After model v3.1 OOMKill incident: always run memory "
            "profiling in staging with production-scale data before "
            "deploying new model versions. Check peak memory during "
            "batch inference, not just single-request latency.",
            "bob",
            [_id("trace-ml-fail")],
            ["machine-learning", "deployment", "performance"],
            0.85,
            {"domain": "machine-learning", "learned_from": "INC-2026-0342"},
        ),
    ]


# ---------------------------------------------------------------------------
#  Load command
# ---------------------------------------------------------------------------
@demo_app.command("load")
def load(
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite existing demo data"
    ),
) -> None:
    """Load demo data — realistic traces, entities, and knowledge."""
    registry = _get_registry()
    trace_store = get_trace_store()
    doc_store = get_document_store()
    graph = get_graph_store()

    # Check if data already exists
    if trace_store.count() > 0 and not force:
        console.print(
            "[yellow]Demo data already loaded (use --force to reload).[/yellow]"
        )
        raise typer.Exit()

    console.print("[bold]Loading demo data...[/bold]\n")

    # 1. Entities → graph nodes
    entities = _build_entities()
    for eid, etype, name, props in entities:
        graph.upsert_node(eid, etype, {"name": name, **props})
    console.print(f"  [green]+[/green] {len(entities)} entities")

    # 2. Edges → graph relationships
    edges = _build_edges()
    for src, tgt, kind, props in edges:
        graph.upsert_edge(src, tgt, kind, props)
    console.print(f"  [green]+[/green] {len(edges)} relationships")

    # 3. Traces
    traces = _build_traces()
    for trace in traces:
        trace_store.append(trace)
    console.print(f"  [green]+[/green] {len(traces)} traces")

    # 4. Evidence → documents
    evidence_items = _build_evidence()
    for ev in evidence_items:
        doc_store.put(ev.evidence_id, ev.content or "", ev.metadata)
        # Also link evidence to entities in graph
        for att in ev.attached_to:
            graph.upsert_edge(
                ev.evidence_id,
                att.target_id,
                EdgeKind.EVIDENCE_ATTACHED_TO,
                {"evidence_type": ev.evidence_type},
            )
    console.print(f"  [green]+[/green] {len(evidence_items)} evidence items")

    # 5. Documents (searchable)
    docs = _build_documents()
    for doc_id, content, meta in docs:
        doc_store.put(doc_id, content, meta)
    console.print(f"  [green]+[/green] {len(docs)} documents")

    # 6. Precedents → graph nodes + edges
    precedents = _build_precedents()
    for (
        prec_id,
        title,
        desc,
        promoted_by,
        source_traces,
        applicability,
        confidence,
        meta,
    ) in precedents:
        graph.upsert_node(
            prec_id,
            "precedent",
            {
                "name": title,
                "title": title,
                "description": desc,
                "promoted_by": promoted_by,
                "applicability": applicability,
                "confidence": confidence,
                **meta,
            },
        )
        for tid in source_traces:
            graph.upsert_edge(
                tid,
                prec_id,
                EdgeKind.TRACE_PROMOTED_TO_PRECEDENT,
                {"promoted_by": promoted_by},
            )
    console.print(f"  [green]+[/green] {len(precedents)} precedents")

    # 7. Emit PRECEDENT_PROMOTED events so the precedents tab works
    event_log = registry.event_log
    for (
        prec_id,
        title,
        desc,
        promoted_by,
        source_traces,
        applicability,
        confidence,
        meta,
    ) in precedents:
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="demo",
            entity_id=prec_id,
            entity_type="precedent",
            payload={
                "title": title,
                "description": desc,
                "promoted_by": promoted_by,
                "domain": meta.get(
                    "domain", applicability[0] if applicability else None
                ),
                "applicability": applicability,
                "confidence": confidence,
                "source_traces": source_traces,
            },
        )

    # Emit summary event
    event_log.emit(
        EventType.SYSTEM_INITIALIZED,
        source="demo",
        payload={
            "entities": len(entities),
            "edges": len(edges),
            "traces": len(traces),
            "evidence": len(evidence_items),
            "documents": len(docs),
            "precedents": len(precedents),
        },
    )

    total = (
        len(entities)
        + len(edges)
        + len(traces)
        + len(evidence_items)
        + len(docs)
        + len(precedents)
    )
    console.print(
        f"\n[bold green]Done![/bold green] "
        f"Loaded {total} items into the knowledge graph.\n"
    )
    console.print("  Try these next:")
    console.print("    trellis admin serve      # start the API + UI")
    console.print("    trellis retrieve search 'user-api'")
    console.print("    trellis retrieve entity user-api")
    console.print("    trellis retrieve traces --domain backend")


@demo_app.command("reset")
def reset() -> None:
    """Remove all demo data and reset stores."""
    console.print("[yellow]This will delete all data in the stores.[/yellow]")
    confirm = typer.confirm("Are you sure?")
    if not confirm:
        raise typer.Abort()

    import shutil  # noqa: PLC0415

    from trellis_cli.config import get_data_dir  # noqa: PLC0415

    data_dir = get_data_dir()
    stores_dir = data_dir / "stores"
    if stores_dir.exists():
        shutil.rmtree(stores_dir)
        stores_dir.mkdir(parents=True, exist_ok=True)
        console.print("[green]Stores reset.[/green]")
    else:
        console.print("[dim]No stores found to reset.[/dim]")
