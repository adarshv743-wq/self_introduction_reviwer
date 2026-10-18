# AI Self-Introduction Reviewer

Upload a self-introduction video, get a transcript and transcript-based feedback.

## Structure

```
app.py  requirements.txt  Dockerfile  .env.example
templates/index.html
static/style.css
uploads/.gitkeep
```

## Run locally

```bash
python -m venv venv
venv\Scripts\activate        # Windows  (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env       # then add your GEMINI_API_KEY (optional)
python app.py
```

Open http://127.0.0.1:5000/ and check http://127.0.0.1:5000/health

Use Python 3.10 or 3.11. MoviePy uses a bundled FFmpeg; if it fails, install FFmpeg and add it to PATH.
Without a Gemini key the app uses simple built-in feedback.

## Deploy (Render, Docker)

1. Push to GitHub (`.env` is git-ignored).
2. Render: New > Web Service > connect repo > Runtime: Docker.
3. Add environment variable `GEMINI_API_KEY`.
4. Open `/health` on your Render URL to verify.

## Limits and honesty

- Max 50 MB, max 120 s video, 5 uploads per minute per IP.
- Feedback is based on the transcript only. Body language and pronunciation are not judged.
- Speech recognition uses the free Google web endpoint (best for short clips).
