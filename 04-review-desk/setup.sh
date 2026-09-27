#!/usr/bin/env bash
# Set the platform up for this example: plan, apply, verify, install the SDK.
#
#   export DMZAGENT_API_KEY=ck_...        # a tenant_admin key for the workspace
#   ./setup.sh [--var posture=observe] [--var remediation_url=https://ops.example.com/hooks/remediate]
set -euo pipefail
cd "$(dirname "$0")"
: "${DMZAGENT_API_KEY:?set DMZAGENT_API_KEY to a tenant_admin API key for the workspace}"
export PYTHONPATH="$PWD/../cli${PYTHONPATH:+:$PYTHONPATH}"
dmz="python3 -m dmz"          # the same as `dmz` once `pip install ../cli` has run

$dmz plan governance.py "$@"
echo
$dmz apply governance.py --write-env app/.env "$@"
echo
$dmz verify governance.py "$@" || echo "verify reported problems (see above)"
echo
echo "Installing the dmzagent SDK for the simulator…"
python3 -m pip install --quiet -r requirements.txt
echo
echo "Next:  python3 app/desk.py            # http://localhost:8002"
echo "       python3 app/simulate.py --loop # scripted agent traffic, in another terminal"
