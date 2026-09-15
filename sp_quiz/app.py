#!/usr/bin/env python3
"""
BYD Service Advisor Quiz — EN/ES fill-in-the-blank questionnaire.

Rebuilt 2026-09-15 after the original source repository (byd-tools) was deleted.
The front end (templates/quiz.html) is the page exactly as served live; the
backend reproduces the observed API contract and adds region tracking.

Uses Turso (hosted SQLite) — the same database as the tech quiz.
Every submission records the respondent's IP and its country/region/city.
Supports ?lang=en | ?lang=es (default: en).
"""

import random, os, json, logging, time, ipaddress
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from flask import Flask, render_template, jsonify, request, g, has_request_context

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Return-to-launcher support ──
#
# TQC (byd-tqc.onrender.com) links here with ?return=<its own URL> so the
# inspector can get back to the checklist item after answering. The value is
# attacker-controllable, so it is checked against an allowlist — otherwise this
# endpoint would be an open redirect that lets anyone bounce users off our
# domain via a crafted quiz link.
RETURN_HOST_ALLOWLIST = {"byd-tqc.onrender.com", "localhost", "127.0.0.1"}


def safe_return_url(raw):
    """Return a validated absolute http(s) URL to hand back to, or None.

    Anything not on RETURN_HOST_ALLOWLIST is dropped (the template then simply
    renders no return button).
    """
    if not raw:
        return None
    try:
        parts = urlparse(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if (parts.hostname or "") not in RETURN_HOST_ALLOWLIST:
        return None
    return raw


# ── Turso config (shared with the tech quiz) ──
TURSO_URL = os.environ.get("TURSO_URL",
    "https://byd-tech-quiz-xinpeng.aws-us-east-1.turso.io/v2/pipeline")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
if not TURSO_TOKEN:
    # Fallback: read from local token file
    token_file = os.path.expanduser("~/.turso_token")
    if os.path.exists(token_file):
        with open(token_file) as f:
            TURSO_TOKEN = f.read().strip()

_BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_BASE, "templates"))

LANGS = {"en": "English", "es": "Español"}
QUESTIONS_PER_QUIZ = 5
MAX_HISTORY_ROWS = 500      # keep the shared Turso table small


# ── Turso HTTP helpers ──
_turso_session = None


def _get_turso_session():
    """Lazy-init a requests.Session for connection reuse."""
    global _turso_session
    if _turso_session is None:
        _turso_session = requests.Session()
        _turso_session.headers.update({
            "Authorization": f"Bearer {TURSO_TOKEN}",
            "Content-Type": "application/json",
        })
    return _turso_session


def _turso_request(sql, params=None):
    """Execute a SQL statement via the Turso HTTP pipeline API."""
    stmt = {"sql": sql}
    if params:
        stmt["args"] = []
        for p in params:
            if p is None:
                stmt["args"].append({"type": "null"})
            elif isinstance(p, bool):
                stmt["args"].append({"type": "integer", "value": "1" if p else "0"})
            elif isinstance(p, str):
                stmt["args"].append({"type": "text", "value": p})
            elif isinstance(p, (int, float)):
                stmt["args"].append({"type": "integer", "value": str(p)})
            else:
                stmt["args"].append({"type": "text", "value": str(p)})

    body = {"requests": [{"type": "execute", "stmt": stmt}]}
    resp = _get_turso_session().post(TURSO_URL, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _turso_execute(sql, params=None):
    """Execute a single SQL statement, return a list of dicts."""
    result = _turso_request(sql, params)
    response = result["results"][0]
    if response["type"] != "ok":
        raise RuntimeError(f"Turso error: {response}")
    exec_result = response["response"]["result"]
    cols = [c["name"] for c in exec_result.get("cols", [])]
    rows = []
    for row in exec_result.get("rows", []):
        d = {}
        for i, col in enumerate(cols):
            v = row[i]
            if v["type"] == "null":
                d[col] = None
            elif v["type"] == "integer":
                d[col] = int(v.get("value", "0"))
            elif v["type"] == "float":
                d[col] = float(v.get("value", "0"))
            else:
                d[col] = v.get("value", "")
        rows.append(d)
    return rows


def _turso_batch(statements):
    """Execute several SQL statements in one pipeline request."""
    body = {"requests": []}
    for sql, params in statements:
        stmt = {"sql": sql}
        if params:
            stmt["args"] = []
            for p in params:
                if p is None:
                    stmt["args"].append({"type": "null"})
                elif isinstance(p, bool):
                    stmt["args"].append({"type": "integer", "value": "1" if p else "0"})
                elif isinstance(p, str):
                    stmt["args"].append({"type": "text", "value": p})
                elif isinstance(p, (int, float)):
                    stmt["args"].append({"type": "integer", "value": str(p)})
                else:
                    stmt["args"].append({"type": "text", "value": str(p)})
        body["requests"].append({"type": "execute", "stmt": stmt})

    resp = _get_turso_session().post(TURSO_URL, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── Client IP + region lookup ──
#
# Every submission records where it came from so the history can be broken
# down by region. The IP is read from the proxy headers (Cloudflare in front
# of Render), never from the request body, and is resolved to a human-readable
# country/region/city. Both steps are best-effort: if anything fails the quiz
# still saves the answers.

_GEO_CACHE = {}                 # ip -> (epoch_seconds, {"country","region","city"})
_GEO_TTL = 24 * 3600            # cache a resolved IP for a day
_GEO_TIMEOUT = 3                # seconds; keep the submit path snappy
_GEO_URL = "https://ipwho.is/{ip}"   # free, HTTPS, no API key

_LOCATION_COLUMNS = ("ip_address", "ip_country", "ip_region", "ip_city")


def _client_ip():
    """Best-effort client IP, honouring the proxies in front of the app."""
    for header in ("CF-Connecting-IP", "True-Client-IP", "X-Real-IP"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return (request.remote_addr or "").strip()


def _is_public_ip(ip):
    """True only for routable addresses (skips dev/local/private ranges)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_reserved
                or addr.is_link_local or addr.is_multicast or addr.is_unspecified)


def _geo_for_ip(ip):
    """Resolve an IP to {"country", "region", "city"}. Never raises."""
    blank = {"country": "", "region": "", "city": ""}
    if not ip or not _is_public_ip(ip):
        return blank

    cached = _GEO_CACHE.get(ip)
    if cached and (time.time() - cached[0]) < _GEO_TTL:
        return cached[1]

    info = dict(blank)
    try:
        # Plain requests.get on purpose: the Turso session carries an
        # Authorization header that must never be sent to a third party.
        resp = requests.get(_GEO_URL.format(ip=ip), timeout=_GEO_TIMEOUT)
        data = resp.json()
        if data.get("success") is not False:
            info = {
                "country": data.get("country") or "",
                "region": data.get("region") or "",
                "city": data.get("city") or "",
            }
    except Exception as exc:  # network hiccup, rate limit, malformed JSON...
        logger.warning(f"Geo lookup failed for {ip}: {exc}")
        if has_request_context():
            cc = (request.headers.get("CF-IPCountry") or "").strip()
            if cc and cc != "XX":
                info["country"] = cc

    _GEO_CACHE[ip] = (time.time(), info)
    return info


def _ensure_schema():
    """Add the IP/location columns to an existing history table.

    Idempotent: a database that already has the columns reports a
    'duplicate column' error, which is expected and ignored. Failure is
    non-fatal — the quiz keeps working, it just will not record locations
    until the migration succeeds.
    """
    for column in _LOCATION_COLUMNS:
        try:
            _turso_execute(
                f"ALTER TABLE service__quiz_history ADD COLUMN {column} TEXT"
            )
        except Exception as exc:
            message = str(exc).lower()
            if "duplicate column" not in message and "already exists" not in message:
                logger.warning(f"Schema migration for {column} failed: {exc}")


# ── Routes ──

@app.before_request
def set_lang():
    lang = request.args.get("lang", "en")
    if lang not in LANGS:
        lang = "en"
    g.lang = lang


@app.context_processor
def inject_lang():
    return {"lang": g.lang, "langs": LANGS}


def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/")
def index():
    return _no_cache(app.make_response(render_template(
        "quiz.html", return_url=safe_return_url(request.args.get("return")))))


@app.route("/api/health")
def api_health():
    try:
        rows = _turso_execute("SELECT COUNT(*) as cnt FROM service__quiz_bank")
        resp = app.make_response(jsonify({"db": "turso", "questions": rows[0]["cnt"]}))
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp
    except Exception as e:
        resp = app.make_response(jsonify({"db": "turso", "error": str(e)}))
        resp.status_code = 500
        return _no_cache(resp)


def _build_question(row, lang):
    """Shape a service__quiz_bank row into the API question object."""
    if lang == "es":
        question = row.get("question_es") or row.get("question") or ""
        answer = row.get("answer_es") or row.get("answer") or ""
    else:
        question = row.get("question") or ""
        answer = row.get("answer") or ""
    return {
        "sn": row.get("sn") or "",
        "question": question,
        "answer": answer,
        "category": row.get("category") or "",
    }


@app.route("/api/questions")
def api_questions():
    """Return 5 random service__quiz_bank questions in the requested language."""
    try:
        all_rows = _turso_execute("SELECT id FROM service__quiz_bank")
        if not all_rows:
            return _no_cache(app.make_response(jsonify({"questions": []})))

        ids = [r["id"] for r in all_rows]
        selected = random.sample(ids, min(QUESTIONS_PER_QUIZ, len(ids)))
        placeholders = ",".join(["?" for _ in selected])
        rows = _turso_execute(
            f"SELECT * FROM service__quiz_bank WHERE id IN ({placeholders})",
            selected,
        )
        questions = [_build_question(r, g.lang) for r in rows]
        random.shuffle(questions)
        return _no_cache(app.make_response(jsonify({"questions": questions})))
    except Exception as e:
        logger.error(f"Questions error: {e}")
        resp = app.make_response(jsonify({"error": "Failed to load questions"}))
        resp.status_code = 500
        return _no_cache(resp)


def _cleanup_history():
    """Trim the oldest history rows so the shared Turso table stays small."""
    try:
        rows = _turso_execute("SELECT COUNT(*) as cnt FROM service__quiz_history")
        count = rows[0]["cnt"]
        if count <= MAX_HISTORY_ROWS:
            return
        _turso_execute(
            """DELETE FROM service__quiz_history WHERE id IN (
                   SELECT id FROM service__quiz_history ORDER BY id ASC LIMIT ?)""",
            [count - MAX_HISTORY_ROWS],
        )
        logger.info(f"Turso cleanup: trimmed history to {MAX_HISTORY_ROWS} rows")
    except Exception as e:
        logger.warning(f"Turso cleanup failed (non-fatal): {e}")


@app.route("/api/submit", methods=["POST"])
def api_submit():
    """Save a finished quiz session, with the respondent's region."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    answers = data.get("answers", [])

    if not session_id or not answers:
        return jsonify({"ok": False, "error": "Missing session_id or answers"}), 400

    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        ip = _client_ip()
        geo = _geo_for_ip(ip)
        batch = []
        for a in answers:
            passed = a.get("passed")
            if passed is None:
                passed = a.get("is_correct")
            batch.append((
                """INSERT INTO service__quiz_history
                   (session_id, question_sn, question, answer, is_correct, created_at,
                    ip_address, ip_country, ip_region, ip_city)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    session_id,
                    a.get("sn", ""),
                    a.get("question", ""),
                    a.get("answer", ""),
                    1 if passed else 0,
                    now,
                    ip,
                    geo["country"],
                    geo["region"],
                    geo["city"],
                ],
            ))
        _turso_batch(batch)
        _cleanup_history()
        return jsonify({"ok": True, "saved": len(answers)})
    except Exception as e:
        logger.error(f"Submit error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/history")
def api_history():
    """Recent sessions with score and region.

    The raw ip_address is deliberately not returned: this endpoint is public.
    """
    limit = request.args.get("limit", 10, type=int)
    try:
        rows = _turso_execute(
            """SELECT session_id,
                      COUNT(*)              AS total,
                      SUM(is_correct)       AS correct,
                      MAX(created_at)       AS time,
                      MAX(ip_country)       AS ip_country,
                      MAX(ip_region)        AS ip_region,
                      MAX(ip_city)          AS ip_city
               FROM service__quiz_history
               GROUP BY session_id
               ORDER BY time DESC
               LIMIT ?""",
            [limit],
        )
        history = [{
            "session_id": r["session_id"],
            "time": r["time"],
            "total": r["total"],
            "correct": r["correct"] or 0,
            "ip_country": r.get("ip_country") or "",
            "ip_region": r.get("ip_region") or "",
            "ip_city": r.get("ip_city") or "",
        } for r in rows]
        return jsonify({"history": history})
    except Exception as e:
        logger.error(f"History error: {e}")
        return jsonify({"history": []})


# Apply the IP/location migration once per process at startup.
try:
    _ensure_schema()
except Exception as _exc:  # never block boot on a migration problem
    logger.warning(f"Startup schema check skipped: {_exc}")


if __name__ == "__main__":
    print("BYD Service Advisor Quiz — http://localhost:8791", flush=True)
    app.run(host="0.0.0.0", port=8791, debug=True)
