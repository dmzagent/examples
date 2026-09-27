#!/usr/bin/env bash
# Set the platform up for this example through the Solution Manifest wheel,
# mint the agent's key, then print the configuration that attaches the
# platform's MCP server to your MCP host.
#
#   export DMZAGENT_API_KEY=ck_...        # a tenant_admin key for the workspace
#   export DMZ_APPROVED_BY=reviewer       # the second pair of eyes the platform requires to apply
#   ./setup.sh [--var posture=observe]
set -euo pipefail
cd "$(dirname "$0")"
: "${DMZAGENT_API_KEY:?set DMZAGENT_API_KEY to a tenant_admin API key for the workspace}"
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
  || $dmz keys mint solution.yaml --workspace agents --label "mcp agent" --write-env app/.env
echo
$dmz verify solution.yaml "$@" || echo "verify reported problems (see above)"
echo
echo "The agent's key is in app/.env. To attach the platform's MCP server to a host:"
echo
$dmz --env-file app/.env mcp config --client claude-code
echo
echo "Next:  python3 app/agent.py --script     # the scripted day, in the terminal"
echo "       python3 app/agent.py              # a conversation"
