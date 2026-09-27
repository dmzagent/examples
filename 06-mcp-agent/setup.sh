#!/usr/bin/env bash
# Set the platform up for this example: plan, apply, verify, then print the
# configuration that attaches the platform's MCP server to your MCP host.
#
#   export DMZAGENT_API_KEY=ck_...        # a tenant_admin key for the workspace
#   ./setup.sh [--var posture=observe]
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
echo "The agent's key is in app/.env. To attach the platform's MCP server to a host:"
echo
$dmz --env-file app/.env mcp config --client claude-code
echo
echo "Next:  python3 app/agent.py --script     # the scripted day, in the terminal"
echo "       python3 app/agent.py              # a conversation"
