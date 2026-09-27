#!/usr/bin/env bash
# Set the platform up for this example: plan, apply, verify, install the SDK.
#
#   export DMZAGENT_API_KEY=ck_...        # a tenant_admin key for the workspace
#   ./setup.sh [--var posture=observe]
set -euo pipefail
cd "$(dirname "$0")"
: "${DMZAGENT_API_KEY:?set DMZAGENT_API_KEY to a tenant_admin API key for the workspace}"
export PYTHONPATH="$PWD/../giaas${PYTHONPATH:+:$PYTHONPATH}"

python3 -m giaas plan governance.py "$@"
echo
python3 -m giaas apply governance.py --env-file app/.env "$@"
echo
python3 -m giaas verify governance.py "$@" || echo "verify reported problems (see above)"
echo
echo "Installing the dmzagent SDK…"
python3 -m pip install --quiet -r requirements.txt
echo
echo "Next:  ollama pull llama3.1   (or set OLLAMA_MODEL)"
echo "       python3 app/chat.py    then open http://localhost:8001"
