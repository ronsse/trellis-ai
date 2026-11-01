#!/usr/bin/env bash
# Install a built trellis-ai wheel into a fresh venv and run the first-run paths.
#
# Usage: scripts/cold_install_check.sh <path-to-wheel>
#
# Why: every other workflow installs the checkout (`pip install -e .`), and
# publish.yml builds the wheel but never installs it. A wheel-only defect (package
# data missing from the wheel, a console script pointing at a moved module, a
# first-run path that quietly needs a checkout) therefore reaches a deployer
# unseen. This script is the check; .github/workflows/cold-install.yml runs it.
#
# What a run proves is "the wheel works from nothing", so the environment is
# scrubbed: an empty HOME, an empty working directory outside any checkout, and
# no TRELLIS_*, PYTHONPATH or PYTHONHOME. The venv gets no extras.
#
# Interpreter: ${PYTHON:-python3}. CI passes setup-python's; locally,
#   PYTHON=$(uv python find 3.11) scripts/cold_install_check.sh dist/*.whl
# Scratch lives under $TMPDIR (mktemp -d) and is removed on exit; set
# COLD_INSTALL_KEEP=1 to keep it.
#
# Output is one line per step: "[ok]   <step>: <detail>". The first failure
# prints "[FAIL] <step>: <reason>" and exits non-zero.
set -Eeuo pipefail

# Floor, not a pin: the job must not churn on every new MCP tool.
MCP_MIN_TOOLS=10
# How long /healthz may take to answer after `admin serve` starts.
SERVER_WAIT_SECS=30
# Ceiling for any one CLI invocation; a hang fails the step instead of the job.
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

cleanup() {
    local rc=$?
    if [ -n "${SERVER_PID}" ] && kill -0 "${SERVER_PID}" 2>/dev/null; then
        kill "${SERVER_PID}" 2>/dev/null || true
        local i
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "${SERVER_PID}" 2>/dev/null || break
            sleep 1
        done
        kill -9 "${SERVER_PID}" 2>/dev/null || true
    fi
    if [ -n "${ROOT}" ] && [ -z "${COLD_INSTALL_KEEP:-}" ]; then
        rm -rf "${ROOT}"
    elif [ -n "${ROOT}" ]; then
        echo "scratch kept at ${ROOT}" >&2
    fi
    exit "${rc}"
}
trap cleanup EXIT

[ "$#" -eq 1 ] || {
    echo "usage: $0 <path-to-wheel> (got $# arguments; a glob that matched several wheels?)" >&2
    exit 2
}
[ -f "$1" ] || { echo "not a file: $1" >&2; exit 2; }
WHEEL="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
# What the wheel must ship is read from the checkout this script lives in, so the
# console scripts and packages checked below follow pyproject.toml and cannot drift
# from it. (The script runs from an empty directory, so resolve the path now.)
PYPROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/pyproject.toml"
[ -f "${PYPROJECT}" ] || { echo "no pyproject.toml at ${PYPROJECT}: run the script from a checkout" >&2; exit 2; }

# Run a command, capturing stdout to $OUT and stderr to $ERR. A non-zero exit or
# a timeout fails the current step and shows both tails.
OUT=""
ERR=""
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

# Assert a Python expression over the parsed JSON in $OUT (bound to `d`).
jcheck() {
    local expr=$1 what=$2
    if ! "${VPY}" -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if eval(sys.argv[2]) else 1)
' "${OUT}" "${expr}" 2>"${ERR}"; then
        { echo "--- stdout (head)"; head -c 2000 "${OUT}"; echo; cat "${ERR}"; } >&2
        die "${what}"
    fi
}

# Print d's value for a Python expression, for the one-line detail.
jget() {
    "${VPY}" -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(eval(sys.argv[2]))
' "${OUT}" "$1"
}

# ---------------------------------------------------------------- setup
step "setup"
ROOT="$(mktemp -d)"
OUT="${ROOT}/out.txt"
ERR="${ROOT}/err.txt"
VENV="${ROOT}/venv"
VPY="${VENV}/bin/python"
"${PYTHON:-python3}" -m venv "${VENV}"
"${VPY}" -m pip install --quiet --disable-pip-version-check "${WHEEL}"
# Read out of the venv's python, which has tomllib (requires-python is >=3.11).
pyproject_names() {
    "${VPY}" -I -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
if sys.argv[2] == "scripts":
    names = sorted(cfg["project"]["scripts"])
else:
    wheel = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]
    names = sorted(p.rsplit("/", 1)[-1] for p in wheel["packages"])
if not names:
    sys.exit(f"pyproject.toml lists no {sys.argv[2]}")
print(*names)
' "${PYPROJECT}" "$1"
}
# Assigned first so a failure here stops the run; inside the here-string it would
# be swallowed and leave an empty roster that every check below passes.
SCRIPTS_LINE="$(pyproject_names scripts)"
PACKAGES_LINE="$(pyproject_names packages)"
read -r -a SCRIPT_NAMES <<<"${SCRIPTS_LINE}"
read -r -a PACKAGE_NAMES <<<"${PACKAGES_LINE}"

# The environment a deployer's first run has: an empty directory outside any
# checkout, an empty HOME, and nothing inherited that could point at a source
# tree or a real config. The console scripts run without -I, so an inherited
# PYTHONPATH would make them import a checkout.
mkdir -p "${ROOT}/work" "${ROOT}/home"
cd "${ROOT}/work"
export HOME="${ROOT}/home"
for v in $(compgen -e | grep '^TRELLIS_' || true); do unset "${v}"; done
unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_STATE_HOME
unset PYTHONPATH
unset PYTHONHOME
export PATH="${VENV}/bin:${PATH}"
TRELLIS="${VENV}/bin/trellis"
pass "$("${VPY}" --version 2>&1), $(basename "${WHEEL}"), no extras"

# A fresh HOME per scenario, so each first-run path starts from nothing: the
# README's `admin init` would otherwise find the config quickstart wrote.
fresh_home() {
    export HOME="${ROOT}/home/$1"
    mkdir -p "${HOME}"
}

# ------------------------------------------------------------ provenance
step "provenance"
# No -I: this must see the environment the console scripts see. Every top-level
# package the wheel ships, not just the two the CLI needs.
FILES="$("${VPY}" -c '
import importlib, sys
for name in sys.argv[1:]:
    print(importlib.import_module(name).__file__)
' "${PACKAGE_NAMES[@]}")"
if ! OUTSIDE="$(printf '%s\n' "${FILES}" | "${VPY}" -I -c '
import os, sys
venv = os.path.realpath(sys.argv[1]) + os.sep
outside = []
for f in sys.stdin.read().splitlines():
    real = os.path.realpath(f)
    if not (real.startswith(venv) and "site-packages" in real):
        outside.append(f)
print(" ".join(outside))
sys.exit(1 if outside else 0)
' "${VENV}")"; then
    die "imported outside the venv's site-packages, so a source tree and not the wheel: ${OUTSIDE}"
fi
pass "${#PACKAGE_NAMES[@]} packages (${PACKAGE_NAMES[*]}) import from the venv's site-packages"

# -------------------------------------------------------- console scripts
step "console scripts"
# Every console script resolves to a target that imports. A script is generated
# from an entry point, so a moved module breaks it without breaking anything the
# steps below run.
cat >"${ROOT}/console_scripts.py" <<'PY'
import sys
from importlib.metadata import distribution

want = set(sys.argv[1:])
eps = {
    e.name: e
    for e in distribution("trellis-ai").entry_points
    if e.group == "console_scripts"
}
missing = want - eps.keys()
if missing:
    raise SystemExit(f"console scripts missing: {sorted(missing)}")
for name in sorted(want):
    eps[name].load()
print(", ".join(sorted(want)))
PY
run "${VPY}" -I "${ROOT}/console_scripts.py" "${SCRIPT_NAMES[@]}"
for name in "${SCRIPT_NAMES[@]}"; do
    [ -x "${VENV}/bin/${name}" ] || die "${VENV}/bin/${name} was not installed"
done
pass "$(cat "${OUT}") load, and are installed in venv/bin"

# ------------------------------------------------------------ quickstart
step "quickstart"
fresh_home quickstart
run "${TRELLIS}" admin quickstart --format json
jcheck 'd["status"] == "ok"' "status is not \"ok\""
jcheck 'd["mcp_on_path"]' "trellis-mcp is not on PATH after installing the wheel"
pass "exit 0, status ok, trellis-mcp on PATH"

step "quickstart --with-skills"
run "${TRELLIS}" admin quickstart --with-skills user --format json
jcheck 'd["status"] == "ok"' "status is not \"ok\""
# A skill that failed to copy is reported per skill while status stays "ok", so
# the files on disk are the check, plus no per-skill failure in the report.
jcheck 'not [s for s in d["skills"] if s["status"] == "failed"]' \
    "a bundled skill failed to install (see the report above)"
SKILL_FILES="$(find "${HOME}/.claude/skills" -type f 2>/dev/null | wc -l | tr -d ' ')"
[ "${SKILL_FILES}" -ge 1 ] || die "no skill file under \$HOME/.claude/skills (package data missing from the wheel?)"
pass "exit 0, ${SKILL_FILES} skill files under \$HOME/.claude/skills"

step "health"
run "${TRELLIS}" admin health --format json
# It exits 5 when a store file is damaged (so `run` fails the step) and 0 when one
# is missing, so the report is the check for the rest. The *.db keys are not
# asserted: SQLite files are created lazily and read false right after quickstart.
jcheck 'd["config"] and d["data_dir"] and d["stores_dir"]' \
    "health reports no config/stores after quickstart"
pass "exit 0, config, data_dir and stores_dir present"
QUICKSTART_HOME="${HOME}"

# --------------------------------------------------------- README sequence
# README.md "Quickstart": init, demo load, serve (see the server step), then the
# four retrieve commands, here with --format json where the command takes it.
step "readme: admin init"
fresh_home readme
run "${TRELLIS}" admin init --format json
jcheck 'd["status"] != "exists"' "a fresh HOME already had a config"
pass "exit 0, status $(jget 'd["status"]')"

step "readme: demo load"
run "${TRELLIS}" demo load
pass "exit 0"

step "readme: retrieve search"
run "${TRELLIS}" retrieve search 'user-api' --format json
jcheck 'd["count"] >= 1' "the search found nothing in the demo data"
pass "exit 0, $(jget 'd["count"]') results"

step "readme: retrieve entity"
run "${TRELLIS}" retrieve entity user-api --format json
pass "exit 0"

step "readme: retrieve traces"
run "${TRELLIS}" retrieve traces --domain backend --format json
jcheck 'd["count"] >= 1' "no backend traces in the demo data"
pass "exit 0, $(jget 'd["count"]') traces"

step "readme: retrieve pack"
run "${TRELLIS}" retrieve pack --intent "deploy staging for user-api" --format json
jcheck 'd["count"] >= 1' "the pack has no items"
pass "exit 0, $(jget 'd["count"]') items"
README_HOME="${HOME}"

# ----------------------------------------------------------------- ingest
step "ingest corpus"
fresh_home ingest
mkdir -p "${ROOT}/corpus"
cat >"${ROOT}/corpus/gardening.md" <<'EOF'
# Gardening notes

Tomatoes need full sunlight and regular watering. Prune the lower leaves and
stake the vines early in the season.
EOF
cat >"${ROOT}/corpus/baking.md" <<'EOF'
# Baking notes

Sourdough rises slowly. Keep the starter warm, feed it daily, and fold the
dough several times before the final proof.
EOF
run "${TRELLIS}" admin init --format json
run "${TRELLIS}" ingest corpus "${ROOT}/corpus" --format json
pass "exit 0"

step "ingest then retrieve pack"
run "${TRELLIS}" retrieve pack --intent "tomatoes sunlight watering" --format json
jcheck 'd["count"] >= 1' "the pack has no items from the ingested corpus"
pass "exit 0, $(jget 'd["count"]') items"

# ----------------------------------------------------------------- server
step "server"
HOME="${README_HOME}"
export HOME
PORT="$("${VPY}" -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
"${TRELLIS}" admin serve --port "${PORT}" >"${ROOT}/server.log" 2>&1 &
SERVER_PID=$!

# Poll until /healthz answers 200, the server dies, or the deadline passes.
http_status() {
    "${VPY}" -I -c '
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as r:
        print(r.status)
except Exception as exc:
    print(f"error: {exc}")
' "$1"
}
READY=""
POLLS=0
T0=${SECONDS}
while [ $((SECONDS - T0)) -lt "${SERVER_WAIT_SECS}" ]; do
    kill -0 "${SERVER_PID}" 2>/dev/null || break
    POLLS=$((POLLS + 1))
    if [ "$(http_status "http://127.0.0.1:${PORT}/healthz")" = "200" ]; then
        READY=1
        break
    fi
    sleep 0.5
done
if [ -z "${READY}" ]; then
    { echo "--- server.log (tail)"; tail -n 20 "${ROOT}/server.log"; } >&2
    if kill -0 "${SERVER_PID}" 2>/dev/null; then
        die "/healthz did not answer 200 on port ${PORT}: ${POLLS} polls over $((SECONDS - T0))s (limit ${SERVER_WAIT_SECS}s)"
    fi
    die "the server exited before /healthz answered"
fi
# `/` is the dashboard the README sends a new user to: it redirects to /ui/, which
# urllib follows, so 200 means the bundled static UI was served.
for path in /healthz /api/version /; do
    got="$(http_status "http://127.0.0.1:${PORT}${path}")"
    [ "${got}" = "200" ] || die "GET ${path} answered ${got}, not 200"
done
kill "${SERVER_PID}" || true
wait "${SERVER_PID}" 2>/dev/null || true
SERVER_PID=""
pass "/healthz, /api/version and / (the dashboard) answer 200 on port ${PORT}"

# -------------------------------------------------------------------- MCP
step "mcp"
HOME="${QUICKSTART_HOME}"
export HOME
# Frames are newline-delimited JSON-RPC on stdout, nothing else; a stray line
# there would break a real client, so a non-JSON line fails the step.
cat >"${ROOT}/mcp_probe.py" <<'PY'
import json, subprocess, sys

exe, floor = sys.argv[1], int(sys.argv[2])
proc = subprocess.Popen(
    [exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1
)


def send(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def reply(want_id):
    for line in proc.stdout:
        msg = json.loads(line)
        if msg.get("id") == want_id and "method" not in msg:
            return msg
    raise SystemExit(f"trellis-mcp closed stdout before answering id {want_id}")


try:
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "cold-install-check", "version": "0"}}})
    init = reply(1)
    result = init.get("result") or sys.exit(f"initialize failed: {init}")
    name = result.get("serverInfo", {}).get("name") or sys.exit(f"no serverInfo: {result}")
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    listed = reply(2)
    tools = (listed.get("result") or sys.exit(f"tools/list failed: {listed}"))["tools"]
    if len(tools) < floor:
        sys.exit(f"tools/list returned {len(tools)} tools, floor is {floor}")
    print(f"initialize ok (server {name}), {len(tools)} tools (floor {floor})")
finally:
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
PY
run timeout 60 "${VPY}" -I "${ROOT}/mcp_probe.py" "${VENV}/bin/trellis-mcp" "${MCP_MIN_TOOLS}"
pass "$(cat "${OUT}")"

echo "cold-install check passed"
