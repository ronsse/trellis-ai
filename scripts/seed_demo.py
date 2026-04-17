#!/usr/bin/env python3
"""Seed demo data for the Trellis UI.

Usage:
    python scripts/seed_demo.py          # Uses ~/.config/trellis config (same as API)
    python scripts/seed_demo.py --clean  # Wipe and re-seed
"""

from __future__ import annotations

import argparse
import random
import shutil
from datetime import UTC, datetime, timedelta
from typing import Any

from trellis.core.ids import generate_ulid
from trellis.schemas.enums import OutcomeStatus, TraceSource
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
from trellis.stores.registry import StoreRegistry


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def hours_ago(h: int) -> datetime:
    return utc_now() - timedelta(hours=h)


def days_ago(d: int) -> datetime:
    return utc_now() - timedelta(days=d)


# ---------------------------------------------------------------------------
# Graph topology: a realistic agent-centric knowledge graph
# ---------------------------------------------------------------------------

NODES: list[tuple[str, str, dict]] = [
    # Teams
    ("team-platform", "team", {"name": "Platform Engineering", "org": "engineering"}),
    ("team-ml", "team", {"name": "ML Infrastructure", "org": "engineering"}),
    ("team-data", "team", {"name": "Data Engineering", "org": "data"}),
    ("team-security", "team", {"name": "Security", "org": "engineering"}),
    # Services
    (
        "svc-auth",
        "service",
        {"name": "auth-api", "language": "python", "status": "active"},
    ),
    (
        "svc-gateway",
        "service",
        {"name": "api-gateway", "language": "go", "status": "active"},
    ),
    (
        "svc-users",
        "service",
        {"name": "user-service", "language": "python", "status": "active"},
    ),
    (
        "svc-ml-serve",
        "service",
        {"name": "ml-serving", "language": "python", "status": "active"},
    ),
    (
        "svc-ingest",
        "service",
        {"name": "data-ingest", "language": "python", "status": "active"},
    ),
    (
        "svc-notifications",
        "service",
        {"name": "notification-service", "language": "typescript", "status": "active"},
    ),
    (
        "svc-billing",
        "service",
        {"name": "billing-api", "language": "go", "status": "active"},
    ),
    # Systems
    (
        "sys-postgres",
        "system",
        {"name": "PostgreSQL", "version": "16.2", "type": "database"},
    ),
    ("sys-redis", "system", {"name": "Redis", "version": "7.2", "type": "cache"}),
    ("sys-kafka", "system", {"name": "Kafka", "version": "3.7", "type": "streaming"}),
    ("sys-s3", "system", {"name": "S3", "provider": "aws", "type": "storage"}),
    ("sys-k8s", "system", {"name": "Kubernetes", "version": "1.29", "provider": "eks"}),
    # Tools
    ("tool-terraform", "tool", {"name": "Terraform", "version": "1.7"}),
    ("tool-github-actions", "tool", {"name": "GitHub Actions", "type": "ci"}),
    ("tool-datadog", "tool", {"name": "Datadog", "type": "observability"}),
    ("tool-claude", "tool", {"name": "Claude Agent", "type": "ai-agent"}),
    # Concepts
    (
        "concept-rate-limiting",
        "concept",
        {"name": "Rate Limiting", "description": "Request throttling patterns"},
    ),
    (
        "concept-caching",
        "concept",
        {"name": "Caching Strategy", "description": "Multi-layer cache architecture"},
    ),
    (
        "concept-auth-flow",
        "concept",
        {"name": "Auth Flow", "description": "OAuth2 + JWT authentication"},
    ),
    (
        "concept-event-sourcing",
        "concept",
        {"name": "Event Sourcing", "description": "Event-driven state management"},
    ),
    (
        "concept-feature-flags",
        "concept",
        {"name": "Feature Flags", "description": "Gradual rollout mechanism"},
    ),
    # Projects
    (
        "proj-api-v2",
        "project",
        {"name": "API v2 Migration", "status": "in-progress", "quarter": "Q2-2026"},
    ),
    (
        "proj-ml-platform",
        "project",
        {"name": "ML Platform", "status": "in-progress", "quarter": "Q1-2026"},
    ),
    (
        "proj-cost-opt",
        "project",
        {"name": "Cost Optimization", "status": "planning", "quarter": "Q3-2026"},
    ),
    # People
    (
        "person-alice",
        "person",
        {"name": "Alice Chen", "role": "Staff Engineer", "team": "platform"},
    ),
    ("person-bob", "person", {"name": "Bob Park", "role": "ML Engineer", "team": "ml"}),
    (
        "person-carol",
        "person",
        {"name": "Carol Wu", "role": "Data Engineer", "team": "data"},
    ),
    # Domains
    (
        "domain-infra",
        "domain",
        {"name": "Infrastructure", "description": "Cloud infra and platform services"},
    ),
    (
        "domain-ml",
        "domain",
        {"name": "Machine Learning", "description": "ML training and serving"},
    ),
    (
        "domain-data",
        "domain",
        {"name": "Data Platform", "description": "Data pipelines and warehousing"},
    ),
    # Documents/files
    (
        "doc-rfc-rate-limit",
        "document",
        {"name": "RFC: Rate Limiting", "type": "rfc", "status": "approved"},
    ),
    (
        "doc-runbook-deploy",
        "document",
        {"name": "Deployment Runbook", "type": "runbook"},
    ),
    (
        "doc-adr-caching",
        "document",
        {"name": "ADR: Cache Strategy", "type": "adr", "status": "accepted"},
    ),
    (
        "file-gateway-py",
        "file",
        {"name": "gateway.py", "path": "src/gateway/main.py", "language": "python"},
    ),
    (
        "file-auth-py",
        "file",
        {"name": "auth.py", "path": "src/auth/middleware.py", "language": "python"},
    ),
    (
        "file-deploy-yaml",
        "file",
        {"name": "deploy.yaml", "path": "k8s/deploy.yaml", "language": "yaml"},
    ),
]

EDGES: list[tuple[str, str, str, dict]] = [
    # Team ownership
    ("svc-auth", "team-platform", "owned_by", {}),
    ("svc-gateway", "team-platform", "owned_by", {}),
    ("svc-users", "team-platform", "owned_by", {}),
    ("svc-ml-serve", "team-ml", "owned_by", {}),
    ("svc-ingest", "team-data", "owned_by", {}),
    ("svc-notifications", "team-platform", "owned_by", {}),
    ("svc-billing", "team-platform", "owned_by", {}),
    # Service dependencies
    ("svc-gateway", "svc-auth", "entity_depends_on", {"type": "runtime"}),
    ("svc-gateway", "svc-users", "entity_depends_on", {"type": "runtime"}),
    ("svc-gateway", "svc-billing", "entity_depends_on", {"type": "runtime"}),
    ("svc-gateway", "svc-ml-serve", "entity_depends_on", {"type": "runtime"}),
    ("svc-auth", "sys-postgres", "entity_depends_on", {"type": "data"}),
    ("svc-auth", "sys-redis", "entity_depends_on", {"type": "cache"}),
    ("svc-users", "sys-postgres", "entity_depends_on", {"type": "data"}),
    ("svc-ml-serve", "sys-redis", "entity_depends_on", {"type": "cache"}),
    ("svc-ml-serve", "sys-s3", "entity_depends_on", {"type": "model-storage"}),
    ("svc-ingest", "sys-kafka", "entity_depends_on", {"type": "streaming"}),
    ("svc-ingest", "sys-s3", "entity_depends_on", {"type": "storage"}),
    ("svc-notifications", "sys-kafka", "entity_depends_on", {"type": "consumer"}),
    ("svc-billing", "sys-postgres", "entity_depends_on", {"type": "data"}),
    # System runs on k8s
    ("svc-auth", "sys-k8s", "entity_part_of", {}),
    ("svc-gateway", "sys-k8s", "entity_part_of", {}),
    ("svc-users", "sys-k8s", "entity_part_of", {}),
    ("svc-ml-serve", "sys-k8s", "entity_part_of", {}),
    ("svc-ingest", "sys-k8s", "entity_part_of", {}),
    ("svc-notifications", "sys-k8s", "entity_part_of", {}),
    ("svc-billing", "sys-k8s", "entity_part_of", {}),
    # Concepts applied to services
    ("svc-gateway", "concept-rate-limiting", "entity_related_to", {}),
    ("svc-auth", "concept-auth-flow", "entity_related_to", {}),
    ("svc-ml-serve", "concept-caching", "entity_related_to", {}),
    ("svc-ingest", "concept-event-sourcing", "entity_related_to", {}),
    ("svc-gateway", "concept-feature-flags", "entity_related_to", {}),
    # Project scope
    ("proj-api-v2", "svc-gateway", "entity_related_to", {"relation": "target"}),
    ("proj-api-v2", "svc-auth", "entity_related_to", {"relation": "target"}),
    ("proj-ml-platform", "svc-ml-serve", "entity_related_to", {"relation": "target"}),
    ("proj-cost-opt", "sys-k8s", "entity_related_to", {"relation": "target"}),
    # People on teams
    ("person-alice", "team-platform", "entity_part_of", {}),
    ("person-bob", "team-ml", "entity_part_of", {}),
    ("person-carol", "team-data", "entity_part_of", {}),
    # Domains
    ("svc-auth", "domain-infra", "entity_part_of", {}),
    ("svc-gateway", "domain-infra", "entity_part_of", {}),
    ("svc-ml-serve", "domain-ml", "entity_part_of", {}),
    ("svc-ingest", "domain-data", "entity_part_of", {}),
    # Documents
    ("doc-rfc-rate-limit", "concept-rate-limiting", "evidence_supports", {}),
    ("doc-rfc-rate-limit", "svc-gateway", "evidence_attached_to", {}),
    ("doc-adr-caching", "concept-caching", "evidence_supports", {}),
    ("doc-runbook-deploy", "sys-k8s", "evidence_attached_to", {}),
    # Files
    ("file-gateway-py", "svc-gateway", "entity_part_of", {}),
    ("file-auth-py", "svc-auth", "entity_part_of", {}),
    ("file-deploy-yaml", "sys-k8s", "evidence_attached_to", {}),
    # Tool usage
    ("tool-terraform", "sys-k8s", "entity_related_to", {"relation": "provisions"}),
    (
        "tool-github-actions",
        "svc-gateway",
        "entity_related_to",
        {"relation": "deploys"},
    ),
    ("tool-datadog", "svc-gateway", "entity_related_to", {"relation": "monitors"}),
    ("tool-datadog", "svc-auth", "entity_related_to", {"relation": "monitors"}),
    ("tool-claude", "proj-api-v2", "entity_related_to", {"relation": "assists"}),
]

# ---------------------------------------------------------------------------
# Documents (searchable evidence)
# ---------------------------------------------------------------------------

DOCUMENTS: list[tuple[str, str, dict]] = [
    (
        "doc-rfc-rate-limit",
        "RFC: Rate Limiting for API Gateway\n\n"
        "Proposal: Implement token-bucket rate limiting at the gateway layer.\n"
        "Limits: 1000 req/min per API key (default), 5000 req/min (premium).\n"
        "Implementation: Redis-backed sliding window counter.\n"
        "Fallback: In-memory local counters if Redis is unavailable.\n"
        "Status: Approved by platform team, Q2-2026 rollout.",
        {
            "evidence_type": "document",
            "source_origin": "manual",
            "domain": "infrastructure",
        },
    ),
    (
        "doc-runbook-deploy",
        "Deployment Runbook\n\n"
        "1. Verify CI/CD pipeline is green on main branch\n"
        "2. Check Datadog for any active alerts on target service\n"
        "3. Run canary deployment to 5% traffic\n"
        "4. Monitor error rates for 15 minutes\n"
        "5. If metrics stable, proceed to full rollout\n"
        "6. Rollback procedure: kubectl rollout undo deployment/<service>",
        {
            "evidence_type": "document",
            "source_origin": "manual",
            "domain": "infrastructure",
        },
    ),
    (
        "doc-adr-caching",
        "ADR: Multi-Layer Caching Strategy\n\n"
        "Context: ML serving latency exceeds SLA under peak load.\n"
        "Decision: Implement L1 (in-process LRU, 100ms TTL) + L2 (Redis, 5min TTL).\n"
        "Consequences: 90% reduction in model-fetch latency at p99.\n"
        "Tradeoffs: Slight increase in memory per pod; "
        "stale predictions possible within TTL window.",
        {"evidence_type": "document", "source_origin": "manual", "domain": "ml"},
    ),
    (
        None,
        "Incident Report: Auth Service Outage (2026-03-15)\n\n"
        "Duration: 47 minutes. Impact: All authenticated API calls failed.\n"
        "Root cause: PostgreSQL connection pool exhaustion "
        "due to long-running analytics query.\n"
        "Resolution: Killed offending query, increased pool size from 20 to 50.\n"
        "Follow-up: Implement connection pool monitoring and query timeout guardrails.",
        {
            "evidence_type": "document",
            "source_origin": "incident",
            "domain": "infrastructure",
        },
    ),
    (
        None,
        "ML Model Card: Recommendation Engine v3.2\n\n"
        "Model type: Two-tower retrieval + cross-attention reranker.\n"
        "Training data: 90 days of user interactions, ~2.1B events.\n"
        "Latency: p50=12ms, p99=45ms (with Redis cache). Without cache: p99=280ms.\n"
        "Accuracy: nDCG@10 = 0.42 (+8% vs v3.1).\n"
        "Deployed: 2026-03-01 to ml-serving cluster.",
        {"evidence_type": "document", "source_origin": "manual", "domain": "ml"},
    ),
    (
        None,
        "Data Pipeline SLA Report — March 2026\n\n"
        "Pipeline: raw -> bronze -> silver -> gold\n"
        "SLA target: data available in gold within 2 hours of ingestion.\n"
        "Achievement: 99.2% (3 breaches, all during Kafka broker rebalance).\n"
        "Throughput: avg 1.2M events/min, peak 4.8M events/min.\n"
        "Recommendations: Add backpressure handling in ingest service.",
        {"evidence_type": "document", "source_origin": "report", "domain": "data"},
    ),
]

# ---------------------------------------------------------------------------
# Traces with steps
# ---------------------------------------------------------------------------


def make_traces() -> list[Trace]:
    """Generate a diverse set of demo traces."""
    traces = []

    # Trace 1: Successful rate limiting implementation
    traces.append(
        Trace(
            source=TraceSource.AGENT,
            intent="Implement rate limiting on api-gateway",
            steps=[
                TraceStep(
                    step_type="research",
                    name="Search existing rate limiting patterns",
                    args={"query": "rate limiting token bucket"},
                    result={"documents_found": 3, "top_match": "RFC: Rate Limiting"},
                    duration_ms=1200,
                    started_at=hours_ago(48),
                ),
                TraceStep(
                    step_type="reasoning",
                    name="Evaluate implementation options",
                    args={},
                    result={"chosen": "redis-backed sliding window"},
                    duration_ms=800,
                    started_at=hours_ago(47),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="edit_file",
                    args={
                        "file": "src/gateway/middleware/rate_limit.py",
                        "action": "create",
                    },
                    result={"lines_added": 87, "status": "success"},
                    duration_ms=2400,
                    started_at=hours_ago(47),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="edit_file",
                    args={"file": "src/gateway/config.py", "action": "modify"},
                    result={"lines_changed": 12, "status": "success"},
                    duration_ms=600,
                    started_at=hours_ago(46),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="run_tests",
                    args={"suite": "unit", "target": "gateway"},
                    result={"passed": 34, "failed": 0, "skipped": 2},
                    duration_ms=8500,
                    started_at=hours_ago(46),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="run_tests",
                    args={"suite": "integration", "target": "rate_limit"},
                    result={"passed": 8, "failed": 0},
                    duration_ms=15200,
                    started_at=hours_ago(46),
                ),
            ],
            evidence_used=[EvidenceRef(evidence_id="doc-rfc-rate-limit", role="input")],
            artifacts_produced=[
                ArtifactRef(
                    artifact_id="file-gateway-rate-limit-py",
                    artifact_type="source_code",
                )
            ],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                metrics={"time_s": 320, "tokens_used": 24000, "files_changed": 2},
                summary=(
                    "Implemented Redis-backed sliding window"
                    " rate limiter on api-gateway"
                ),
            ),
            feedback=[
                Feedback(
                    rating=5.0,
                    label="quality",
                    comment="Clean implementation, good test coverage",
                    given_by="alice",
                ),
            ],
            context=TraceContext(
                agent_id="claude-agent-1",
                team="platform",
                domain="infrastructure",
                started_at=hours_ago(48),
                ended_at=hours_ago(45),
            ),
        )
    )

    # Trace 2: Failed deployment attempt
    traces.append(
        Trace(
            source=TraceSource.AGENT,
            intent="Deploy auth-api v2.3.1 to production",
            steps=[
                TraceStep(
                    step_type="tool_call",
                    name="kubectl_apply",
                    args={"manifest": "k8s/auth-api/deploy.yaml", "env": "prod"},
                    result={"status": "applied", "replicas": 3},
                    duration_ms=4200,
                    started_at=hours_ago(36),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="check_rollout_status",
                    args={"deployment": "auth-api", "timeout": "300s"},
                    result={"status": "failed", "ready": "1/3"},
                    error="Rollout timed out: CrashLoopBackOff on 2 of 3 pods",
                    duration_ms=305000,
                    started_at=hours_ago(36),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="kubectl_rollback",
                    args={"deployment": "auth-api"},
                    result={"status": "rolled_back", "revision": "v2.3.0"},
                    duration_ms=8900,
                    started_at=hours_ago(35),
                ),
                TraceStep(
                    step_type="reasoning",
                    name="Analyze failure",
                    args={},
                    result={
                        "root_cause": "Missing env var AUTH_JWT_SECRET in new configmap"
                    },
                    duration_ms=3200,
                    started_at=hours_ago(35),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.FAILURE,
                metrics={"time_s": 540, "downtime_min": 5},
                summary=(
                    "Deployment failed due to missing"
                    " AUTH_JWT_SECRET in configmap. Rolled back."
                ),
            ),
            feedback=[
                Feedback(
                    rating=2.0,
                    label="process",
                    comment="Should have validated configmap before deploying",
                    given_by="alice",
                ),
            ],
            context=TraceContext(
                agent_id="deploy-bot",
                team="platform",
                domain="infrastructure",
                started_at=hours_ago(36),
                ended_at=hours_ago(35),
            ),
        )
    )

    # Trace 3: ML model retraining
    traces.append(
        Trace(
            source=TraceSource.WORKFLOW,
            intent="Retrain recommendation model on latest 90-day data",
            steps=[
                TraceStep(
                    step_type="tool_call",
                    name="fetch_training_data",
                    args={"days": 90, "source": "gold.user_interactions"},
                    result={"rows": 2_100_000_000, "size_gb": 48.3},
                    duration_ms=180000,
                    started_at=days_ago(3),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="train_model",
                    args={"model": "two-tower-v3", "epochs": 5, "batch_size": 2048},
                    result={"ndcg_10": 0.42, "loss": 0.0312, "epoch": 5},
                    duration_ms=7200000,
                    started_at=days_ago(3),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="evaluate_model",
                    args={"holdout": "2026-03-last-7d"},
                    result={"ndcg_10": 0.42, "precision_10": 0.38, "recall_10": 0.52},
                    duration_ms=600000,
                    started_at=days_ago(2),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="upload_model",
                    args={"destination": "s3://models/rec-v3.2/", "format": "onnx"},
                    result={"model_size_mb": 340, "uploaded": True},
                    duration_ms=45000,
                    started_at=days_ago(2),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="deploy_model",
                    args={"service": "ml-serving", "version": "v3.2", "canary_pct": 10},
                    result={"status": "canary_deployed", "canary_latency_p99_ms": 42},
                    duration_ms=30000,
                    started_at=days_ago(2),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                metrics={
                    "training_hours": 2.0,
                    "ndcg_improvement": 0.08,
                    "cost_usd": 12.40,
                },
                summary=(
                    "Model v3.2 trained, evaluated (+8% nDCG), deployed to 10% canary"
                ),
            ),
            context=TraceContext(
                agent_id="ml-pipeline",
                team="ml",
                domain="ml",
                started_at=days_ago(3),
                ended_at=days_ago(2),
            ),
        )
    )

    # Trace 4: Data pipeline debugging
    traces.append(
        Trace(
            source=TraceSource.AGENT,
            intent="Debug slow data ingest pipeline (SLA breach alert)",
            steps=[
                TraceStep(
                    step_type="research",
                    name="Check Datadog dashboard",
                    args={"dashboard": "data-pipeline-health"},
                    result={
                        "lag_minutes": 45,
                        "consumer_group": "ingest-silver",
                        "partition_lag": [0, 0, 12400, 0, 58200, 0],
                    },
                    duration_ms=2100,
                    started_at=hours_ago(12),
                ),
                TraceStep(
                    step_type="reasoning",
                    name="Analyze partition lag distribution",
                    args={},
                    result={
                        "finding": (
                            "Partitions 2 and 4 have high lag,"
                            " others are fine — likely skewed keys"
                        )
                    },
                    duration_ms=1500,
                    started_at=hours_ago(12),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="kafka_describe_group",
                    args={"group": "ingest-silver"},
                    result={"members": 6, "assignment": "range", "rebalancing": False},
                    duration_ms=800,
                    started_at=hours_ago(12),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="edit_file",
                    args={"file": "src/ingest/config.py", "action": "modify"},
                    result={
                        "change": "partition_assignment=sticky",
                        "lines_changed": 3,
                    },
                    duration_ms=900,
                    started_at=hours_ago(11),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="restart_consumers",
                    args={"service": "data-ingest", "strategy": "rolling"},
                    result={"restarted": 6, "status": "healthy"},
                    duration_ms=60000,
                    started_at=hours_ago(11),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="verify_lag",
                    args={"wait_minutes": 10},
                    result={"lag_minutes": 3, "all_partitions_caught_up": True},
                    duration_ms=600000,
                    started_at=hours_ago(11),
                ),
            ],
            evidence_used=[],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                metrics={"resolution_minutes": 65, "lag_reduction_pct": 93},
                summary=(
                    "Partition skew causing lag. Switched to"
                    " sticky assignment, lag dropped"
                    " from 45m to 3m."
                ),
            ),
            feedback=[
                Feedback(
                    rating=4.0,
                    label="speed",
                    comment="Good root cause analysis",
                    given_by="carol",
                ),
            ],
            context=TraceContext(
                agent_id="claude-agent-1",
                team="data",
                domain="data",
                started_at=hours_ago(12),
                ended_at=hours_ago(10),
            ),
        )
    )

    # Trace 5: Security audit
    traces.append(
        Trace(
            source=TraceSource.AGENT,
            intent="Audit auth-api for OWASP Top 10 vulnerabilities",
            steps=[
                TraceStep(
                    step_type="tool_call",
                    name="scan_dependencies",
                    args={"tool": "safety", "target": "auth-api"},
                    result={
                        "vulnerabilities": 2,
                        "critical": 0,
                        "high": 1,
                        "medium": 1,
                    },
                    duration_ms=12000,
                    started_at=days_ago(5),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="static_analysis",
                    args={"tool": "bandit", "target": "src/auth/"},
                    result={
                        "issues": 3,
                        "high": 1,
                        "medium": 2,
                        "details": "SQL injection risk in legacy query builder",
                    },
                    duration_ms=8000,
                    started_at=days_ago(5),
                ),
                TraceStep(
                    step_type="reasoning",
                    name="Assess findings and prioritize",
                    args={},
                    result={
                        "priority_1": "SQL injection in legacy query builder",
                        "priority_2": "Outdated cryptography dependency",
                    },
                    duration_ms=2000,
                    started_at=days_ago(5),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="edit_file",
                    args={"file": "src/auth/legacy/queries.py", "action": "fix"},
                    result={
                        "fix": "Replaced string formatting with parameterized queries",
                        "lines_changed": 24,
                    },
                    duration_ms=3500,
                    started_at=days_ago(5),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="run_tests",
                    args={"suite": "security", "target": "auth"},
                    result={"passed": 15, "failed": 0},
                    duration_ms=22000,
                    started_at=days_ago(5),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.PARTIAL,
                metrics={"vulns_found": 5, "vulns_fixed": 3, "remaining": 2},
                summary=(
                    "Fixed SQL injection and 2 medium issues."
                    " 2 dependency vulns require coordinated"
                    " upgrade (tracked in JIRA)."
                ),
            ),
            context=TraceContext(
                agent_id="security-scanner",
                team="security",
                domain="infrastructure",
                started_at=days_ago(5),
                ended_at=days_ago(5),
            ),
        )
    )

    # Trace 6: Cost optimization investigation
    traces.append(
        Trace(
            source=TraceSource.HUMAN,
            intent="Investigate high EKS costs — $48k/mo exceeds budget by 35%",
            steps=[
                TraceStep(
                    step_type="research",
                    name="Pull AWS Cost Explorer data",
                    args={"period": "last-30d", "group_by": "service"},
                    result={
                        "eks_compute": 32400,
                        "eks_storage": 8100,
                        "data_transfer": 7500,
                    },
                    duration_ms=5000,
                    started_at=days_ago(7),
                ),
                TraceStep(
                    step_type="reasoning",
                    name="Identify cost drivers",
                    args={},
                    result={
                        "finding": (
                            "ml-serving pods over-provisioned:"
                            " 8 vCPU requested, avg usage 1.2 vCPU"
                        )
                    },
                    duration_ms=3000,
                    started_at=days_ago(7),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="kubectl_top",
                    args={"namespace": "ml-serving"},
                    result={
                        "pods": 12,
                        "avg_cpu_pct": 15,
                        "avg_mem_pct": 42,
                        "total_cpu_requested": 96,
                    },
                    duration_ms=2000,
                    started_at=days_ago(7),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                metrics={"potential_savings_pct": 40, "potential_savings_usd": 19200},
                summary=(
                    "ML serving over-provisioned by 6x CPU."
                    " Right-sizing to 2 vCPU would"
                    " save ~$19k/mo."
                ),
            ),
            feedback=[
                Feedback(
                    rating=5.0,
                    label="insight",
                    comment="Great finding, exactly what we needed",
                    given_by="alice",
                ),
            ],
            context=TraceContext(
                agent_id="claude-agent-1",
                team="platform",
                domain="infrastructure",
                started_at=days_ago(7),
                ended_at=days_ago(7),
            ),
        )
    )

    # Trace 7: Notification service feature
    traces.append(
        Trace(
            source=TraceSource.AGENT,
            intent="Add webhook delivery to notification-service",
            steps=[
                TraceStep(
                    step_type="research",
                    name="Review existing notification channels",
                    args={"target": "src/notifications/"},
                    result={
                        "existing_channels": ["email", "slack", "sms"],
                        "pattern": "strategy-based dispatch",
                    },
                    duration_ms=1800,
                    started_at=hours_ago(24),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="create_file",
                    args={"file": "src/notifications/channels/webhook.ts"},
                    result={"lines": 95, "implements": "NotificationChannel"},
                    duration_ms=4200,
                    started_at=hours_ago(23),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="edit_file",
                    args={
                        "file": "src/notifications/dispatch.ts",
                        "action": "register channel",
                    },
                    result={"lines_changed": 8},
                    duration_ms=1100,
                    started_at=hours_ago(23),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="run_tests",
                    args={"suite": "unit"},
                    result={"passed": 52, "failed": 0},
                    duration_ms=6800,
                    started_at=hours_ago(23),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                metrics={
                    "time_s": 180,
                    "tokens_used": 11000,
                    "files_changed": 2,
                    "files_created": 1,
                },
                summary=(
                    "Added webhook channel following existing"
                    " strategy pattern. All tests pass."
                ),
            ),
            context=TraceContext(
                agent_id="claude-agent-2",
                team="platform",
                domain="infrastructure",
                started_at=hours_ago(24),
                ended_at=hours_ago(22),
            ),
        )
    )

    # Trace 8: Kafka topic management
    traces.append(
        Trace(
            source=TraceSource.SYSTEM,
            intent="Auto-scale Kafka partitions for user-events topic",
            steps=[
                TraceStep(
                    step_type="tool_call",
                    name="check_partition_throughput",
                    args={"topic": "user-events"},
                    result={
                        "partitions": 12,
                        "throughput_msg_s": 45000,
                        "target_msg_s": 60000,
                    },
                    duration_ms=500,
                    started_at=hours_ago(6),
                ),
                TraceStep(
                    step_type="reasoning",
                    name="Calculate partition count",
                    args={},
                    result={
                        "recommended_partitions": 18,
                        "reasoning": "60k/3.5k per partition = 18",
                    },
                    duration_ms=100,
                    started_at=hours_ago(6),
                ),
                TraceStep(
                    step_type="tool_call",
                    name="kafka_alter_partitions",
                    args={"topic": "user-events", "new_count": 18},
                    result={"status": "success", "previous": 12, "current": 18},
                    duration_ms=3200,
                    started_at=hours_ago(6),
                ),
            ],
            outcome=Outcome(
                status=OutcomeStatus.SUCCESS,
                metrics={"partition_increase": 6},
                summary=(
                    "Scaled user-events topic from 12 to 18"
                    " partitions to meet throughput target."
                ),
            ),
            context=TraceContext(
                agent_id="kafka-autoscaler",
                team="data",
                domain="data",
                started_at=hours_ago(6),
                ended_at=hours_ago(6),
            ),
        )
    )

    return traces


# ---------------------------------------------------------------------------
# Events (to populate event log for dashboard counters and effectiveness)
# ---------------------------------------------------------------------------


def seed_events(
    event_log: Any,
    trace_ids: list[str],
    node_ids: list[str],
) -> None:
    """Emit a realistic stream of events."""
    # Trace events
    for tid in trace_ids:
        event_log.emit(
            EventType.TRACE_INGESTED, source="demo", entity_id=tid, entity_type="trace"
        )

    # Entity events
    for nid in node_ids[:15]:
        event_log.emit(
            EventType.ENTITY_CREATED, source="demo", entity_id=nid, entity_type="node"
        )

    # Some update events
    for nid in random.sample(node_ids[:15], 5):
        event_log.emit(
            EventType.ENTITY_UPDATED, source="demo", entity_id=nid, entity_type="node"
        )

    # Pack assembled events (for effectiveness view)
    for _i in range(8):
        pack_id = generate_ulid()
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="demo",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "intent": random.choice(  # noqa: S311
                    [
                        "Debug pipeline lag",
                        "Deploy service",
                        "Implement feature",
                        "Security audit",
                        "Cost analysis",
                        "Model retraining",
                    ]
                ),
                "item_count": random.randint(3, 12),  # noqa: S311
                "domain": random.choice(  # noqa: S311
                    ["infrastructure", "ml", "data"]
                ),
            },
        )

    # Feedback events
    for i in range(5):
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="demo",
            entity_id=trace_ids[i % len(trace_ids)],
            entity_type="trace",
            payload={
                "rating": random.choice([3.0, 4.0, 4.5, 5.0]),  # noqa: S311
                "label": "quality",
            },
        )

    # Evidence events
    event_log.emit(
        EventType.EVIDENCE_INGESTED,
        source="demo",
        entity_id="doc-rfc-rate-limit",
        entity_type="document",
    )
    event_log.emit(
        EventType.EVIDENCE_INGESTED,
        source="demo",
        entity_id="doc-adr-caching",
        entity_type="document",
    )
    event_log.emit(
        EventType.EVIDENCE_INGESTED,
        source="demo",
        entity_id="doc-runbook-deploy",
        entity_type="document",
    )

    # System events
    event_log.emit(EventType.SYSTEM_INITIALIZED, source="demo")

    # Link events
    event_log.emit(
        EventType.LINK_CREATED,
        source="demo",
        entity_id="svc-gateway",
        entity_type="edge",
        payload={"target": "svc-auth", "type": "depends_on"},
    )
    event_log.emit(
        EventType.LINK_CREATED,
        source="demo",
        entity_id="svc-auth",
        entity_type="edge",
        payload={"target": "sys-postgres", "type": "depends_on"},
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed demo data for XPG UI")
    parser.add_argument(
        "--clean", action="store_true", help="Remove existing data before seeding"
    )
    args = parser.parse_args()

    registry = StoreRegistry.from_config_dir()

    if args.clean:
        # Close, wipe stores dir, re-init
        stores_dir = registry._stores_dir
        if stores_dir and stores_dir.exists():
            print(f"Cleaning {stores_dir}...")
            registry.close()
            shutil.rmtree(stores_dir)
            stores_dir.mkdir(parents=True, exist_ok=True)
            registry = StoreRegistry.from_config_dir()

    graph = registry.graph_store
    trace_store = registry.trace_store
    doc_store = registry.document_store
    event_log = registry.event_log

    # 1. Graph nodes
    print("Seeding graph nodes...")
    node_ids = []
    for node_id, node_type, props in NODES:
        nid = graph.upsert_node(node_id, node_type, props, commit=True)
        node_ids.append(nid)
    print(f"  {len(node_ids)} nodes created")

    # 2. Graph edges
    print("Seeding graph edges...")
    edge_count = 0
    for src, tgt, etype, props in EDGES:
        graph.upsert_edge(src, tgt, etype, props or None, commit=True)
        edge_count += 1
    print(f"  {edge_count} edges created")

    # 3. Documents
    print("Seeding documents...")
    doc_count = 0
    for doc_id, content, metadata in DOCUMENTS:
        doc_store.put(doc_id, content, metadata)
        doc_count += 1
    print(f"  {doc_count} documents created")

    # 4. Traces
    print("Seeding traces...")
    traces = make_traces()
    trace_ids = []
    for trace in traces:
        tid = trace_store.append(trace)
        trace_ids.append(tid)
    print(f"  {len(trace_ids)} traces created")

    # 5. Events
    print("Seeding events...")
    seed_events(event_log, trace_ids, node_ids)
    print(f"  {event_log.count()} events created")

    # Summary
    print("\n--- Demo Data Summary ---")
    print(f"  Nodes:     {graph.count_nodes()}")
    print(f"  Edges:     {graph.count_edges()}")
    print(f"  Documents: {doc_store.count()}")
    print(f"  Traces:    {trace_store.count()}")
    print(f"  Events:    {event_log.count()}")
    print("\nStart the API with: trellis-api")
    print("Then open: http://localhost:8420/ui")


if __name__ == "__main__":
    main()
