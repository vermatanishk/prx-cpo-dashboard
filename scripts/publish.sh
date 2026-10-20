#!/usr/bin/env bash
# Refresh the dashboard data from the latest pipeline run and push it to GitHub.
# Usage: scripts/publish.sh "message"      (run after Stages 00-17 have produced intermediate/)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PIPE="${PIPELINE_DIR:-$HOME/Desktop/CTO/pipeline}"        # the working pipeline checkout (holds raw/ + intermediate/)

(cd "$PIPE/scripts" && python3 33_export_dashboard_data.py)
cp "$PIPE/export/dashboard_long.csv" "$PIPE/export/labels.csv" "$PIPE/export/meta.json" "$REPO/data/"
rsync -a --exclude '__pycache__' --exclude '_adhoc_*' "$PIPE/scripts/" "$REPO/pipeline/scripts/"
rsync -a "$PIPE/reference/" "$REPO/pipeline/reference/"
cp "$PIPE/config/run_config.yaml" "$REPO/pipeline/config/run_config.yaml"

cd "$REPO"
git add -A
if git diff --cached --quiet; then echo "No changes to publish."; exit 0; fi
git commit -m "${1:-Refresh dashboard data}"
git push
echo "Pushed. In the Sheet: PRX CPO -> Refresh from GitHub (or wait for the 8am trigger)."
