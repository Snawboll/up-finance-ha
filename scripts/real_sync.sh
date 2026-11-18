#!/bin/bash
# One-off local sync against the real Up API (2Up accounts only).
# The token is read from the keyboard and never written to disk.
set -euo pipefail
cd "$(dirname "$0")/../up_finance"
read -rsp "Paste your Up token (hidden), then press Enter: " UP_TOKEN
echo
echo "Syncing 2Up accounts... (first run downloads full history)"
UP_TOKEN="$UP_TOKEN" ACCOUNT_SCOPE=joint DATA_DIR=.dev-data OPTIONS_FILE=.dev-data/options.json python3 -m app.main --once > .dev-data-sync.log 2>&1 \
  && echo "Done. Tell Claude it's finished." \
  || { echo "Sync failed - last lines of the log:"; tail -5 .dev-data-sync.log | grep -v -i token; }
