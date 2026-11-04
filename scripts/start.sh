#!/usr/bin/env bash
# Start local Arcane end-to-end: venv, .env, Supabase, then medical ETL.
# Skips setup steps that are already done.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! docker info >/dev/null 2>&1; then
  echo "Start Docker Desktop first, then re-run."
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "==> Creating .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c 'import pipeline, psycopg, pdfplumber' >/dev/null 2>&1; then
  echo "==> Installing deps"
  pip install -e ".[dev]"
fi

if [[ ! -f .env ]]; then
  echo "==> Creating .env"
  cp .env.example .env
fi

if ! nc -z -G 1 127.0.0.1 54322 >/dev/null 2>&1; then
  echo "==> Starting Supabase"
  npx --yes supabase start
else
  echo "==> Supabase already running"
fi

echo "==> Extract + transform"
python -m pipeline.cli run --segment alberta-medical-benefits --phase extract,transform

echo "==> Transform + load"
python -m pipeline.cli run --segment alberta-medical-benefits --phase transform,load

echo
echo "Done."
echo "  Postgres: postgresql://postgres:postgres@127.0.0.1:54322/postgres"
echo "  Studio:   http://127.0.0.1:54323"
