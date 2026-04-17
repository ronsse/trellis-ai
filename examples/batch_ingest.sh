#!/usr/bin/env bash
# Batch-ingest a directory of trace JSON files through the CLI.
#
# Each file in the directory should be a single valid trace JSON
# (see docs/agent-guide/trace-format.md).
#
# Usage:
#   ./examples/batch_ingest.sh ./traces/
#
# Output is one line of JSON per ingest, suitable for piping into jq.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <traces-dir>" >&2
  exit 64
fi

dir="$1"
if [[ ! -d "$dir" ]]; then
  echo "not a directory: $dir" >&2
  exit 66
fi

shopt -s nullglob
ok=0
fail=0
for f in "$dir"/*.json; do
  if trellis ingest trace --file "$f" --format json; then
    ok=$((ok + 1))
  else
    fail=$((fail + 1))
    echo "FAILED: $f" >&2
  fi
done

echo "ingested=$ok failed=$fail" >&2
