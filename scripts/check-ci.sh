#!/usr/bin/env bash
# Runs the same checks as CI (pytest, JS tests, hassfest, HACS) locally, via the same Docker
# images the GitHub Actions workflows use, so failures surface before a push instead of after.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== pytest =="
.venv/bin/pytest tests/

echo "== node --test =="
(cd frontend && node --test)

echo "== hassfest =="
docker run --rm -v "$(pwd)":/github/workspace -w /github/workspace ghcr.io/home-assistant/hassfest:latest

echo "== HACS =="
docker run --rm \
  -e "INPUT_CATEGORY=integration" \
  -e "GITHUB_REPOSITORY=Rom1-B/solar-planner-scheduler" \
  -e "GITHUB_REF=refs/heads/main" \
  -e "INPUT_GITHUB_TOKEN=$(gh auth token)" \
  -v "$(pwd)":/github/workspace -w /github/workspace \
  ghcr.io/hacs/action:main

echo "All checks passed."
