import os
import uuid
import json
import threading
import time
import subprocess
from datetime import datetime, timedelta

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    send_file,
    render_template_string,
    abort
)

import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = "downloads"
DB_FILE = os.path.join(DOWNLOAD_DIR, "database.json")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Thread-safe DB lock
db_lock = threading.Lock()


# -------------------------
# Database helpers
# -------------------------

def load_db():
    with db_lock:
        if not os.path.exists(DB_FILE):
            return {}
        with open(DB_FILE, "r") as f:
            return json.load(f)


def save_db(db):
    with db_lock:
        with open(DB_FILE, "w") as f:
            json.dump(db, f)


# -------------------------
# Cleanup old files
# -------------------------

def cleanup_loop():
    while True:
        db = load_db()
        now = datetime.utcnow()

        changed = False

        for vid in list(db.keys()):
            created = datetime.fromisoformat(db[vid]["created"])
            if now - created > timedelta(hours=24):

                filepath = db[vid].get("file")
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)

                hls_dir = db[vid].get("hls")
                if hls_dir and os.path.exists(hls_dir):
                    for f in os.listdir(hls_dir):
                        os.remove(os.path.join(hls_dir, f))
                    os.rmdir(hls_dir)

                del db[vid]
                changed = True

        if changed:
            save_db(db)

        time.sleep(1800)


threading.Thread(target=cleanup_loop, daemon=True).start()


# -------------------------
# HLS conversion
# -------------------------

def start_hls_conversion(input_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "hls",
        "-hls_time", "6",
        "-hls_playlist_type", "vod",
        "-hls_list_size", "0",
        "-hls_segment_filename",
        os.path.join(output_dir, "seg_%03d.ts"),
        os.path.join(output_dir, "playlist.m3u8")
    ]

    subprocess.run(cmd, check=True)


def ensure_hls(video_id, video):
    input_file = video.get("file")
    hls_dir = video.get("hls")

    if not input_file or not os.path.exists(input_file):
        return False

    if not hls_dir:
        hls_dir = os.path.join(DOWNLOAD_DIR, f"{video_id}_hls")
        video["hls"] = hls_dir

        db = load_db()
        db[video_id] = video
        save_db(db)

    playlist = os.path.join(hls_dir, "playlist.m3u8")

    if os.path.exists(playlist):
        return True

    try:
        start_hls_conversion(input_file, hls_dir)
    except Exception:
        return False

    return os.path.exists(playlist)


def repair_hls():
    db = load_db()
    for vid, video in db.items():
        ensure_hls(vid, video)


threading.Thread(target=repair_hls, daemon=True).start()


# -------------------------
# Download worker
# -------------------------

MAX_CONCURRENT_JOBS = 2
job_semaphore = threading.Semaphore(MAX_CONCURRENT_JOBS)


def download_video(video_id, url):

    def worker():
        with job_semaphore:

            db = load_db()

            filename = f"{video_id}.mp4"
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            hls_dir = os.path.join(DOWNLOAD_DIR, f"{video_id}_hls")

            db[video_id]["file"] = filepath
            db[video_id]["hls"] = hls_dir
            db[video_id]["status"] = "downloading"
            save_db(db)

            ydl_opts: yt_dlp._Params = {
                "outtmpl": filepath,
                "format": "bv*+ba/best",
                "merge_output_format": "mp4",
                "postprocessors": [{
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": "mp4"
                }],
                "postprocessor_args": ["-movflags", "+faststart"]
            }

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

                db = load_db()
                db[video_id]["status"] = "converting"
                save_db(db)

                start_hls_conversion(filepath, hls_dir)

                db = load_db()
                db[video_id]["status"] = "ready"
                save_db(db)

            except Exception as e:
                db = load_db()
                db[video_id]["status"] = "failed"
                db[video_id]["error"] = str(e)
                save_db(db)

    threading.Thread(target=worker, daemon=True).start()


# -------------------------
# Routes
# -------------------------

BASE_STYLE = """
<style>
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0f172a;
    color: #e5e7eb;
}
.container {
    max-width: 640px;
    margin: auto;
    padding: 16px;
}
.card {
    background: #111827;
    border-radius: 16px;
    padding: 16px;
    margin-bottom: 16px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
}
h1, h2, h3 {
    margin-top: 0;
}
input {
    width: 100%;
    padding: 14px;
    border-radius: 12px;
    border: none;
    margin-bottom: 10px;
    font-size: 16px;
}
button {
    width: 100%;
    padding: 14px;
    border-radius: 12px;
    border: none;
    background: #3b82f6;
    color: white;
    font-weight: 600;
    font-size: 16px;
}
button.secondary {
    background: #374151;
}
a {
    color: #60a5fa;
    text-decoration: none;
    word-break: break-all;
}
.video-item {
    padding: 10px 0;
    border-bottom: 1px solid #1f2937;
}
.status {
    font-size: 14px;
    opacity: 0.8;
}
video {
    width: 100%;
    border-radius: 12px;
    background: black;
}
.actions {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 8px;
    margin-top: 12px;
}
.footer {
    margin-top: 20px;
    text-align: center;
    font-size: 14px;
    opacity: 0.6;
}
</style>
"""


@app.route("/", methods=["GET", "POST"])
def home():
    db = load_db()

    if request.method == "POST":
        url = request.form["url"]
        video_id = str(uuid.uuid4())

        db[video_id] = {
            "id": video_id,
            "url": url,
            "status": "queued",
            "created": datetime.utcnow().isoformat(),
            "file": "",
            "hls": ""
        }

        save_db(db)

        download_video(video_id, url)

        return redirect(url_for("home"))

    videos = list(db.values())[::-1]

    return render_template_string(f"""
    {BASE_STYLE}
    <div class="container">

        <div class="card">
            <h1>📥 Video Downloader</h1>
            <form method="POST">
                <input name="url" placeholder="Paste video link..." required>
                <button>Download</button>
            </form>
        </div>

        <div class="card">
            <h2>Recent</h2>

            {{% for v in videos %}}
                <div class="video-item">
                    <a href="/video/{{{{v.id}}}}">{{{{v.url}}}}</a>
                    <div class="status">Status: {{{{v.status}}}}</div>
                </div>
            {{% endfor %}}

            {{% if not videos %}}
                <div class="status">No downloads yet</div>
            {{% endif %}}
        </div>

        <div class="footer">
            Files auto-delete after 24 hours
        </div>

    </div>
    """, videos=videos)


@app.route("/video/<video_id>")
def video_page(video_id):
    db = load_db()

    if video_id not in db:
        return "Not found"

    video = db[video_id]

    return render_template_string(f"""
    {BASE_STYLE}
    <div class="container">

        <div class="card">
            <h2>Video</h2>
            <div class="status">Status: {{{{video.status}}}}</div>

            <video id="video" controls playsinline></video>

            <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>

            <script>
            const video = document.getElementById('video');
            const src = "/hls/{{{{video.id}}}}/playlist.m3u8";

            if (video.canPlayType('application/vnd.apple.mpegurl')) {{
                video.src = src;
            }} else if (Hls.isSupported()) {{
                const hls = new Hls();
                hls.loadSource(src);
                hls.attachMedia(video);
            }}
            </script>

            <br>

            <a href="/download/{{{{video.id}}}}">
                <button>⬇️ Download MP4</button>
            </a>

            <div class="actions">
                <form method="POST" action="/rotate/{{{{video.id}}}}/90">
                    <button class="secondary">90°</button>
                </form>
                <form method="POST" action="/rotate/{{{{video.id}}}}/180">
                    <button class="secondary">180°</button>
                </form>
                <form method="POST" action="/rotate/{{{{video.id}}}}/270">
                    <button class="secondary">270°</button>
                </form>
            </div>

            <br>
            <a href="/">← Back</a>

        </div>
    </div>
    """, video=video)


# -------------------------
# Serve HLS files
# -------------------------

@app.route("/hls/<video_id>/<path:filename>")
def hls(video_id, filename):
    db = load_db()

    video = db.get(video_id)
    if not video:
        return abort(404, description="Video not found")

    if not ensure_hls(video_id, video):
        return abort(404, description="HLS not ready")

    hls_dir = video.get("hls")
    path = os.path.join(hls_dir, filename)

    if not os.path.exists(path):
        return abort(404, description="File not found")

    return send_file(path)


@app.route("/download/<video_id>")
def download(video_id):
    db = load_db()

    video = db.get(video_id)
    if not video:
        return abort(404, description="Video not found")

    video_file = video.get("file")
    if not video_file or not os.path.exists(video_file):
        return abort(404, description="Video file not found")

    return send_file(video_file, as_attachment=True)


@app.route("/rotate/<video_id>/<angle>", methods=["POST"])
def rotate(video_id, angle):
    db = load_db()

    video = db.get(video_id)
    if not video:
        return abort(404, description="Video not found")

    input_file = video["file"]
    hls_dir = video.get("hls")

    output_file = input_file.replace(".mp4", "_rotated.mp4")

    transpose = {
        "90": "transpose=1",
        "180": "transpose=2,transpose=2",
        "270": "transpose=2"
    }[angle]

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_file,
        "-vf", transpose,
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_file
    ]

    subprocess.run(cmd, check=True)

    os.remove(input_file)
    os.rename(output_file, input_file)

    if hls_dir:
        try:
            start_hls_conversion(input_file, hls_dir)
        except Exception:
            pass

    return redirect(url_for("video_page", video_id=video_id))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)