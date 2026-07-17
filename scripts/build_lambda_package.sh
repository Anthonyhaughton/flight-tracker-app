#!/usr/bin/env bash
# Builds the Lambda deployment zip that infra/lambda.tf's `lambda_zip_path`
# variable points at (default: dist/poller.zip, relative to the repo root).
#
# Installs src/'s runtime dependencies (from pyproject.toml's [project]
# dependencies -- NOT the pytest/respx dev extras) for the Lambda's exact
# runtime (Python 3.12, arm64/manylinux, matching lambda.tf's `runtime` and
# `architectures`), copies src/ and watchlist.yaml alongside them, and zips
# the result.
#
# This is a manual step, run before `terraform plan`/`apply` any time src/ or
# its dependencies change -- Terraform does not invoke pip/zip itself (no
# null_resource); a plain script you run and can read top to bottom is more
# transparent than a build hidden inside a Terraform provisioner.
#
#     scripts/build_lambda_package.sh
#     cd infra && terraform plan
#
# Requires Python 3.11+ on PATH (for tomllib) with a working pip -- e.g. run
# with the repo's .venv activated, same as the other scripts/ tools.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_ROOT/dist/build"
ZIP_PATH="$REPO_ROOT/dist/poller.zip"

if ! python3 -c 'import tomllib' 2>/dev/null; then
    echo "error: python3 on PATH has no tomllib (needs 3.11+)." >&2
    echo "Activate the repo's .venv first: source .venv/bin/activate" >&2
    exit 1
fi

# Runtime deps only -- read straight out of pyproject.toml so this list can't
# silently drift from the real dependency set (e.g. someone adding a package
# to [project.optional-dependencies].dev by mistake and it ending up in prod,
# or vice versa).
DEPS="$(python3 -c '
import tomllib
with open("'"$REPO_ROOT"'/pyproject.toml", "rb") as f:
    data = tomllib.load(f)
print("\n".join(data["project"]["dependencies"]))
')"

echo "Runtime dependencies (from pyproject.toml):"
echo "$DEPS"
echo

rm -rf "$BUILD_DIR" "$ZIP_PATH"
mkdir -p "$BUILD_DIR"

echo "Installing for linux/arm64, Python 3.12 (matches lambda.tf's runtime/architectures)..."
# --platform + --python-version + --only-binary=:all: cross-installs
# manylinux wheels for the Lambda's target, regardless of the host machine
# this script runs on (e.g. a local Mac).
python3 -m pip install \
    --target "$BUILD_DIR" \
    --platform manylinux2014_aarch64 \
    --implementation cp \
    --python-version 3.12 \
    --only-binary=:all: \
    --upgrade \
    --quiet \
    $DEPS

echo "Copying src/ and watchlist.yaml into the package..."
cp -R "$REPO_ROOT/src" "$BUILD_DIR/src"
cp "$REPO_ROOT/watchlist.yaml" "$BUILD_DIR/watchlist.yaml"

# Bytecode caches aren't needed at runtime -- just bloats the zip.
find "$BUILD_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "Zipping to ${ZIP_PATH#"$REPO_ROOT"/}..."
mkdir -p "$(dirname "$ZIP_PATH")"
(cd "$BUILD_DIR" && zip -r -X -q "$ZIP_PATH" .)

echo "Built ${ZIP_PATH#"$REPO_ROOT"/} ($(du -h "$ZIP_PATH" | cut -f1))"
