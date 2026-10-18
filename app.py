import json
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import moviepy.editor as mp
import speech_recognition as sr

try:
    from google import genai
    from google.genai import types
except ImportError:  # app still works with fallback feedback
    genai = None
    types = None

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {"mp4", "avi", "mov", "mkv"}
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "120"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "5"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE
# Trust one proxy hop (Render / Hugging Face) so request.remote_addr is the real client.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
UPLOAD_FOLDER.mkdir(exist_ok=True)

client = None
if genai is not None and os.getenv("GEMINI_API_KEY"):
    try:
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    except Exception:
        client = None

# ---------- simple in-memory rate limiter (per IP) ----------
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def is_rate_limited(ip):
    now = time.time()
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            return True
        q.append(now)
        if len(_hits) > 1000:  # keep memory bounded
            for key in [k for k, v in _hits.items() if not v]:
                del _hits[key]
        return False


def allowed_file(filename):
    return (
        bool(filename)
        and "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def extract_audio_and_transcribe(video_path):
    audio_path = video_path.with_name(f"{video_path.stem}_{uuid.uuid4().hex}.wav")
    video = None

    try:
        video = mp.VideoFileClip(str(video_path))

        if video.duration and video.duration > MAX_DURATION_SECONDS:
            return (
                f"[Video is longer than {MAX_DURATION_SECONDS} seconds. "
                "Please upload a shorter introduction]"
            )

        if video.audio is None:
            return "[No audio track found in the uploaded video]"

        video.audio.write_audiofile(
            str(audio_path),
            fps=16000,
            codec="pcm_s16le",
            ffmpeg_params=["-ac", "1"],
            logger=None,
        )

        recognizer = sr.Recognizer()
        with sr.AudioFile(str(audio_path)) as source:
            audio_data = recognizer.record(source)

        try:
            return recognizer.recognize_google(audio_data)
        except sr.UnknownValueError:
            return "[Audio detected, but speech could not be understood clearly]"
        except sr.RequestError:
            return "[Speech Recognition service is unavailable]"

    except Exception as exc:
        return f"[Error processing audio/video: {exc}]"

    finally:
        if video:
            try:
                video.close()
            except Exception:
                pass
        if audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                pass


def fallback_analysis(transcript):
    if not transcript or transcript.startswith("["):
        return {
            "score": "5/10",
            "positives": ["Video was received successfully"],
            "improvements": ["Make sure the video contains clear spoken audio"],
            "tips": ["Record in a quiet environment", "Speak clearly and slowly"],
        }

    words = transcript.split()
    fillers = {"um", "uh", "like", "actually", "basically", "so"}
    filler_count = sum(w.lower().strip(".,!?;:") in fillers for w in words)

    score = 8 if len(words) >= 30 else 6
    if filler_count >= 5:
        score -= 1

    return {
        "score": f"{max(1, min(score, 10))}/10",
        "positives": [
            "Speech was successfully transcribed",
            "The introduction can be reviewed for clarity and structure",
        ],
        "improvements": (
            ["Reduce repeated filler words"]
            if filler_count >= 5
            else ["Add specific achievements or project examples"]
        ),
        "tips": [
            "Start with your name, education, and target role",
            "Mention 2-3 relevant technical skills",
            "End with a concise career objective",
        ],
    }


def normalize_score(value):
    """Return a safe 'X/10' string from whatever the model produced."""
    try:
        num = float(str(value).split("/")[0].strip())
        return f"{max(0, min(round(num), 10))}/10"
    except (ValueError, TypeError):
        return "N/A"


def as_str_list(value):
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def analyze_transcript_with_ai(transcript):
    if not client or not transcript or transcript.startswith("["):
        return fallback_analysis(transcript)

    prompt = f"""
Analyze this job applicant self-introduction.

Evaluate quality, grammar, structure, professional tone, filler words,
clarity, and confidence. Base feedback only on the transcript text.

Transcript:
{transcript[:5000]}

Return ONLY valid JSON:
{{
  "score": "X/10",
  "positives": ["positive 1", "positive 2"],
  "improvements": ["improvement 1", "improvement 2"],
  "tips": ["tip 1", "tip 2"]
}}
"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        result = json.loads(response.text)
        if not isinstance(result, dict):
            raise ValueError("Unexpected AI response shape")
        return {
            "score": normalize_score(result.get("score")),
            "positives": as_str_list(result.get("positives")),
            "improvements": as_str_list(result.get("improvements")),
            "tips": as_str_list(result.get("tips")),
        }
    except Exception:
        app.logger.warning("Gemini analysis failed, using fallback", exc_info=True)
        return fallback_analysis(transcript)


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "File is too large. Maximum allowed size is 50 MB."}), 413


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload_video():
    if is_rate_limited(request.remote_addr or "unknown"):
        return jsonify({
            "error": "Too many requests. Please wait a minute and try again."
        }), 429

    if "video" not in request.files:
        return jsonify({"error": "No video file was provided."}), 400

    file = request.files["video"]

    if not file.filename:
        return jsonify({"error": "No video file was selected."}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Invalid format. Use MP4, AVI, MOV, or MKV."}), 400

    safe_name = secure_filename(file.filename)
    if not safe_name:
        return jsonify({"error": "Invalid file name."}), 400

    video_path = UPLOAD_FOLDER / f"{uuid.uuid4().hex}_{safe_name}"

    try:
        file.save(str(video_path))
        transcript = extract_audio_and_transcribe(video_path)
        analysis = analyze_transcript_with_ai(transcript)

        return jsonify({
            "success": True,
            "transcript": transcript,
            "score": analysis.get("score", "N/A"),
            "positives": analysis.get("positives", []),
            "improvements": analysis.get("improvements", []),
            "tips": analysis.get("tips", []),
        })
    except Exception:
        app.logger.exception("Video processing failed")
        return jsonify({"error": "Could not process the video. Please try another file."}), 500
    finally:
        if video_path.exists():
            try:
                video_path.unlink()
            except OSError:
                pass


@app.route("/health")
def health():
    return jsonify({"status": "ok", "gemini_enabled": client is not None})


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )
