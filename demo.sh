#!/usr/bin/env bash
# ProofHeal demo: record on v1 → heal on v2 → refuse on v2-broken → open the proof report.
set -uo pipefail
cd "$(dirname "$0")"
PH="${PROOFHEAL_BIN:-proofheal}"
command -v "$PH" >/dev/null || PH=".venv/bin/proofheal"

bold() { printf "\n\033[1m%s\033[0m\n" "$*"; }

if [ -z "${MODEL_API_KEY:-}${PROOFHEAL_API_KEY:-}" ]; then
  echo "MODEL_API_KEY is not set. export MODEL_API_KEY=<your Meta Model API key> and re-run." >&2
  exit 2
fi

bold "1/3  Record success-run memory on the original UI (v1)"
"$PH" record --variant v1 || { echo "recording failed"; exit 1; }

bold "2/3  Restyled UI (v2): tests break → ProofHeal repairs them with proof"
"$PH" run --variant v2 --heal
V2=$?
V2_REPORT=$(python3 -c 'import json;print(json.load(open("proof_reports/latest.json"))["report"])')

bold "3/3  Broken checkout (v2-broken): ProofHeal must refuse to hide it"
"$PH" run --variant v2-broken --heal
BROKEN=$?
BROKEN_REPORT=$(python3 -c 'import json;print(json.load(open("proof_reports/latest.json"))["report"])')

bold "Summary"
"$PH" report "$V2_REPORT"
"$PH" report "$BROKEN_REPORT"
echo
[ $V2 -eq 0 ] && echo "✔ v2: suite green after proven repairs" || echo "✘ v2: suite still failing (exit $V2)"
[ $BROKEN -ne 0 ] && echo "✔ v2-broken: checkout failure exposed, not hidden (exit $BROKEN)" || echo "✘ v2-broken: unexpectedly green"

if [ "${NO_OPEN:-0}" != "1" ]; then
  "$PH" report --open "$V2_REPORT" >/dev/null
  "$PH" report --open "$BROKEN_REPORT" >/dev/null
fi
[ $V2 -eq 0 ] && [ $BROKEN -ne 0 ]
