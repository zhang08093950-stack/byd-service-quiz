# BYD Service Advisor Quiz

EN/ES fill-in-the-blank questionnaire for BYD Service Advisors, served at
<https://service-quiz.onrender.com/>.

This repository is a faithful rebuild (2026-09-15) of the app that used to live
in `byd-tools`, which was deleted. The front end in `sp_quiz/templates/quiz.html`
is the page exactly as it was served live; the backend reproduces the observed
API contract and adds **region tracking**.

## Repository layout

```
sp_quiz/                 <- the application
├── app.py               Flask backend
├── templates/quiz.html  EN/ES quiz page (inline CSS + JS)
├── requirements.txt
├── Procfile             web: ./start_render.sh
└── start_render.sh      gunicorn app:app on $PORT

Procfile                 <- root launcher, used when Root Directory is empty
start_render.sh          gunicorn --chdir sp_quiz app:app
requirements.txt
README.md
```

The real application lives in `sp_quiz/` because the existing Render service
`service-quiz` has its **Root Directory** set to `sp_quiz` (inherited from the
old `byd-tools` repository).

The three root-level files are a thin launcher that loads the same code with
`gunicorn --chdir sp_quiz`; nothing is duplicated. The service therefore builds
whether the Root Directory is left empty **or** set to `sp_quiz`. If you only
ever use one setting, you can delete the launcher files for the other.

## What it does

- Serves a self-contained page (inline CSS + JS, no build step).
- `GET /api/questions` returns 5 random questions from `service__quiz_bank`,
  localised to English or Spanish via `?lang=`.
- The advisor types each answer, sees the correct answer, and self-marks
  Correct / Incorrect.
- At the end the whole session is posted to `POST /api/submit`.
- Every stored answer row records the respondent's **IP and region**.

## Region tracking

On submit the server:

1. reads the client IP from the proxy headers — `CF-Connecting-IP`, then
   `True-Client-IP` / `X-Real-IP`, then the first entry of `X-Forwarded-For`.
   The IP is never taken from the request body, so it cannot be spoofed by the
   browser;
2. resolves it to country / region / city via the free
   [ipwho.is](https://ipwho.is) lookup (results cached in memory for 24 h);
3. stores `ip_address`, `ip_country`, `ip_region`, `ip_city` on every row.

Both steps are best-effort: if the lookup fails, the answers are still saved and
the location columns stay empty.

`GET /api/history` returns the region fields but **not** the raw IP, because the
endpoint is unauthenticated. Query Turso directly if you need the IPs.

## API

| Route | Behaviour |
| --- | --- |
| `GET /` | The quiz page. `?lang=en` (default) or `?lang=es`. |
| `GET /api/health` | `{"db": "turso", "questions": 88}` |
| `GET /api/questions?lang=` | `{"questions": [{"sn", "question", "answer", "category"}, ...]}` — 5 random rows |
| `POST /api/submit` | body `{"session_id": "...", "answers": [{"sn", "question", "answer", "passed"}, ...]}` → `{"ok": true, "saved": N}` |
| `GET /api/history?limit=N` | `{"history": [{"session_id", "time", "total", "correct", "ip_country", "ip_region", "ip_city"}, ...]}` |

## Database

Turso (hosted SQLite), shared with the tech quiz
(`byd-tech-quiz-xinpeng.aws-us-east-1.turso.io`).

- reads `service__quiz_bank` (columns: `sn`, `question`, `answer`, `category`,
  `question_es`, `answer_es`)
- writes `service__quiz_history` (columns: `session_id`, `question_sn`,
  `question`, `answer`, `is_correct`, `created_at`, plus the four `ip_*` columns)

The `ip_*` columns are added automatically at startup by an idempotent
migration, so a database created before the change is upgraded in place.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `TURSO_URL` | the shared Turso pipeline URL | Database endpoint |
| `TURSO_AUTH_TOKEN` | read from `~/.turso_token` locally | Turso auth |
| `PORT` | `8791` | Bind port on Render |

## Running locally

```bash
cd sp_quiz
pip install -r requirements.txt
export TURSO_AUTH_TOKEN=...      # or put it in ~/.turso_token
python3 app.py                   # http://localhost:8791
```

## Deploying on Render

- **Root Directory:** `sp_quiz` — or leave it **empty**; both work.
- **Start Command:** leave empty so the `Procfile` is used. If a custom command
  is set, make it `gunicorn app:app` (with Root Directory `sp_quiz`) or
  `gunicorn --chdir sp_quiz app:app` (with Root Directory empty).
- Build: Python.
- Set `TURSO_AUTH_TOKEN` in the service environment (the token itself is never
  committed).
- Auto-deploy on push to `main`.
- If a build still reports `Root directory "sp_quiz" does not exist` after this
  commit, use **Clear build cache & deploy** in Render — the directory is
  present in the repository, so that error means a stale build cache.
