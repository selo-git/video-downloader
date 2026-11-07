# ローカル動画ダウンローダー(YouTube / Instagram / TikTok など yt-dlp 対応サイト)
# 実行: .venv/bin/python app.py → http://localhost:3456
import json
import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

# Finder から .app 経由で起動すると PATH に Homebrew が含まれず、
# yt-dlp / mlx_whisper が ffmpeg を見つけられないため補完する。
for _bin in ("/opt/homebrew/bin", "/usr/local/bin"):
    if os.path.isdir(_bin) and _bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")

from flask import Flask, jsonify, render_template, request
import yt_dlp
from yt_dlp.utils import DownloadCancelled, download_range_func

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.json"
HISTORY_FILE = BASE_DIR / "history.json"

DEFAULT_SETTINGS = {
    "download_dir": str(Path.home() / "Downloads" / "VideoDownloader"),
    "filename_template": "%(title).100s.%(ext)s",
    "cookies_browser": "none",   # none / chrome / safari / firefox / edge / brave
    "max_concurrent": 2,
}

PLAYLIST_LIMIT = 100  # プレイリスト展開の上限件数

settings_lock = threading.Lock()
history_lock = threading.Lock()

# ---------------- 設定 ----------------

def load_settings():
    with settings_lock:
        if SETTINGS_FILE.exists():
            try:
                saved = json.loads(SETTINGS_FILE.read_text())
                return {**DEFAULT_SETTINGS, **saved}
            except (json.JSONDecodeError, OSError):
                pass
        return dict(DEFAULT_SETTINGS)


def save_settings(s):
    with settings_lock:
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2))


# ---------------- 履歴 ----------------

def load_history():
    with history_lock:
        if HISTORY_FILE.exists():
            try:
                return json.loads(HISTORY_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return []


def append_history(record):
    with history_lock:
        items = []
        if HISTORY_FILE.exists():
            try:
                items = json.loads(HISTORY_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                items = []
        items.append(record)
        HISTORY_FILE.write_text(json.dumps(items[-500:], ensure_ascii=False, indent=1))


# ---------------- ジョブ管理 ----------------

jobs = {}          # id -> dict
jobs_lock = threading.Lock()
job_seq = 0

active_slots = 0
slots_cond = threading.Condition()


def new_job(url, quality, options, title=""):
    global job_seq
    with jobs_lock:
        job_seq += 1
        job_id = str(job_seq)
        jobs[job_id] = {
            "id": job_id,
            "url": url,
            "title": title,
            "thumbnail": "",
            "quality": quality,
            "options": options,
            "status": "queued",   # queued/downloading/processing/done/error/canceled/expanded
            "percent": 0,
            "speed": "",
            "eta": "",
            "size": "",
            "filename": "",
            "transcript": "",
            "error": "",
            "note": "",
        }
        cancel_events[job_id] = threading.Event()
        return job_id


cancel_events = {}  # id -> threading.Event


def acquire_slot(job_id):
    global active_slots
    ev = cancel_events[job_id]
    with slots_cond:
        while True:
            if ev.is_set():
                return False
            if active_slots < max(1, int(load_settings()["max_concurrent"])):
                active_slots += 1
                return True
            slots_cond.wait(0.5)


def release_slot():
    global active_slots
    with slots_cond:
        active_slots -= 1
        slots_cond.notify_all()


# ---------------- yt-dlp オプション構築 ----------------

def parse_time(t):
    """'1:23:45' / '3:20' / '90' → 秒"""
    t = (t or "").strip()
    if not t:
        return None
    parts = t.split(":")
    try:
        sec = 0
        for p in parts:
            sec = sec * 60 + float(p)
        return sec
    except ValueError:
        return None


def format_selector(quality):
    if quality == "audio":
        return "ba/b"
    if quality == "best":
        return "bv*+ba/b"
    h = int(quality)
    return f"bv*[height<={h}]+ba/b[height<={h}]/b"


# ---------------- 文字起こし (Whisper) ----------------
# 言語選択 → (モデル, 言語指定)。精度優先の割り当て:
#   英語: large-v3-turbo(英語はフル版とほぼ同精度で高速)
#   日本語/混在/自動: large-v3 フル版(日本語精度を優先)
WHISPER_MODELS = {
    "en": ("mlx-community/whisper-large-v3-turbo", "en"),
    "ja": ("mlx-community/whisper-large-v3-mlx", "ja"),
    "mixed": ("mlx-community/whisper-large-v3-mlx", None),
    "auto": ("mlx-community/whisper-large-v3-mlx", None),
}

def _srt_ts(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int((t % 1) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def transcribe_file(media_path, lang_choice):
    """動画/音声ファイルを文字起こしし、.txt と .srt のパスを返す"""
    import mlx_whisper  # 初回import が重いため遅延読み込み

    model, language = WHISPER_MODELS.get(lang_choice, WHISPER_MODELS["auto"])
    result = mlx_whisper.transcribe(
        media_path, path_or_hf_repo=model, language=language, verbose=None
    )

    base = os.path.splitext(media_path)[0]
    txt_path = base + ".txt"
    srt_path = base + ".transcript.srt"

    with open(txt_path, "w") as f:
        f.write((result.get("text") or "").strip() + "\n")
    with open(srt_path, "w") as f:
        for i, seg in enumerate(result.get("segments") or [], 1):
            f.write(f"{i}\n{_srt_ts(seg['start'])} --> {_srt_ts(seg['end'])}\n"
                    f"{seg['text'].strip()}\n\n")
    return txt_path


def base_opts():
    """cookie設定など全リクエスト共通のオプション"""
    s = load_settings()
    opts = {"quiet": True, "no_warnings": True}
    if s["cookies_browser"] != "none":
        opts["cookiesfrombrowser"] = (s["cookies_browser"],)
    return opts


def build_download_opts(job):
    s = load_settings()
    quality = job["quality"]
    o = job["options"]
    opts = {
        **base_opts(),
        "noplaylist": True,
        "format": format_selector(quality),
        "outtmpl": str(Path(s["download_dir"]) / s["filename_template"]),
    }

    postprocessors = []
    if quality == "audio":
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": o.get("audio_format", "m4a"),
            "preferredquality": "0",
        })
    else:
        opts["merge_output_format"] = "mp4"

    if o.get("subtitles"):
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["ja", "en"]
        postprocessors.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})

    if o.get("thumbnail"):
        opts["writethumbnail"] = True
        postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})

    start = parse_time(o.get("start"))
    end = parse_time(o.get("end"))
    if start is not None or end is not None:
        opts["download_ranges"] = download_range_func(
            None, [(start or 0, end if end is not None else float("inf"))]
        )
        opts["force_keyframes_at_cuts"] = True

    if postprocessors:
        opts["postprocessors"] = postprocessors
    return opts


# ---------------- ワーカー ----------------

def worker(job_id, expand=True):
    job = jobs[job_id]
    ev = cancel_events[job_id]

    if not acquire_slot(job_id):
        job["status"] = "canceled"
        return
    try:
        if ev.is_set():
            job["status"] = "canceled"
            return

        # プレイリストかどうかを軽量チェックし、必要なら子ジョブに展開
        if expand:
            try:
                flat_opts = {
                    **base_opts(),
                    "extract_flat": "in_playlist",
                    "playlistend": PLAYLIST_LIMIT,
                }
                with yt_dlp.YoutubeDL(flat_opts) as ydl:
                    info = ydl.extract_info(job["url"], download=False)
            except yt_dlp.utils.DownloadError as e:
                job["status"] = "error"
                job["error"] = str(e).split("\n")[-1]
                return

            if info.get("_type") == "playlist" and info.get("entries"):
                entries = [e for e in info["entries"] if e]
                for entry in entries:
                    url = entry.get("url") or entry.get("webpage_url")
                    if not url:
                        continue
                    child_id = new_job(url, job["quality"], job["options"],
                                       title=entry.get("title") or "")
                    threading.Thread(target=worker, args=(child_id, False),
                                     daemon=True).start()
                job["status"] = "expanded"
                job["title"] = info.get("title") or job["url"]
                total = info.get("playlist_count") or len(entries)
                job["note"] = f"プレイリスト: {len(entries)}件をキューに追加" + (
                    f"(全{total}件中、上限{PLAYLIST_LIMIT}件)" if total > len(entries) else ""
                )
                return
            else:
                job["title"] = job["title"] or info.get("title") or ""
                job["thumbnail"] = info.get("thumbnail") or ""

        download(job_id)
    finally:
        release_slot()


def download(job_id):
    job = jobs[job_id]
    ev = cancel_events[job_id]
    job["status"] = "downloading"
    tmpfiles = set()

    def hook(d):
        for key in ("tmpfilename", "filename"):
            if d.get(key):
                tmpfiles.add(d[key])
        if ev.is_set():
            raise DownloadCancelled("canceled by user")
        info = d.get("info_dict") or {}
        if not job["title"] and info.get("title"):
            job["title"] = info["title"]
        if not job["thumbnail"] and info.get("thumbnail"):
            job["thumbnail"] = info["thumbnail"]
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            job["percent"] = round(done / total * 100, 1) if total else 0
            job["speed"] = (d.get("_speed_str") or "").strip()
            job["eta"] = (d.get("_eta_str") or "").strip()
            job["size"] = (d.get("_total_bytes_str")
                           or d.get("_total_bytes_estimate_str") or "").strip()
        elif d["status"] == "finished":
            job["status"] = "processing"
            job["percent"] = 100

    def pp_hook(d):
        if ev.is_set():
            raise DownloadCancelled("canceled by user")
        if d["status"] == "finished":
            fp = (d.get("info_dict") or {}).get("filepath")
            if fp:
                job["filename"] = fp

    opts = build_download_opts(job)
    opts["progress_hooks"] = [hook]
    opts["postprocessor_hooks"] = [pp_hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["url"], download=True)
        if not job["filename"]:
            reqs = (info or {}).get("requested_downloads") or [{}]
            job["filename"] = reqs[0].get("filepath", "")
        job["percent"] = 100

        transcript = ""
        if (job["options"].get("transcribe") and job["filename"]
                and os.path.exists(job["filename"])):
            job["status"] = "transcribing"
            try:
                transcript = transcribe_file(
                    job["filename"], job["options"].get("transcribe_lang", "auto")
                )
                job["transcript"] = transcript
            except Exception as e:
                job["note"] = f"文字起こしに失敗: {type(e).__name__}: {e}"
        job["status"] = "done"

        size = 0
        if job["filename"] and os.path.exists(job["filename"]):
            size = os.path.getsize(job["filename"])
        append_history({
            "title": job["title"] or (info or {}).get("title", ""),
            "uploader": (info or {}).get("uploader") or (info or {}).get("channel") or "",
            "url": job["url"],
            "filepath": job["filename"],
            "quality": job["quality"],
            "thumbnail": job["thumbnail"] or (info or {}).get("thumbnail", ""),
            "extractor": (info or {}).get("extractor_key", ""),
            "size": size,
            "transcript": transcript,
            "date": datetime.now().isoformat(timespec="seconds"),
        })
    except DownloadCancelled:
        job["status"] = "canceled"
        # 中断で残った部分ファイルを削除
        for f in tmpfiles:
            for p in (f, f + ".part", f + ".ytdl"):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
    except yt_dlp.utils.DownloadError as e:
        job["status"] = "error"
        job["error"] = str(e).split("\n")[-1]
    except Exception as e:  # 予期しないエラーもUIに出す
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"


# ---------------- ルーティング ----------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/info")
def api_info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify(error="URLを指定してください"), 400
    opts = {**base_opts(), "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify(error="動画情報を取得できませんでした",
                       detail=str(e).split("\n")[-1]), 400

    heights = sorted(
        {f["height"] for f in info.get("formats", [])
         if f.get("vcodec") not in (None, "none") and f.get("height")},
        reverse=True,
    )
    return jsonify(
        title=info.get("title", ""),
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=info.get("duration_string") or "",
        thumbnail=info.get("thumbnail") or "",
        extractor=info.get("extractor_key") or "",
        heights=heights,
    )


@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    urls = [u.strip() for u in (data.get("urls") or []) if u.strip()]
    quality = data.get("quality") or "best"
    options = {
        "subtitles": bool(data.get("subtitles")),
        "thumbnail": bool(data.get("thumbnail")),
        "audio_format": data.get("audio_format") or "m4a",
        "start": data.get("start") or "",
        "end": data.get("end") or "",
        "transcribe": bool(data.get("transcribe")),
        "transcribe_lang": data.get("transcribe_lang") or "auto",
    }
    if not urls:
        return jsonify(error="URLを入力してください"), 400

    ids = []
    for url in urls:
        job_id = new_job(url, quality, options)
        threading.Thread(target=worker, args=(job_id, True), daemon=True).start()
        ids.append(job_id)
    return jsonify(ids=ids)


@app.get("/api/jobs")
def api_jobs():
    with jobs_lock:
        items = list(jobs.values())
    items.reverse()  # 新しい順
    return jsonify(items)


@app.post("/api/cancel")
def api_cancel():
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("id", ""))
    if job_id not in jobs:
        return jsonify(error="ジョブが見つかりません"), 404
    cancel_events[job_id].set()
    with slots_cond:
        slots_cond.notify_all()
    return jsonify(ok=True)


@app.get("/api/history")
def api_history():
    items = load_history()
    items.reverse()
    return jsonify(items)


@app.post("/api/history/clear")
def api_history_clear():
    with history_lock:
        HISTORY_FILE.write_text("[]")
    return jsonify(ok=True)


@app.get("/api/settings")
def api_settings_get():
    return jsonify(load_settings())


@app.post("/api/settings")
def api_settings_post():
    data = request.get_json(silent=True) or {}
    s = load_settings()

    d = str(data.get("download_dir") or "").strip()
    if d:
        d = str(Path(d).expanduser())
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
        except OSError:
            return jsonify(error="保存先フォルダを作成できません: " + d), 400
        s["download_dir"] = d

    t = str(data.get("filename_template") or "").strip()
    if t:
        if "%(ext)s" not in t:
            return jsonify(error="ファイル名テンプレートには %(ext)s を含めてください"), 400
        s["filename_template"] = t

    browser = data.get("cookies_browser")
    if browser in ("none", "chrome", "safari", "firefox", "edge", "brave"):
        s["cookies_browser"] = browser

    try:
        mc = int(data.get("max_concurrent", s["max_concurrent"]))
        s["max_concurrent"] = min(max(mc, 1), 5)
    except (TypeError, ValueError):
        pass

    save_settings(s)
    with slots_cond:
        slots_cond.notify_all()
    return jsonify(s)


@app.post("/api/reveal")
def api_reveal():
    data = request.get_json(silent=True) or {}
    path = data.get("path") or ""
    if path and os.path.exists(path):
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["open", load_settings()["download_dir"]])
    return jsonify(ok=True)


@app.post("/api/open-file")
def api_open_file():
    data = request.get_json(silent=True) or {}
    path = data.get("path") or ""
    if path and os.path.exists(path) and path.endswith((".txt", ".srt")):
        subprocess.Popen(["open", path])
        return jsonify(ok=True)
    return jsonify(error="ファイルが見つかりません"), 404


@app.post("/api/open-folder")
def api_open_folder():
    subprocess.Popen(["open", load_settings()["download_dir"]])
    return jsonify(ok=True)


@app.post("/api/shutdown")
def api_shutdown():
    # アクティブなジョブがある場合は拒否
    active = [j for j in jobs.values()
              if j["status"] in ("queued", "downloading", "processing", "transcribing")]
    if active:
        return jsonify(error=f"実行中のジョブが{len(active)}件あります。完了またはキャンセル後に終了してください"), 409

    def _exit():
        import time
        time.sleep(0.5)  # レスポンスを返してから終了
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()
    return jsonify(ok=True)


if __name__ == "__main__":
    s = load_settings()
    Path(s["download_dir"]).mkdir(parents=True, exist_ok=True)
    print(f"保存先: {s['download_dir']}")
    app.run(host="127.0.0.1", port=3456, debug=False)
