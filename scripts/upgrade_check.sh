#!/usr/bin/env bash
# Upgrade one SQLite store from the previous release to this build, and back.
#
# Usage: scripts/upgrade_check.sh [candidate]
#
#   candidate  a wheel, or a source tree to install; default: the checkout this
#              script lives in.
#
# Why: cold_install_check.sh proves a wheel works from nothing. Nothing proves that
# a store written by the last release still reads under the next one, or that a
# deployer who rolls back can still read what the new build wrote. This script is
# that check; .github/workflows/upgrade-check.yml runs it on release tags, nightly
# and on demand.
#
# What a run does, with one set of SQLite stores under a scratch directory:
#   1. installs the previous release (the newest v<N>[.<N>...] tag that is an
#      ancestor of HEAD and does not point at HEAD, read from git, never named
#      here) from a clone of that tag into its own fresh venv, and the candidate
#      into a second fresh venv;
#   2. old build: writes one document, one trace, two entities and an edge, a
#      pack, and pack feedback ("alpha" set), then reads them back;
#   3. candidate: reads the alpha set back (this is the upgrade), writes a "beta"
#      set, reads both, runs `trellis admin health`;
#   4. old build again: reads both sets (this is the rollback).
# Fields the script chose are compared with what it wrote. Fields the build chose
# (content hash, timestamps) are pinned at the first read and must not change in
# any later read by either build.
#
# What this does not cover: Postgres and the other cloud backends (SQLite only),
# per-item pack feedback (the MCP tool and POST /packs/{id}/feedback, whose request
# shape differs between releases), and a release that bumps a store's SCHEMA_VERSION:
# the registry's schema fingerprint is crossed only with matching values, because
# none has changed since v0.7.0. A pack's items differ between
# builds by design, so a pack is checked for the document it must carry and for the
# pack and feedback counts, not for equality of what it served.
#
# The environment is scrubbed: an empty HOME, no TRELLIS_*, PYTHONPATH or
# PYTHONHOME, and TRELLIS_CONFIG_DIR and TRELLIS_DATA_DIR set to scratch, so no
# step can fall back to a real config or store. The venvs get no extras.
#
# Interpreter: ${PYTHON:-python3}. Scratch lives under $TMPDIR (mktemp -d) and is
# removed on exit; set UPGRADE_KEEP=1 to keep it. UPGRADE_FROM=<tag> overrides the
# derived previous release, for reproducing a failure against an older one.
#
# Output is one line per step: "[ok]   <step>: <detail>". The first failure prints
# "[FAIL] <step>: <reason>" and exits 1; a missing prerequisite exits 2.
set -Eeuo pipefail

# How long /healthz may take to answer after `admin serve` starts.
SERVER_WAIT_SECS=30
# Ceiling for any one CLI or HTTP call; a hang fails the step instead of the job.
CMD_TIMEOUT=180

CURRENT_STEP="setup"
ROOT=""
SERVER_PID=""

die() {
    echo "[FAIL] ${CURRENT_STEP}: $*" >&2
    exit 1
}

pass() {
    echo "[ok]   ${CURRENT_STEP}: $*"
}

step() {
    CURRENT_STEP="$1"
}

on_error() {
    local rc=$? line=$1
    echo "[FAIL] ${CURRENT_STEP}: command failed (exit ${rc}) at line ${line}" >&2
    exit "${rc}"
}
trap 'on_error $LINENO' ERR

stop_server() {
    if [ -n "${SERVER_PID}" ] && kill -0 "${SERVER_PID}" 2>/dev/null; then
        kill "${SERVER_PID}" 2>/dev/null || true
        local _
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "${SERVER_PID}" 2>/dev/null || break
            sleep 1
        done
        kill -9 "${SERVER_PID}" 2>/dev/null || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
    SERVER_PID=""
}

cleanup() {
    local rc=$?
    stop_server
    if [ -n "${ROOT}" ] && [ -z "${UPGRADE_KEEP:-}" ]; then
        rm -rf "${ROOT}"
    elif [ -n "${ROOT}" ]; then
        echo "scratch kept at ${ROOT}" >&2
    fi
    exit "${rc}"
}
trap cleanup EXIT

[ "$#" -le 1 ] || {
    echo "usage: $0 [wheel-or-source-tree] (got $# arguments; a glob that matched several wheels?)" >&2
    exit 2
}
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
git -C "${REPO}" rev-parse --git-dir >/dev/null 2>&1 || {
    echo "${REPO} is not a git checkout: the previous release is read from its tags" >&2
    exit 2
}
if [ "$(git -C "${REPO}" rev-parse --is-shallow-repository)" = "true" ]; then
    echo "shallow clone: the release tags are missing (actions/checkout needs fetch-depth: 0)" >&2
    exit 2
fi
CANDIDATE="${1:-${REPO}}"
if [ -d "${CANDIDATE}" ]; then
    CANDIDATE="$(cd "${CANDIDATE}" && pwd)"
elif [ ! -f "${CANDIDATE}" ]; then
    echo "not a wheel or a directory: ${CANDIDATE}" >&2
    exit 2
else
    CANDIDATE="$(cd "$(dirname "${CANDIDATE}")" && pwd)/$(basename "${CANDIDATE}")"
fi

# The newest release tag that is an ancestor of HEAD and does not point at HEAD, so
# the same script serves a nightly (HEAD is untagged: the latest release) and a tag
# push (HEAD is the tag being released: the one before it). Pre-release tags such
# as v1.0.0-rc1 are not releases and are skipped.
previous_release_tag() {
    local at_head tags tag
    at_head="$(git -C "${REPO}" tag --points-at HEAD)"
    tags="$(git -C "${REPO}" tag --list 'v[0-9]*' --merged HEAD --sort=-v:refname)"
    while IFS= read -r tag; do
        [[ "${tag}" =~ ^v[0-9]+(\.[0-9]+)*$ ]] || continue
        case " ${at_head//$'\n'/ } " in *" ${tag} "*) continue ;; esac
        printf '%s\n' "${tag}"
        return 0
    done <<<"${tags}"
    return 1
}
if [ -n "${UPGRADE_FROM:-}" ]; then
    PREV_TAG="${UPGRADE_FROM}"
    git -C "${REPO}" rev-parse --verify --quiet "refs/tags/${PREV_TAG}" >/dev/null || {
        echo "UPGRADE_FROM=${PREV_TAG} is not a tag in ${REPO}" >&2
        exit 2
    }
else
    PREV_TAG="$(previous_release_tag)" || {
        echo "no earlier release tag (v<N>.<N>...) is an ancestor of HEAD: nothing to upgrade from" >&2
        exit 2
    }
fi

# The JSON checks below run under the interpreter that makes the venvs, not either
# build's, so a defect in a build cannot break the check that reports it. Each
# expression is evaluated over the parsed JSON in $OUT, bound to `d`, with the
# environment as `E`.
PYBIN="${PYTHON:-python3}"
OUT=""
ERR=""
PINS=""

# Run a command, capturing stdout to $OUT and stderr to $ERR. A non-zero exit or
# a timeout fails the current step and shows both tails.
run() {
    local rc=0
    timeout "${CMD_TIMEOUT}" "$@" >"${OUT}" 2>"${ERR}" || rc=$?
    if [ "${rc}" -ne 0 ]; then
        {
            echo "--- stdout (tail)"
            tail -n 20 "${OUT}"
            echo "--- stderr (tail)"
            tail -n 20 "${ERR}"
        } >&2
        die "exit ${rc} from: $*"
    fi
}

jcheck() {
    local expr=$1 what=$2
    if ! "${PYBIN}" -I -c '
import json, os, sys
d = json.load(open(sys.argv[1]))
E = os.environ
sys.exit(0 if eval(sys.argv[2]) else 1)
' "${OUT}" "${expr}" 2>"${ERR}"; then
        { echo "--- stdout (head)"; head -c 2000 "${OUT}"; echo; cat "${ERR}"; } >&2
        die "${what}"
    fi
}

# Print d's value for a Python expression.
jget() {
    "${PYBIN}" -I -c '
import json, os, sys
d = json.load(open(sys.argv[1]))
E = os.environ
print(eval(sys.argv[2]))
' "${OUT}" "$1"
}

# Pin a value the build chose: the first read records it, every later read, by
# either build, must return the same.
pin() {
    local name=$1 expr=$2 msg
    if ! msg="$("${PYBIN}" -I -c '
import json, os, sys
d = json.load(open(sys.argv[1]))
E = os.environ
now = json.dumps(eval(sys.argv[2]), sort_keys=True)
path = sys.argv[3]
if not os.path.exists(path):
    open(path, "w").write(now)
    sys.exit(0)
was = open(path).read()
if was != now:
    print(f"{sys.argv[4]} changed between reads: first {was}, now {now}")
    sys.exit(1)
' "${OUT}" "${expr}" "${PINS}/${name}" "${name}" 2>&1)"; then
        die "${msg}"
    fi
}

# ---------------------------------------------------------------- install
step "install"
ROOT="$(mktemp -d)"
OUT="${ROOT}/out.txt"
ERR="${ROOT}/err.txt"
PINS="${ROOT}/pins"
mkdir -p "${PINS}"

# The previous release, from a clone of its tag: hatch-vcs derives the version from
# the tag, so the build reports the version a deployer would have installed.
git -c advice.detachedHead=false clone --quiet --branch "${PREV_TAG}" "${REPO}" "${ROOT}/old-src" ||
    die "could not clone tag ${PREV_TAG} from ${REPO}"
for build in old new; do
    "${PYBIN}" -m venv "${ROOT}/${build}"
done
"${ROOT}/old/bin/python" -m pip install --quiet --disable-pip-version-check "${ROOT}/old-src"
"${ROOT}/new/bin/python" -m pip install --quiet --disable-pip-version-check "${CANDIDATE}"
version_of() {
    "${ROOT}/$1/bin/python" -I -c 'from importlib.metadata import version; print(version("trellis-ai"))'
}
OLD_VERSION="$(version_of old)"
NEW_VERSION="$(version_of new)"
[ "${OLD_VERSION}" != "${NEW_VERSION}" ] ||
    die "the previous release and the candidate are both ${OLD_VERSION}: nothing is being upgraded"
pass "previous ${PREV_TAG} (${OLD_VERSION}), candidate ${NEW_VERSION}"

# The environment the stores live in: scratch everywhere, nothing inherited that
# could point at a real config, a real store or a source tree.
mkdir -p "${ROOT}/work" "${ROOT}/home" "${ROOT}/config" "${ROOT}/data"
cd "${ROOT}/work"
export HOME="${ROOT}/home"
for v in $(compgen -e | grep '^TRELLIS_' || true); do unset "${v}"; done
unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_STATE_HOME
unset PYTHONPATH
unset PYTHONHOME
export TRELLIS_CONFIG_DIR="${ROOT}/config"
export TRELLIS_DATA_DIR="${ROOT}/data"
# v0.9.0 prints `--format json` through Rich, which hard-wraps a long line at the
# terminal width (80 columns off a tty) and so breaks a string in two. Later
# builds print the JSON unwrapped; the previous release must be readable too.
export COLUMNS=100000

# Which build the next command runs as.
BUILD=""
TRELLIS=""
use() {
    BUILD="$1"
    TRELLIS="${ROOT}/${BUILD}/bin/trellis"
}

# ----------------------------------------------------------------- server
# `admin serve` for the calls that have no CLI form: assembling a pack under an id,
# feedback against that id, and the edges around an entity.
server_up() {
    local port t0 ready=""
    port="$("${PYBIN}" -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
    PORT="${port}"
    "${TRELLIS}" admin serve --port "${PORT}" >"${ROOT}/server-${BUILD}.log" 2>&1 &
    SERVER_PID=$!
    t0=${SECONDS}
    while [ $((SECONDS - t0)) -lt "${SERVER_WAIT_SECS}" ]; do
        kill -0 "${SERVER_PID}" 2>/dev/null || break
        if "${PYBIN}" -I -c '
import sys, urllib.request
sys.exit(0 if urllib.request.urlopen(sys.argv[1], timeout=5).status == 200 else 1)
' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null; then
            ready=1
            break
        fi
        sleep 0.5
    done
    if [ -z "${ready}" ]; then
        { echo "--- server log (tail)"; tail -n 20 "${ROOT}/server-${BUILD}.log"; } >&2
        die "the ${BUILD} build's server did not answer /healthz on port ${PORT} within ${SERVER_WAIT_SECS}s"
    fi
}

# rest METHOD PATH [JSON-BODY]: the response body lands in $OUT; any HTTP error
# fails the step with the status and body.
rest() {
    run "${PYBIN}" -I -c '
import sys, urllib.error, urllib.request
method, url = sys.argv[1], sys.argv[2]
body = sys.argv[3].encode() if len(sys.argv) > 3 else None
req = urllib.request.Request(
    url, data=body, method=method, headers={"Content-Type": "application/json"}
)
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as exc:
    sys.exit(f"HTTP {exc.code} from {method} {url}: {exc.read().decode()[:500]}")
' "$1" "http://127.0.0.1:${PORT}$2" "${@:3}"
}

# ----------------------------------------------------------------- records
# Two sets of records, "alpha" written by the previous release and "beta" by the
# candidate. Ids the script picks are fixed per set; ids the build mints are kept in
# exported variables (read back through E[...] in the expressions).

write_set() {
    local s=$1
    export "DOC_${s}=upgrade-doc-${s}"
    export "CONTENT_${s}=Sourdough starter ${s}marker needs warm water and daily feeding."
    export "TRACE_${s}=upgrade-trace-${s}"
    export "TRACE_INTENT_${s}=Feed the sourdough starter (${s})"
    local doc="DOC_${s}" content="CONTENT_${s}" trace="TRACE_${s}" intent="TRACE_INTENT_${s}"

    step "${BUILD} writes ${s}: document"
    cat >"${ROOT}/doc-${s}.json" <<EOF
{"evidence_id": "${!doc}", "evidence_type": "document", "source_origin": "upgrade-check", "content": "${!content}"}
EOF
    run "${TRELLIS}" ingest evidence "${ROOT}/doc-${s}.json" --format json
    jcheck 'd["status"] == "ingested" and d["evidence_id"] == E["DOC_'"${s}"'"]' "the document was not ingested"
    pass "exit 0, ${!doc}"

    step "${BUILD} writes ${s}: trace"
    cat >"${ROOT}/trace-${s}.json" <<EOF
{"trace_id": "${!trace}", "source": "agent", "intent": "${!intent}",
 "context": {"agent_id": "upgrade-check", "domain": "baking",
             "started_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:01:00Z"},
 "steps": [{"step_type": "tool_call", "name": "feed", "args": {"flour_g": 50},
            "result": {"summary": "fed starter"}}],
 "outcome": {"status": "success", "summary": "starter bubbling"}}
EOF
    run "${TRELLIS}" ingest trace "${ROOT}/trace-${s}.json" --format json
    jcheck 'd["status"] == "ingested" and d["trace_id"] == E["TRACE_'"${s}"'"]' "the trace was not ingested"
    pass "exit 0, ${!trace}"

    step "${BUILD} writes ${s}: entities and an edge"
    run "${TRELLIS}" curate entity concept "Starter ${s}" --properties "{\"upgrade_check\": \"${s}\"}" --format json
    jcheck 'd["status"] == "ok" and d["node_id"]' "the first entity was not created"
    local minted
    minted="$(jget 'd["node_id"]')"
    export "ENT1_${s}=${minted}"
    run "${TRELLIS}" curate entity concept "Culture ${s}" --format json
    jcheck 'd["status"] == "ok" and d["node_id"]' "the second entity was not created"
    minted="$(jget 'd["node_id"]')"
    export "ENT2_${s}=${minted}"
    local ent1="ENT1_${s}" ent2="ENT2_${s}"
    run "${TRELLIS}" curate link "${!ent1}" "${!ent2}" --kind entity_related_to --format json
    jcheck 'd["status"] == "ok" and d["edge_id"]' "the edge was not created"
    minted="$(jget 'd["edge_id"]')"
    export "EDGE_${s}=${minted}"
    pass "exit 0, ${!ent1} -> ${!ent2}"

    step "${BUILD} writes ${s}: pack and pack feedback"
    server_up
    rest POST /api/v1/packs "{\"intent\": \"${s}marker sourdough starter\"}"
    jcheck 'd["pack_id"] and any(i["item_id"] == E["DOC_'"${s}"'"] for i in d["items"])' \
        "the pack does not carry the document this build just wrote"
    minted="$(jget 'd["pack_id"]')"
    export "PACK_${s}=${minted}"
    local pack="PACK_${s}"
    rest POST /api/v1/feedback \
        "{\"target_id\": \"${!pack}\", \"pack_id\": \"${!pack}\", \"rating\": 0.8, \"comment\": \"upgrade check\"}"
    jcheck 'd["status"] == "success"' "the pack feedback was not recorded"
    stop_server
    pass "pack ${!pack} carries ${!doc}, feedback recorded"
    SETS_WRITTEN=$((SETS_WRITTEN + 1))
}

# read_set SET: read one set back with the active build. SETS_WRITTEN is how many
# sets exist in the stores now, so a row lost or doubled by a build shows as a count.
read_set() {
    local s=$1
    step "${BUILD} reads ${s}: document"
    run "${TRELLIS}" retrieve search "${s}marker" --format json
    jcheck "any(r['doc_id'] == E['DOC_${s}'] and r['content'] == E['CONTENT_${s}'] \
and r['metadata'].get('evidence_type') == 'document' \
and r['metadata'].get('source_origin') == 'upgrade-check' for r in d['results'])" \
        "the document is not found by search, or its content or metadata differ from what was written"
    pin "doc-${s}" "[[r['content_hash'], r['created_at']] for r in d['results'] if r['doc_id'] == E['DOC_${s}']]"
    pass "found, content and metadata equal what was written"

    step "${BUILD} reads ${s}: trace"
    local trace="TRACE_${s}"
    run "${TRELLIS}" retrieve trace "${!trace}" --format json
    jcheck "d['trace_id'] == E['TRACE_${s}'] and d['source'] == 'agent' \
and d['intent'] == E['TRACE_INTENT_${s}'] \
and d['context']['agent_id'] == 'upgrade-check' and d['context']['domain'] == 'baking' \
and len(d['steps']) == 1 and d['steps'][0]['name'] == 'feed' \
and d['steps'][0]['step_type'] == 'tool_call' and d['steps'][0]['args'] == {'flour_g': 50} \
and d['steps'][0]['result'] == {'summary': 'fed starter'} \
and d['outcome']['status'] == 'success' and d['outcome']['summary'] == 'starter bubbling'" \
        "the trace differs from what was written"
    pin "trace-${s}" "d['created_at']"
    pass "intent, context, step and outcome equal what was written"

    step "${BUILD} reads ${s}: entities"
    local ent1="ENT1_${s}" ent2="ENT2_${s}"
    run "${TRELLIS}" retrieve entity "${!ent1}" --format json
    jcheck "d['node_id'] == E['ENT1_${s}'] and d['node_type'] == 'concept' \
and d['properties']['name'] == 'Starter ${s}' and d['properties']['upgrade_check'] == '${s}' \
and d['valid_to'] is None" "the first entity differs from what was written"
    pin "entity-${s}" "[d['created_at'], d['valid_from']]"
    run "${TRELLIS}" retrieve entity "${!ent2}" --format json
    jcheck "d['node_id'] == E['ENT2_${s}'] and d['node_type'] == 'concept' \
and d['properties']['name'] == 'Culture ${s}' and d['valid_to'] is None" \
        "the second entity differs from what was written"
    pass "both entities equal what was written"

    step "${BUILD} reads ${s}: edge"
    server_up
    rest GET "/api/v1/entities/${!ent1}"
    stop_server
    jcheck "any(e['edge_id'] == E['EDGE_${s}'] and e['source_id'] == E['ENT1_${s}'] \
and e['target_id'] == E['ENT2_${s}'] and e['edge_type'] == 'entity_related_to' \
for e in d['subgraph']['edges'])" "the edge is not in the entity's subgraph, or differs from what was written"
    pass "edge ${!ent1} -> ${!ent2} present"

    step "${BUILD} reads ${s}: packs and feedback"
    run "${TRELLIS}" analyze context-effectiveness --days 1 --no-meta-trace --format json
    # Every pack and every pack feedback written so far, by either build. The
    # counts are exact: a pack event lost or doubled by a build would show here.
    export EXPECT_SETS="${SETS_WRITTEN}"
    # item_scores is left alone: it only lists an item once it has appeared in
    # enough packs, so it is empty here by design.
    jcheck "d['total_packs'] == int(E['EXPECT_SETS']) and d['total_feedback'] == int(E['EXPECT_SETS']) \
and d['success_rate'] == 1.0" \
        "pack or feedback events differ from what was written: expected ${SETS_WRITTEN} of each, every rating a success"
    pass "${SETS_WRITTEN} pack(s) and ${SETS_WRITTEN} feedback row(s), success rate 1.0"

    step "${BUILD} reads ${s}: totals"
    run "${TRELLIS}" admin stats --format json
    jcheck "d['traces'] == int(E['EXPECT_SETS']) and d['documents'] == int(E['EXPECT_SETS']) \
and d['nodes'] >= 2 * int(E['EXPECT_SETS']) and d['edges'] >= int(E['EXPECT_SETS'])" \
        "store totals differ from what was written: expected ${SETS_WRITTEN} traces and documents"
    pass "$(jget "'traces=%s documents=%s nodes=%s edges=%s' % (d['traces'], d['documents'], d['nodes'], d['edges'])")"
}

health() {
    step "${BUILD} admin health"
    run "${TRELLIS}" admin health --format json
    # It exits 5 when a store file is damaged (so `run` fails the step); the keys
    # are the rest. vectors.db is created lazily and reads false until first use.
    jcheck "d['config'] and d['data_dir'] and d['stores_dir'] and d['documents.db'] and d['graph.db'] \
and d['events.db'] and d['traces.db'] \
and all(v == 'sqlite' for v in d.get('backends', {}).values())" \
        "health does not report the stores as present, or reports a backend other than sqlite"
    pass "exit 0, config, data_dir and the four store files present"
}

SETS_WRITTEN=0

# ------------------------------------------------------- previous release
step "init"
use old
run "${TRELLIS}" admin init --format json
jcheck 'd["status"] == "initialized"' "init did not initialize a fresh store"
[ -f "${ROOT}/config/config.yaml" ] || die "init wrote no config under \$TRELLIS_CONFIG_DIR"
pass "previous release ${OLD_VERSION}, config under the scratch \$TRELLIS_CONFIG_DIR"

write_set alpha
# The stores are created on first write; they must have landed in the scratch dir.
for store in documents traces graph events; do
    [ -f "${ROOT}/data/stores/${store}.db" ] || die "no ${store}.db under the scratch \$TRELLIS_DATA_DIR after the first writes"
done
read_set alpha

# ------------------------------------------------------------ the upgrade
use new
health
read_set alpha
write_set beta
read_set beta
read_set alpha
health

# ------------------------------------------------------------ the rollback
use old
read_set alpha
read_set beta
health

step "scratch"
# Nothing above may have written outside the scratch tree it was given.
STRAY="$(find "${HOME}" -mindepth 1 -print -quit)"
[ -z "${STRAY}" ] || die "a step wrote under the scratch HOME: ${STRAY}"
pass "nothing under the scratch HOME"

echo "upgrade check passed: ${PREV_TAG} (${OLD_VERSION}) -> ${NEW_VERSION} -> back to ${OLD_VERSION}"
