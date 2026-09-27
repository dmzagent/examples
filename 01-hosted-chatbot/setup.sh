#!/usr/bin/env bash
# Set the platform up for this example: plan, apply, verify.
#
#   export DMZAGENT_API_KEY=ck_...        # a tenant_admin key for the workspace
#   export DMZAGENT_BASE_URL=...          # optional; default https://api.dmzagent.com
#   ./setup.sh [--var posture=observe] [--var site_domain=support.example.com]
#
# Everything the site server needs lands in app/.env (mode 0600).
set -euo pipefail
cd "$(dirname "$0")"
: "${DMZAGENT_API_KEY:?set DMZAGENT_API_KEY to a tenant_admin API key for the workspace}"
export PYTHONPATH="$PWD/../cli${PYTHONPATH:+:$PYTHONPATH}"
dmz="python3 -m dmz"          # the same as `dmz` once `pip install ../cli` has run

$dmz plan governance.py "$@"
echo
$dmz apply governance.py --write-env app/.env "$@"
echo
if ! $dmz verify governance.py "$@"; then
  echo
  echo "verify reported problems (see above). The site still runs; policies on tags"
  echo "from a Canon that is not installed will not fire until it is."
fi
echo
echo "Next:  python3 app/serve.py    then open http://localhost:8000"
