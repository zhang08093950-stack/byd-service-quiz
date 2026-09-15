#!/bin/bash
# Root-directory launcher.
#
# The application lives in sp_quiz/. This wrapper exists so the service builds
# successfully whether Render's "Root Directory" setting is empty or set to
# "sp_quiz" — the real code is not duplicated, it is loaded from sp_quiz/.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
exec gunicorn --chdir sp_quiz app:app --bind "0.0.0.0:${PORT:-8791}" --workers 1 --timeout 120
