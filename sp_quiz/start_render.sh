#!/bin/bash
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
exec gunicorn app:app --bind "0.0.0.0:${PORT:-8791}" --workers 1 --timeout 120
