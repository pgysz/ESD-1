#!/usr/bin/env bash
# Applies the ILM policy (delete logs after 3 days) and the index template
# that binds every "attendance-logs-*" index to that policy.
#
# Run this ONCE after Elasticsearch is up (docker compose up -d), before or
# after Filebeat starts creating indices — it's safe to re-run any time.
#
# Usage: ./setup-ilm.sh

set -e

ES_HOST="${ES_HOST:-http://localhost:9200}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Applying ILM policy 'attendance-logs-policy' (delete after 3 days)..."
curl -s -X PUT "$ES_HOST/_ilm/policy/attendance-logs-policy" \
  -H "Content-Type: application/json" \
  -d @"$SCRIPT_DIR/ilm-policy.json" | python3 -m json.tool

echo
echo "Applying index template binding attendance-logs-* to that policy..."
curl -s -X PUT "$ES_HOST/_index_template/attendance-logs-template" \
  -H "Content-Type: application/json" \
  -d @"$SCRIPT_DIR/index-template.json" | python3 -m json.tool

echo
echo "Done. New attendance-logs-* indices will now be deleted after 3 days."
