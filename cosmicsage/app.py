"""CosmicSage — Flask backend for Render.

Serves the single-page app and proxies chart questions to the Anthropic API.
Set the ANTHROPIC_API_KEY environment variable in the Render dashboard.
"""
import os
import time
from collections import defaultdict

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"

# --- very simple per-IP daily rate limit to protect your API bill ---
DAILY_LIMIT = int(os.environ.get("DAILY_QUESTION_LIMIT", "40"))
_usage = defaultdict(list)  # ip -> [timestamps]


def _allowed(ip: str) -> bool:
    now = time.time()
    _usage[ip] = [t for t in _usage[ip] if now - t < 86400]
    if len(_usage[ip]) >= DAILY_LIMIT:
        return False
    _usage[ip].append(now)
    return True


SYSTEM_PROMPT = """You are CosmicSage, a warm, grounded Vedic astrologer. You have the
user's verified natal chart, dasha periods, and today's transits below. Give DETAILED,
substantive readings of 250-400 words. In every answer: cite the SPECIFIC planets,
houses, degrees and nakshatras you are reading (e.g. 'your Jupiter at 14.9 degrees in
Scorpio in the 4th house'), explain WHY each placement matters for the question, weave
in the current mahadasha/antardasha and at least one relevant transit, and structure the
answer in short readable paragraphs. Never give generic sun-sign text. Frame insights as
tendencies and timings, not certainties. Be encouraging but honest, and end with one or
two practical suggestions. For health, finance, or legal topics, gently note that chart
guidance complements but never replaces professional advice.

{chart}"""


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


# Rough timezone offsets by country for birth-time conversion (no-DST simplification;
# for multi-zone countries we fall back to longitude/15).
TZ_BY_COUNTRY = {
    "in": 5.5, "lk": 5.5, "np": 5.75, "bd": 6, "pk": 5, "ae": 4, "sa": 3,
    "sg": 8, "my": 8, "hk": 8, "cn": 8, "jp": 9, "kr": 9, "th": 7, "id": 7,
    "gb": 0, "ie": 0, "de": 1, "fr": 1, "it": 1, "es": 1, "nl": 1, "za": 2,
    "ke": 3, "qa": 3, "om": 4, "mu": 4, "nz": 12,
}


@app.get("/api/geocode")
def geocode():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(error="Type a place name."), 400
    # Primary: Open-Meteo geocoding (keyless, returns IANA timezone)
    try:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": q, "count": 1, "language": "en", "format": "json"},
            timeout=10,
        )
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        if results:
            h = results[0]
            name = ", ".join(x for x in [h.get("name"), h.get("admin1"), h.get("country")] if x)
            return jsonify(name=name, lat=h["latitude"], lon=h["longitude"],
                           tzname=h.get("timezone", ""), tz=None)
    except requests.RequestException:
        pass
    # Fallback: Nominatim (OpenStreetMap)
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": "CosmicSage/2.0 (free astrology site)"},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json()
        if not results:
            return jsonify(error="Place not found — try adding the state or country."), 404
        hit = results[0]
        lat, lon = float(hit["lat"]), float(hit["lon"])
        cc = (hit.get("address", {}).get("country_code") or "").lower()
        tz = TZ_BY_COUNTRY.get(cc, round(lon / 15 * 2) / 2)
        return jsonify(name=hit.get("display_name", q), lat=lat, lon=lon, tzname="", tz=tz)
    except requests.RequestException:
        return jsonify(error="Geocoding services unavailable — try again in a moment."), 502


@app.get("/health")
def health():
    return {"ok": True, "key_configured": bool(ANTHROPIC_API_KEY)}


@app.post("/api/ask")
def ask():
    if not ANTHROPIC_API_KEY:
        return jsonify(error="Server is missing ANTHROPIC_API_KEY."), 500

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0]
    if not _allowed(ip):
        return jsonify(error="Daily question limit reached — please return tomorrow."), 429

    data = request.get_json(silent=True) or {}
    chart = str(data.get("chart", ""))[:8000]
    history = data.get("history", [])
    if not chart or not isinstance(history, list) or not history:
        return jsonify(error="Missing chart or question."), 400

    # sanitize history: only role/content pairs, capped length
    msgs = []
    for m in history[-20:]:
        role = m.get("role")
        content = str(m.get("content", ""))[:2000]
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    if not msgs or msgs[-1]["role"] != "user":
        return jsonify(error="Last message must be a user question."), 400

    payload = {
        "model": MODEL,
        "max_tokens": 1500,
        "system": SYSTEM_PROMPT.format(chart=chart),
        "messages": msgs,
    }
    try:
        r = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if r.status_code != 200:
            detail = ""
            try:
                detail = (r.json().get("error") or {}).get("message", "")[:200]
            except Exception:
                pass
            app.logger.error("Anthropic %s: %s", r.status_code, detail)
            return jsonify(error=f"Reading service error ({r.status_code}): {detail or 'check API key and credits in the Anthropic console.'}"), 502
        out = r.json()
        text = "\n".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text").strip()
        return jsonify(answer=text or "The sky is quiet — please ask again.")
    except requests.RequestException as e:
        app.logger.error("Anthropic call failed: %s", e)
        return jsonify(error="Could not reach the reading service. Please try again."), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
