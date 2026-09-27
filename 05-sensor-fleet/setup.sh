#!/usr/bin/env bash
# Set the platform up for this example: plan, apply, verify, install the SDK.
#
#   export DMZAGENT_API_KEY=ck_...   # a tenant_admin key for a LOGIC workspace
#   ./setup.sh [--var posture=observe]
set -euo pipefail
cd "$(dirname "$0")"
: "${DMZAGENT_API_KEY:?set DMZAGENT_API_KEY to a tenant_admin API key for the logic workspace}"
export PYTHONPATH="$PWD/../cli${PYTHONPATH:+:$PYTHONPATH}"
dmz="python3 -m dmz"          # the same as `dmz` once `pip install ../cli` has run

$dmz plan governance.py "$@"
echo
$dmz apply governance.py --write-env app/.env "$@"
echo
$dmz verify governance.py "$@" || echo "verify reported problems (see above)"
echo
echo "Installing the dmzagent SDK for the controller's breaker checks…"
python3 -m pip install --quiet -r requirements.txt
echo
echo "Next:  python3 app/fleet.py            # stream telemetry, watch the breakers"
