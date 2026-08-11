#!/bin/bash
# package.sh — Build the SCF deployment zip.
#
# Usage:  bash package.sh
# Output: tc-billing-processor.zip (ready to upload to Tencent Cloud SCF)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT="${SCRIPT_DIR}/tc-billing-processor.zip"

cd "$SCRIPT_DIR"

# Build the zip with only the files SCF needs at the root.
# The SCF handler entry point is: index.main_handler
zip -j "$OUTPUT" index.py

echo "==> Deployment package ready: $OUTPUT"
echo "    Upload this ZIP to the SCF console (Function Code → Local ZIP File)."
echo "    Handler: index.main_handler"
