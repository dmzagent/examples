#!/usr/bin/env bash
# Set the platform up for this example through the Solution Manifest wheel,
# then install the SDK the controller uses for its breaker checks.
#
#   export DMZAGENT_API_KEY=ck_...        # a tenant_admin key for a LOGIC workspace
#   export DMZ_APPROVED_BY=reviewer       # the second pair of eyes the platform requires to apply
#   ./setup.sh [--var posture=observe]
set -euo pipefail
cd "$(dirname "$0")"
: "${DMZAGENT_API_KEY:?set DMZAGENT_API_KEY to a tenant_admin API key for the logic workspace}"
: "${DMZ_APPROVED_BY:?set DMZ_APPROVED_BY to the reviewer approving this apply (maker-checker); in CI it is the merger}"
export PYTHONPATH="$PWD/../cli${PYTHONPATH:+:$PYTHONPATH}"
dmz="python3 -m dmz"

$dmz validate solution.yaml "$@"
echo
$dmz plan solution.yaml "$@"
echo
$dmz apply solution.yaml --yes --write-env app/.env "$@"
echo
grep -q '^DMZAGENT_APP_KEY=' app/.env 2>/dev/null \
  || $dmz keys mint solution.yaml --workspace fleet --label "fleet controller" --write-env app/.env
echo
$dmz verify solution.yaml "$@" || echo "verify reported problems (see above)"
echo
echo "Installing the dmzagent SDK for the controller's breaker checks…"
python3 -m pip install --quiet -r requirements.txt
echo
echo "Next:  python3 app/fleet.py            # stream telemetry, watch the breakers"
