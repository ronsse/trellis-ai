#!/usr/bin/env bash
#
# Smoke-test the docker-compose stack end-to-end. Hits the four
# orchestrator-relevant endpoints, posts a real trace through the
# governed ingest path, and verifies it round-trips through Postgres.
#
# Usage:
#   docker compose up -d --build
#   ./deploy/smoke.sh             # exits 0 on success, non-zero on any failure
#
# The script is intentionally bash-only (no python, no jq required) so
# it runs in the same shell environments AWS ECS/EKS post-deploy hooks
# usually have available. If you need a richer assertion surface, use
# the unit-test suite — this is the offline-rehearsal probe.
#
# Reset between runs: docker compose down -v
#
set -euo pipefail

BASE="${TRELLIS_BASE_URL:-http://localhost:8420}"
PASS=0
FAIL=0

probe_status() {
    # probe_status <name> <expected_status> <url>
    local name="$1" expected="$2" url="$3"
    local status
    status=$(curl -sS -o /dev/null -w "%{http_code}" -m 5 "$url" || echo "000")
    if [[ "$status" == "$expected" ]]; then
        printf "  [PASS] %-30s %s -> %s\n" "$name" "$url" "$status"
        PASS=$((PASS + 1))
    else
        printf "  [FAIL] %-30s %s -> got %s, want %s\n" "$name" "$url" "$status" "$expected"
        FAIL=$((FAIL + 1))
    fi
}

probe_body_contains() {
    # probe_body_contains <name> <url> <substring>
    local name="$1" url="$2" needle="$3"
    local body
    body=$(curl -sS -m 5 "$url" || echo "")
    if [[ "$body" == *"$needle"* ]]; then
        printf "  [PASS] %-30s body contains %q\n" "$name" "$needle"
        PASS=$((PASS + 1))
    else
        printf "  [FAIL] %-30s body missing %q\n" "$name" "$needle"
        printf "        body was: %s\n" "${body:0:200}"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Endpoint probes against $BASE ==="
probe_status        "GET /healthz"      200 "$BASE/healthz"
probe_body_contains "GET /healthz body"     "$BASE/healthz" '"status":"ok"'
probe_status        "GET /readyz"       200 "$BASE/readyz"
probe_body_contains "GET /readyz body"      "$BASE/readyz" '"status":"ready"'
probe_status        "GET /api/version"  200 "$BASE/api/version"
probe_body_contains "GET /api/version body" "$BASE/api/version" '"api_version":'
probe_status        "GET /ui/"          200 "$BASE/ui/"
probe_body_contains "GET /ui/ body"         "$BASE/ui/" '<title>Trellis</title>'

echo ""
echo "=== Backend round-trip via /api/v1/traces ==="

trace_body='{
  "source": "agent",
  "intent": "smoke-test ingest via Postgres+pgvector",
  "context": {"agent_id": "smoke-test", "domain": "smoke"},
  "steps": [{"step_type": "tool_call", "name": "noop", "args": {}, "result": {}}],
  "outcome": {"status": "success", "summary": "ok"}
}'

ingest_resp=$(curl -sS -m 5 -X POST "$BASE/api/v1/traces" \
    -H "Content-Type: application/json" \
    -d "$trace_body" || echo "")
echo "  POST /api/v1/traces -> $ingest_resp"

# Extract trace_id from {"status":"ok","trace_id":"...",...} without
# requiring jq. The ULID is the only 26-char [0-9A-Z] sequence.
trace_id=$(printf "%s" "$ingest_resp" | grep -oE '[0-9A-Z]{26}' | head -1 || true)
if [[ -z "$trace_id" ]]; then
    echo "  [FAIL] could not extract trace_id from ingest response"
    FAIL=$((FAIL + 1))
else
    printf "  [PASS] %-30s trace_id=%s\n" "trace ingested" "$trace_id"
    PASS=$((PASS + 1))

    # Round-trip read.
    get_resp=$(curl -sS -m 5 "$BASE/api/v1/traces/$trace_id" || echo "")
    if [[ "$get_resp" == *"$trace_id"* ]]; then
        printf "  [PASS] %-30s GET returns the same trace_id\n" "trace round-trips"
        PASS=$((PASS + 1))
    else
        printf "  [FAIL] %-30s GET response missing trace_id\n" "trace round-trips"
        printf "        body was: %s\n" "${get_resp:0:200}"
        FAIL=$((FAIL + 1))
    fi
fi

echo ""
echo "=== Summary ==="
echo "  passed: $PASS"
echo "  failed: $FAIL"
if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
exit 0
