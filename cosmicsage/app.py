"""CosmicSage backend — hybrid model quota, uid credits, UTR claims (QR payments).

Render env vars required:
  ANTHROPIC_API_KEY  - Anthropic API key (console.anthropic.com)
  DATABASE_URL       - Neon Postgres connection string
  ADMIN_TOKEN        - secret for /admin audit page
Optional:
  DAILY_QUESTION_LIMIT (per-IP backstop, default 40)
"""
import os
import time
from collections import defaultdict

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL_SONNET = "claude-sonnet-4-6"   # first free question + all paid questions
MODEL_HAIKU = "claude-haiku-4-5-20251001"  # free questions 2-3
FREE_LIMIT = 3
PACK_PRICE = 69
PACK_QUESTIONS = 9

DAILY_LIMIT = int(os.environ.get("DAILY_QUESTION_LIMIT", "40"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Always-free family/VIP names (normalized). Override with FREE_NAMES env var
# (comma-separated) to keep names out of a public repo.
def _norm_name(s):
    return " ".join(str(s).lower().split())


FREE_NAMES = {_norm_name(n) for n in os.environ.get(
    "FREE_NAMES", "Kaushik VM,Keerthana VS,Srivatsa VK").split(",") if n.strip()}

_usage = defaultdict(list)


def _ip_allowed(ip: str) -> bool:
    now = time.time()
    _usage[ip] = [t for t in _usage[ip] if now - t < 86400]
    if len(_usage[ip]) >= DAILY_LIMIT:
        return False
    _usage[ip].append(now)
    return True


# ---------------- database ----------------
_mem_users, _mem_claims = {}, {}  # dev fallback when DATABASE_URL is absent


def db():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        app.logger.warning("DATABASE_URL not set — using in-memory store (data lost on restart).")
        return
    with db() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS users(
            uid TEXT PRIMARY KEY, free_used INT NOT NULL DEFAULT 0,
            credits INT NOT NULL DEFAULT 0, created TIMESTAMPTZ DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS claims(
            utr TEXT PRIMARY KEY, uid TEXT NOT NULL, amount INT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created TIMESTAMPTZ DEFAULT now())""")


init_db()


def get_user(uid):
    if not DATABASE_URL:
        return _mem_users.setdefault(uid, {"free_used": 0, "credits": 0})
    with db() as c, c.cursor() as cur:
        cur.execute("INSERT INTO users(uid) VALUES(%s) ON CONFLICT(uid) DO NOTHING", (uid,))
        cur.execute("SELECT free_used, credits FROM users WHERE uid=%s", (uid,))
        f, cr = cur.fetchone()
        return {"free_used": f, "credits": cr}


def bump_user(uid, free_delta=0, credit_delta=0):
    if not DATABASE_URL:
        u = _mem_users.setdefault(uid, {"free_used": 0, "credits": 0})
        u["free_used"] += free_delta
        u["credits"] = max(0, u["credits"] + credit_delta)
        return u
    with db() as c, c.cursor() as cur:
        cur.execute("""UPDATE users SET free_used=free_used+%s,
            credits=GREATEST(0, credits+%s) WHERE uid=%s
            RETURNING free_used, credits""", (free_delta, credit_delta, uid))
        f, cr = cur.fetchone()
        return {"free_used": f, "credits": cr}


def status_payload(u):
    return {"free_left": max(0, FREE_LIMIT - u["free_used"]), "credits": u["credits"],
            "pack_price": PACK_PRICE, "pack_questions": PACK_QUESTIONS}


SYSTEM_PROMPT = """You are CosmicSage, a warm, wise Vedic astrologer who explains the
stars the way a caring elder would — simply, clearly, and personally.

LANGUAGE: Reply ONLY in this language: {language}. If it is not English, write naturally
in that language's own script. Keep graha names recognisable (Guru/Jupiter, Shani/Saturn).

DEPTH — this is critical, never be generic: You have the person's exact chart below. Every
claim you make MUST be tied to a SPECIFIC placement you can see in their data — a named
planet in a named house or sign or nakshatra, their current mahadasha/antardasha, a dosha,
or a live transit. If you cannot tie a statement to their actual chart, do not say it. Two
different people must never be able to receive the same reading. Read the combinations
(e.g. 5th-house lord placement for children, 10th lord and Saturn for career, Venus and
7th house for marriage), not single planets in isolation.

FORMAT — easy to read:
- Open with one warm sentence naming what their chart shows on this topic.
- Then 4-7 short bullet points. Start each bullet with "• ". Each bullet states ONE
  insight and names the placement behind it in plain words, e.g.
  "• Your 10th house is ruled by Mars, sitting strong in your 1st house — leadership and
  initiative come naturally; you do best when you lead rather than follow."
- Close with 1-2 practical, doable suggestions, each on a "• " bullet.
- Keep total length 200-320 words. No markdown stars, no headings — just the opening line
  and "• " bullets.

Frame everything as tendencies and timing, never fixed fate. Be encouraging and honest.
For health, money, or legal topics, add a gentle bullet noting the chart guides but
doctors, advisors and lawyers decide. For any remedy, prefer simple, free or low-cost
practices (mantra, charity, discipline) and never use fear.

{chart}"""


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/health")
def health():
    db_ok = False
    if DATABASE_URL:
        try:
            with db() as c, c.cursor() as cur:
                cur.execute("SELECT 1")
                db_ok = True
        except Exception:
            db_ok = False
    return {"ok": True, "key_configured": bool(ANTHROPIC_API_KEY), "db_connected": db_ok}


@app.get("/api/status")
def api_status():
    uid = (request.args.get("uid") or "").strip()
    if not (8 <= len(uid) <= 64):
        return jsonify(error="Bad uid."), 400
    p = status_payload(get_user(uid))
    p["vip"] = _norm_name(request.args.get("name", "")) in FREE_NAMES
    return jsonify(p)


@app.post("/api/claim")
def api_claim():
    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", "")).strip()
    utr = "".join(ch for ch in str(data.get("utr", "")) if ch.isdigit())
    if not (8 <= len(uid) <= 64):
        return jsonify(error="Bad uid."), 400
    if len(utr) != 12:
        return jsonify(error="A UPI UTR is exactly 12 digits — check your payment app's transaction details."), 400
    get_user(uid)
    if not DATABASE_URL:
        if utr in _mem_claims:
            return jsonify(error="This UTR has already been used."), 409
        _mem_claims[utr] = {"uid": uid, "amount": PACK_PRICE, "status": "active"}
    else:
        with db() as c, c.cursor() as cur:
            cur.execute("SELECT 1 FROM claims WHERE utr=%s", (utr,))
            if cur.fetchone():
                return jsonify(error="This UTR has already been used."), 409
            cur.execute("INSERT INTO claims(utr, uid, amount) VALUES(%s,%s,%s)", (utr, uid, PACK_PRICE))
    u = bump_user(uid, credit_delta=PACK_QUESTIONS)
    return jsonify(message=f"Unlocked {PACK_QUESTIONS} questions. Thank you for supporting CosmicSage! ♥",
                   status=status_payload(u))


@app.post("/api/ask")
def ask():
    if not ANTHROPIC_API_KEY:
        return jsonify(error="Server is missing ANTHROPIC_API_KEY."), 500
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0]
    if not _ip_allowed(ip):
        return jsonify(error="Daily question limit reached for this network — please return tomorrow."), 429

    data = request.get_json(silent=True) or {}
    chart = str(data.get("chart", ""))[:8000]
    history = data.get("history", [])
    uid = str(data.get("uid", "")).strip()
    if not chart or not isinstance(history, list) or not history:
        return jsonify(error="Missing chart or question."), 400
    if not (8 <= len(uid) <= 64):
        return jsonify(error="Missing user id — please regenerate your chart."), 400

    u = get_user(uid)
    vip = _norm_name(data.get("name", "")) in FREE_NAMES
    if vip:
        kind, model, max_tokens = "vip", MODEL_SONNET, 1500
    elif u["free_used"] < FREE_LIMIT:
        kind = "free"
        model = MODEL_SONNET if u["free_used"] == 0 else MODEL_HAIKU
        max_tokens = 1500 if u["free_used"] == 0 else 900
    elif u["credits"] > 0:
        kind, model, max_tokens = "paid", MODEL_SONNET, 1500
    else:
        return jsonify(error="quota", paywall=True,
                       message=f"You've used your free questions. Unlock {PACK_QUESTIONS} more for ₹{PACK_PRICE}.",
                       status=status_payload(u)), 402

    msgs = []
    for m in history[-20:]:
        role, content = m.get("role"), str(m.get("content", ""))[:2000]
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    if not msgs or msgs[-1]["role"] != "user":
        return jsonify(error="Last message must be a user question."), 400

    langs = {"en": "English", "kn": "Kannada", "te": "Telugu", "ta": "Tamil",
             "hi": "Hindi", "mr": "Marathi"}
    language = langs.get(str(data.get("lang", "en")), "English")
    payload = {"model": model, "max_tokens": max_tokens,
               "system": SYSTEM_PROMPT.format(chart=chart, language=language), "messages": msgs}
    try:
        r = requests.post(ANTHROPIC_URL, json=payload, timeout=60, headers={
            "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
            "content-type": "application/json"})
        if r.status_code != 200:
            detail = ""
            try:
                detail = (r.json().get("error") or {}).get("message", "")[:200]
            except Exception:
                pass
            app.logger.error("Anthropic %s: %s", r.status_code, detail)
            return jsonify(error=f"Reading service error ({r.status_code}): {detail or 'check API key and credits.'}"), 502
        out = r.json()
        text = "\n".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text").strip()
        # burn quota only after a successful answer
        u = u if kind == "vip" else bump_user(uid, free_delta=1 if kind == "free" else 0,
                      credit_delta=-1 if kind == "paid" else 0)
        return jsonify(answer=text or "The sky is quiet — please ask again.", status=status_payload(u))
    except requests.RequestException as e:
        app.logger.error("Anthropic call failed: %s", e)
        return jsonify(error="Could not reach the reading service. Please try again."), 502


# ---------------- admin audit ----------------
@app.get("/admin")
def admin():
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        return "Forbidden", 403
    rows = []
    if DATABASE_URL:
        with db() as c, c.cursor() as cur:
            cur.execute("""SELECT cl.utr, cl.uid, cl.amount, cl.status, cl.created,
                u.free_used, u.credits FROM claims cl JOIN users u ON u.uid=cl.uid
                ORDER BY cl.created DESC LIMIT 200""")
            rows = cur.fetchall()
    else:
        rows = [(utr, d["uid"], d["amount"], d["status"], "-", "-", "-") for utr, d in _mem_claims.items()]
    body = "".join(
        f"<tr><td>{r[0]}</td><td>{r[1][:12]}…</td><td>₹{r[2]}</td><td>{r[3]}</td><td>{r[4]}</td>"
        f"<td>{r[5]}/{r[6]}</td>"
        f"<td>{'<a href=\"/admin/revoke?token=' + ADMIN_TOKEN + '&utr=' + r[0] + '\">revoke</a>' if r[3]=='active' else '—'}</td></tr>"
        for r in rows)
    return f"""<html><head><title>CosmicSage claims</title><style>
      body{{font-family:sans-serif;background:#14122B;color:#eee;padding:24px}}
      table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #444;padding:6px 10px;font-size:13px}}
      a{{color:#E8C887}}</style></head><body>
      <h2>Claims — audit against your bank app (₹{PACK_PRICE} receipts)</h2>
      <table><tr><th>UTR</th><th>uid</th><th>Amount</th><th>Status</th><th>Claimed at</th><th>free/credits</th><th></th></tr>
      {body or '<tr><td colspan=7>No claims yet</td></tr>'}</table></body></html>"""


@app.get("/admin/revoke")
def admin_revoke():
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        return "Forbidden", 403
    utr = request.args.get("utr", "")
    if DATABASE_URL:
        with db() as c, c.cursor() as cur:
            cur.execute("UPDATE claims SET status='revoked' WHERE utr=%s AND status='active' RETURNING uid", (utr,))
            row = cur.fetchone()
        if row:
            bump_user(row[0], credit_delta=-PACK_QUESTIONS)
    return f"<a href='/admin?token={ADMIN_TOKEN}'>← revoked, back to claims</a>"


@app.get("/api/geocode")
def geocode():
    """Return up to 6 matches, best first, so the user picks the right city.
    Open-Meteo gives population + IANA timezone; we sort by population so big
    cities (Bangalore, Karnataka) outrank tiny namesakes (Bangalore Town, Sindh)."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(error="Type a place name."), 400
    matches = []
    try:
        r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": q, "count": 10, "language": "en", "format": "json"}, timeout=10)
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        results.sort(key=lambda h: h.get("population") or 0, reverse=True)
        for h in results[:6]:
            name = ", ".join(x for x in [h.get("name"), h.get("admin1"), h.get("country")] if x)
            matches.append({"name": name, "lat": h["latitude"], "lon": h["longitude"],
                            "tzname": h.get("timezone", ""), "tz": None,
                            "pop": h.get("population") or 0})
    except requests.RequestException:
        pass
    if matches:
        return jsonify(matches=matches)
    # fallback: Nominatim (knows tiny villages Open-Meteo misses)
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": q, "format": "json", "limit": 6, "addressdetails": 1},
                         headers={"User-Agent": "CosmicSage/5.0 (free astrology site)"}, timeout=10)
        r.raise_for_status()
        results = r.json()
        if not results:
            return jsonify(error="Place not found — try adding the state or country."), 404
        for hit in results:
            lon = float(hit["lon"])
            matches.append({"name": hit.get("display_name", q), "lat": float(hit["lat"]), "lon": lon,
                            "tzname": "", "tz": round(lon / 15 * 2) / 2, "pop": 0})
        return jsonify(matches=matches)
    except requests.RequestException:
        return jsonify(error="Geocoding services unavailable — try again in a moment."), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
