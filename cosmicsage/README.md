# CosmicSage — Free Vedic Birth Chart & Insights

A donation-supported astrology web app: real ephemeris-based Kundali, Vimshottari
dasha timeline, numerology, and AI-powered chart Q&A.

## Deploy on Render (new Web Service)

1. Push this folder to a new GitHub repo (e.g. `cosmicsage`).
2. Render dashboard → **New → Web Service** → connect the repo.
3. Settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app`
   - **Environment variables:**
     - `ANTHROPIC_API_KEY` = your Anthropic API key (required for Q&A)
     - `DAILY_QUESTION_LIMIT` = optional, per-visitor daily cap (default 40)
4. Drop your UPI donation QR image into `static/qr.png` before pushing
   (the same QR you already use on your other Render site).
5. Deploy. Check `/health` — it should show `"key_configured": true`.

## Notes

- Free tier: Render spins down after inactivity; first visit may take ~30s to wake.
- Chart math runs entirely in the visitor's browser; only a text summary of the
  chart is sent to the backend for AI readings. No birth data is stored.
- The per-IP rate limit in `app.py` protects your Anthropic API bill.
