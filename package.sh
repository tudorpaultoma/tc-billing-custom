#!/bin/bash
# package.sh — Build the SCF deployment zip with billing SDK + requests.
#
# The tencentcloud-sdk-python package bundles ALL product SDKs (~330 MB).
# We extract only what we need (common, billing, region) from the source
# tarball, add requests + its deps, and zip it up.
#
# Usage:  bash package.sh
# Output: tc-billing-processor.zip (ready to upload to Tencent Cloud SCF)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"
OUTPUT="${SCRIPT_DIR}/tc-billing-processor.zip"
TMP_DIR="$(mktemp -d)"

cd "$SCRIPT_DIR"

# Clean previous build
rm -rf "$BUILD_DIR" "$OUTPUT"
mkdir -p "$BUILD_DIR/tencentcloud"

echo "==> Downloading tencentcloud-sdk-python source..."
pip download tencentcloud-sdk-python --no-deps --no-binary :all: -d "$TMP_DIR" >/dev/null 2>&1

echo "==> Extracting billing/common/region modules..."
TARBALL=$(ls "$TMP_DIR"/tencentcloud_sdk_python-*.tar.gz)
tar xzf "$TARBALL" -C "$TMP_DIR"
SRC_DIR=$(ls -d "$TMP_DIR"/tencentcloud_sdk_python-*/tencentcloud)

cp "$SRC_DIR/__init__.py" "$BUILD_DIR/tencentcloud/"
cp -r "$SRC_DIR/common" "$BUILD_DIR/tencentcloud/"
cp -r "$SRC_DIR/billing" "$BUILD_DIR/tencentcloud/"
cp -r "$SRC_DIR/region" "$BUILD_DIR/tencentcloud/"

echo "==> Installing requests + deps..."
pip install --target="$BUILD_DIR" requests >/dev/null 2>&1

echo "==> Copying handler..."
cp index.py "$BUILD_DIR/"

echo "==> Cleaning up caches and metadata..."
find "$BUILD_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name '*.pyc' -delete 2>/dev/null || true
find "$BUILD_DIR" -name '*.pyi' -delete 2>/dev/null || true
find "$BUILD_DIR" -name '*.dist-info' -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name 'bin' -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name '*.so' -delete 2>/dev/null || true

echo "==> Creating zip..."
cd "$BUILD_DIR"
zip -r "$OUTPUT" . -x '__pycache__/*' '*.pyc' >/dev/null

cd "$SCRIPT_DIR"
rm -rf "$BUILD_DIR" "$TMP_DIR"

echo "==> Deployment package ready: $OUTPUT"
ls -lh "$OUTPUT"
echo ""
echo "    Upload this ZIP to the SCF console (Function Code → Local ZIP File)."
echo "    Handler: index.main_handler"
echo "    Runtime: Python 3.9+"
echo "    Memory:  512 MB (recommended)"
echo "    Timeout: 300 seconds"
