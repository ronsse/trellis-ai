# Trace Format Reference

Complete reference for constructing valid trace JSON for ingestion into the Trellis.

## Trace Schema

A `Trace` is the primary record of an agent or workflow execution. Every trace is immutable after ingestion.

### Top-Level Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `trace_id` | `string` | No | Auto-generated ULID | Unique identifier. Omit to let the system generate one. |
| `source` | `TraceSource` | **Yes** | -- | Who produced this trace. One of: `agent`, `human`, `workflow`, `system`. |
| `intent` | `string` | **Yes** | -- | What this trace was trying to accomplish. |
| `steps` | `list[TraceStep]` | No | `[]` | Ordered list of steps executed. |
| `evidence_used` | `list[EvidenceRef]` | No | `[]` | References to evidence consumed as input. |
| `artifacts_produced` | `list[ArtifactRef]` | No | `[]` | References to artifacts created as output. |
| `outcome` | `Outcome` | No | `null` | Final outcome of the trace. |
| `feedback` | `list[Feedback]` | No | `[]` | Quality feedback recorded against this trace. |
| `context` | `TraceContext` | **Yes** | -- | Execution context (agent, team, domain, timing). |
| `metadata` | `dict` | No | `{}` | Arbitrary key-value pairs. |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version. Do not set manually. |
| `created_at` | `datetime` | No | Current UTC time | When the trace was created. |
| `updated_at` | `datetime` | No | Current UTC time | When the trace was last updated. |

### TraceSource Enum

| Value | Use When |
|-------|----------|
| `agent` | An AI agent performed the work |
| `human` | A human performed the work |
| `workflow` | An automated workflow or pipeline ran |
| `system` | A system-level operation (maintenance, migration) |

### TraceStep

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `step_type` | `string` | **Yes** | -- | Category of step (e.g., `tool_call`, `llm_call`, `decision`, `observation`). |
| `name` | `string` | **Yes** | -- | Human-readable step name (e.g., `search_codebase`, `create_pr`). |
| `args` | `dict` | No | `{}` | Input arguments passed to the step. |
| `result` | `dict` | No | `{}` | Output from the step. |
| `error` | `string` | No | `null` | Error message if the step failed. |
| `duration_ms` | `int` | No | `null` | Execution time in milliseconds. |
| `started_at` | `datetime` | No | Current UTC time | When the step started. |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version. |

### Outcome

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `status` | `OutcomeStatus` | No | `"unknown"` | Final status of the trace. |
| `metrics` | `dict` | No | `{}` | Quantitative metrics (e.g., `{"files_changed": 3}`). |
| `summary` | `string` | No | `null` | Brief human-readable summary of what happened. |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version. |

### OutcomeStatus Enum

| Value | Meaning |
|-------|---------|
| `success` | All goals achieved |
| `failure` | Goals not achieved |
| `partial` | Some goals achieved, others not |
| `unknown` | Outcome not yet determined |

### Feedback

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `feedback_id` | `string` | No | Auto-generated ULID | Unique identifier. |
| `rating` | `float` | No | `null` | Quality score, typically 0.0 to 1.0. |
| `label` | `string` | No | `null` | Categorical label (e.g., `good`, `needs_improvement`). |
| `comment` | `string` | No | `null` | Free-text comment. |
| `given_by` | `string` | No | `"unknown"` | Who provided the feedback. |
| `given_at` | `datetime` | No | Current UTC time | When feedback was given. |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version. |

### TraceContext

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `agent_id` | `string` | No | `null` | ID of the agent that executed this trace. |
| `team` | `string` | No | `null` | Team or group context. |
| `domain` | `string` | No | `null` | Domain scope (e.g., `platform`, `frontend`). |
| `workflow_id` | `string` | No | `null` | Workflow ID if triggered by a workflow. |
| `parent_trace_id` | `string` | No | `null` | Parent trace ID for nested executions. |
| `started_at` | `datetime` | No | Current UTC time | When execution started. |
| `ended_at` | `datetime` | No | `null` | When execution ended. |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version. |

### EvidenceRef

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `evidence_id` | `string` | **Yes** | -- | ID of the evidence item. |
| `role` | `string` | No | `"input"` | Role of this evidence (e.g., `input`, `reference`, `context`). |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version. |

### ArtifactRef

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `artifact_id` | `string` | **Yes** | -- | ID of the artifact. |
| `artifact_type` | `string` | **Yes** | -- | Type of artifact (e.g., `file`, `pr`, `note`, `entity`). |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version. |

---

## Examples

### Example 1: Simple Agent Tool Call (Success)

```json
{
  "source": "agent",
  "intent": "Find and fix the broken import in auth_service.py",
  "steps": [
    {
      "step_type": "tool_call",
      "name": "search_codebase",
      "args": {"query": "from auth_service import", "file_pattern": "*.py"},
      "result": {"matches": 3, "files": ["api/routes.py", "api/middleware.py", "tests/test_auth.py"]},
      "duration_ms": 450
    },
    {
      "step_type": "tool_call",
      "name": "edit_file",
      "args": {"file": "api/routes.py", "old": "from auth_service import verify", "new": "from auth.service import verify"},
      "result": {"status": "applied"},
      "duration_ms": 120
    }
  ],
  "outcome": {
    "status": "success",
    "metrics": {"files_changed": 1},
    "summary": "Fixed broken import in api/routes.py — changed auth_service to auth.service"
  },
  "context": {
    "agent_id": "code-orchestrator",
    "domain": "backend",
    "started_at": "2026-03-10T14:30:00Z",
    "ended_at": "2026-03-10T14:30:05Z"
  }
}
```

### Example 2: Multi-Step Workflow (Partial Outcome)

```json
{
  "source": "workflow",
  "intent": "Deploy v2.3.1 to staging and run smoke tests",
  "steps": [
    {
      "step_type": "tool_call",
      "name": "git_tag",
      "args": {"tag": "v2.3.1", "ref": "main"},
      "result": {"sha": "a1b2c3d"},
      "duration_ms": 800
    },
    {
      "step_type": "tool_call",
      "name": "deploy",
      "args": {"environment": "staging", "version": "v2.3.1"},
      "result": {"status": "deployed", "url": "https://staging.example.com"},
      "duration_ms": 45000
    },
    {
      "step_type": "tool_call",
      "name": "run_smoke_tests",
      "args": {"suite": "smoke", "target": "https://staging.example.com"},
      "result": {"passed": 18, "failed": 2},
      "error": "2 smoke tests failed: test_login_redirect, test_payment_flow",
      "duration_ms": 30000
    }
  ],
  "evidence_used": [
    {"evidence_id": "ev_01JRK5N7QF8GHTM2XVZP3CWD9E", "role": "reference"}
  ],
  "artifacts_produced": [
    {"artifact_id": "deploy_staging_v2.3.1", "artifact_type": "deployment"}
  ],
  "outcome": {
    "status": "partial",
    "metrics": {"tests_passed": 18, "tests_failed": 2},
    "summary": "Deployed v2.3.1 to staging successfully but 2 of 20 smoke tests failed"
  },
  "context": {
    "workflow_id": "deploy_staging",
    "domain": "platform",
    "team": "infra",
    "started_at": "2026-03-10T16:00:00Z",
    "ended_at": "2026-03-10T16:01:16Z"
  }
}
```

### Example 3: Human Action with Feedback

```json
{
  "source": "human",
  "intent": "Review and approve PR #847 for the billing refactor",
  "steps": [
    {
      "step_type": "decision",
      "name": "review_pr",
      "args": {"pr_number": 847, "repository": "acme/billing"},
      "result": {"verdict": "approved", "comments_left": 3}
    },
    {
      "step_type": "observation",
      "name": "note_risk",
      "args": {},
      "result": {"note": "Migration script needs manual verification on prod data volume"}
    }
  ],
  "outcome": {
    "status": "success",
    "summary": "PR #847 approved with 3 comments. Flagged migration risk for prod."
  },
  "feedback": [
    {
      "rating": 0.85,
      "label": "good",
      "comment": "Thorough review that caught the migration risk early",
      "given_by": "tech-lead"
    }
  ],
  "context": {
    "agent_id": "nathan",
    "domain": "billing",
    "team": "payments",
    "started_at": "2026-03-10T10:00:00Z",
    "ended_at": "2026-03-10T10:25:00Z"
  }
}
```

---

## Ingestion

### From a File

```bash
trellis ingest trace trace.json
```

### From Stdin

```bash
echo '{"source":"agent","intent":"test","context":{}}' | trellis ingest trace -
```

### With JSON Output

```bash
trellis ingest trace trace.json --format json
```

Output on success:

```json
{"status": "ingested", "trace_id": "01JRK5N7QF8GHTM2XVZP3CWD9E", "source": "agent", "intent": "test"}
```

Output on validation error:

```json
{"status": "error", "message": "1 validation error for Trace\nsource\n  Input should be 'agent', 'human', 'workflow' or 'system' ..."}
```

---

## Common Mistakes and Validation Errors

| Mistake | Error | Fix |
|---------|-------|-----|
| Missing `source` field | `Field required` | Add `"source": "agent"` (or `human`, `workflow`, `system`) |
| Missing `intent` field | `Field required` | Add `"intent": "description of what happened"` |
| Missing `context` field | `Field required` | Add `"context": {}` at minimum |
| Invalid `source` value | `Input should be 'agent', 'human', 'workflow' or 'system'` | Use one of the four valid enum values |
| Invalid `outcome.status` value | `Input should be 'success', 'failure', 'partial' or 'unknown'` | Use one of the four valid enum values |
| Extra fields not in schema | `Extra inputs are not permitted` | Remove unrecognized fields. The schema uses `extra="forbid"`. |
| `steps` not a list | `Input should be a valid list` | Wrap steps in `[]` |
| `duration_ms` as string | `Input should be a valid integer` | Use integer, not string |
| `rating` outside expected range | No schema error, but meaningless | Use 0.0 to 1.0 by convention |

### Minimum Valid Trace

The absolute minimum valid trace JSON:

```json
{
  "source": "agent",
  "intent": "Describe what happened",
  "context": {}
}
```

All other fields have defaults or are optional.
