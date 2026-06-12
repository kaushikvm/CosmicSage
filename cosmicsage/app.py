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
user's verified natal chart, dasha periods, and today's transits below — reference
SPECIFIC placements (signs, houses, nakshatras, current mahadasha) in every answer;
never give generic sun-sign text. Frame insights as tendencies and timings, not
certainties. Be encouraging but honest. Keep answers to 150-220 words and end with one
practical suggestion. For health, finance, or legal topics, gently note that chart
guidance complements but never replaces professional advice.

{chart}"""


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


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
        "max_tokens": 1000,
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
        r.raise_for_status()
        out = r.json()
        text = "\n".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text").strip()
        return jsonify(answer=text or "The sky is quiet — please ask again.")
    except requests.RequestException as e:
        app.logger.error("Anthropic call failed: %s", e)
        return jsonify(error="Could not reach the reading service. Please try again."), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
