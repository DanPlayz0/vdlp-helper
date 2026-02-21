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


# -------------------------
# Database helpers
# -------------------------

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r") as f:
        return json.load(f)


def save_db(db):
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

                # delete mp4
                filepath = db[vid].get("file")
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)

                # delete hls folder
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
        "-c", "copy",
        "-f", "hls",
        "-hls_time", "6",
        "-hls_list_size", "0",
        "-hls_flags", "delete_segments+append_list",
        os.path.join(output_dir, "playlist.m3u8")
    ]

    subprocess.Popen(cmd)


# -------------------------
# Download worker
# -------------------------

def download_video(video_id, url):
    db = load_db()

    filename = f"{video_id}.mp4"
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    hls_dir = os.path.join(DOWNLOAD_DIR, f"{video_id}_hls")

    db[video_id]["file"] = filepath
    db[video_id]["hls"] = hls_dir
    save_db(db)

    try:
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

        # Start download in background
        def run_download():
            try:
              with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                  ydl.download([url])

              db = load_db()
              db[video_id]["status"] = "ready"
              save_db(db)

            except Exception as e:
                db = load_db()
                db[video_id]["status"] = "failed"
                db[video_id]["error"] = str(e)
                save_db(db)

        threading.Thread(target=run_download, daemon=True).start()

        # Wait until file starts growing then start HLS
        while not os.path.exists(filepath):
            time.sleep(1)

        start_hls_conversion(filepath, hls_dir)

        db[video_id]["status"] = "processing"
        save_db(db)

    except Exception as e:
        db[video_id]["status"] = "failed"
        db[video_id]["error"] = str(e)
        save_db(db)


# -------------------------
# Routes
# -------------------------

@app.route("/", methods=["GET", "POST"])
def home():
    db = load_db()

    if request.method == "POST":
        url = request.form["url"]

        video_id = str(uuid.uuid4())

        db[video_id] = {
            "id": video_id,
            "url": url,
            "status": "processing",
            "created": datetime.utcnow().isoformat(),
            "file": "",
            "hls": ""
        }

        save_db(db)

        threading.Thread(
            target=download_video,
            args=(video_id, url),
            daemon=True
        ).start()

        return redirect(url_for("home"))

    videos = list(db.values())[::-1]

    return render_template_string("""
    <h1>What link would you like to download?</h1>

    <form method="POST">
        <input name="url" style="width:300px" required>
        <button>Download</button>
    </form>

    <h2>Previously Requested</h2>

    <ul>
    {% for v in videos %}
        <li>
            <a href="/video/{{v.id}}">{{v.url}}</a>
            — {{v.status}}
        </li>
    {% endfor %}
    </ul>
    """, videos=videos)


@app.route("/video/<video_id>")
def video_page(video_id):
    db = load_db()

    if video_id not in db:
        return "Not found"

    video = db[video_id]

    return render_template_string("""
    <h1>Video</h1>

    <p>Status: {{video.status}}</p>

    <video id="video" width="400" controls playsinline></video>

    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>

    <script>
    const video = document.getElementById('video');
    const src = "/hls/{{video.id}}/playlist.m3u8";

    if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src;
    } else if (Hls.isSupported()) {
        const hls = new Hls();
        hls.loadSource(src);
        hls.attachMedia(video);
    }
    </script>

    <br><br>

    <a href="/download/{{video.id}}">Download MP4</a>

    <h3>Rotate</h3>

    <form method="POST" action="/rotate/{{video.id}}/90">
        <button>Rotate 90°</button>
    </form>

    <form method="POST" action="/rotate/{{video.id}}/180">
        <button>Rotate 180°</button>
    </form>

    <form method="POST" action="/rotate/{{video.id}}/270">
        <button>Rotate 270°</button>
    </form>

    <br><br>
    <a href="/">Back</a>
    """, video=video)


# -------------------------
# Serve HLS files
# -------------------------

@app.route("/hls/<video_id>/<path:filename>")
def hls(video_id, filename):
    db = load_db()

    if video_id not in db:
        return abort(404)

    hls_dir = db[video_id]["hls"]

    path = os.path.join(hls_dir, filename)

    if not os.path.exists(path):
        return abort(404)

    return send_file(path)


@app.route("/download/<video_id>")
def download(video_id):
    db = load_db()

    if video_id not in db:
        return abort(404)

    return send_file(db[video_id]["file"], as_attachment=True)


@app.route("/rotate/<video_id>/<angle>", methods=["POST"])
def rotate(video_id, angle):
    db = load_db()

    if video_id not in db:
        return abort(404)

    video = db[video_id]

    input_file = video["file"]
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

    subprocess.run(cmd)

    os.remove(input_file)
    os.rename(output_file, input_file)

    return redirect(url_for("video_page", video_id=video_id))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)