#!/usr/bin/env bash
# One-time dev environment setup. Run after cloning the repo.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Wire git to use the committed hooks directory
git config core.hooksPath .githooks
echo "OK: git hooks installed (.githooks/pre-push)"
