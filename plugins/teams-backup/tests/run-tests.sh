#!/usr/bin/env bash
# Offline unit tests for teams-backup (no browser, no network). Needs Python 3.8+.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -W error::ResourceWarning -m unittest discover -s . -p 'test_*.py' -v
