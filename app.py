"""
Podcast Episode Explorer and Audio Downloader
=============================================

A small Flask app that turns one or more Apple Podcasts URLs into browsable
podcast cards, episode tables with inline players, RSS access, full downloads,
and custom-length audio clips.

Run:
    python app.py
Then open http://127.0.0.1:5000
"""

import os
import concurrent.futures
from urllib.parse import urlparse, quote

import requests
from flask import (
    Flask, render_template, request, jsonify, Response,
    send_file, after_this_request, abort,
)

from podcast_core import apple, rss, audio, inputs, bulk

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload cap

# Optional password protection. If the APP_PASSWORD environment variable is set,
# every request must supply it via HTTP Basic auth (any username, that password).
# If APP_PASSWORD is unset, the app is open (no login) - so set it before putting
# the app on a public URL. Browsers remember Basic-auth credentials for the
# session, so it prompts once and then all pages/downloads work normally.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()


@app.before_request
def _require_password():
    if not APP_PASSWORD:
        return  # auth disabled
    auth = request.authorization
    if auth and (auth.password or "") == APP_PASSWORD:
        return  # correct password
    return Response(
        "Authentication required.", 401,
        {"WWW-Authenticate": 'Basic realm="Podcast Explorer"'},
    )

MAX_PODCASTS = 100          # safety cap on how many URLs we process at once
LOOKUP_WORKERS = 10         # parallelism for the iTunes Lookup API
STREAM_CHUNK = 64 * 1024
DOWNLOAD_UA = {"User-Agent": "Mozilla/5.0 (compatible; PodcastExplorer/1.0)"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _is_http_url(url):
    """Only allow http(s) targets for proxying/clipping."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def _content_disposition(filename):
    """
    Build a Content-Disposition header that preserves spaces and non-ASCII
    characters. Modern browsers use the RFC 5987 filename* (UTF-8) value; the
    plain quoted filename is an ASCII fallback for very old clients.
    """
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip()
    if not ascii_name:
        ascii_name = "download." + filename.rsplit(".", 1)[-1] if "." in filename else "download"
    utf8_name = quote(filename)
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'


def _process_one(parsed):
    """Look up a single parsed input and build a podcast card dict."""
    try:
        meta = apple.lookup_show(parsed["show_id"], parsed.get("storefront", "us"))
    except Exception as exc:  # noqa: BLE001 - surface any lookup failure per item
        return {"ok": False, "input": parsed["raw"], "error": str(exc)}

    card = {
        "ok": True,
        "input": parsed["raw"],
        "podcast_id": meta["podcast_id"],
        "podcast_name": meta["podcast_name"],
        "artist_name": meta["artist_name"],
        "apple_url": meta["apple_url"],
        "feed_url": meta["feed_url"],
        "artwork": meta["artwork"],
        "primary_genre": meta["primary_genre"],
        "genres": meta["genres"],
        "country": meta["country"],
        "release_date": meta["release_date"],
        "episode_count": meta["episode_count"],
        "has_rss": bool(meta["feed_url"]),
        "focus_audio_url": "",
        "focus_title": "",
    }

    # If the user pasted an episode URL, resolve that episode so the UI can
    # jump straight to it inside the episode table.
    if parsed.get("episode_id"):
        ep = apple.lookup_episode(parsed["episode_id"], parsed.get("storefront", "us"))
        if ep:
            card["focus_audio_url"] = ep["audio_url"]
            card["focus_title"] = ep["title"]

    return card


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------- #
# API: process one or more inputs into podcast cards
# --------------------------------------------------------------------------- #
@app.route("/api/process", methods=["POST"])
def api_process():
    candidates = []

    text = request.form.get("input", "")
    candidates.extend(inputs.from_text(text))

    upload = request.files.get("file")
    if upload and upload.filename:
        name = upload.filename.lower()
        try:
            if name.endswith(".csv"):
                candidates.extend(inputs.from_csv(upload))
            elif name.endswith((".xlsx", ".xlsm")):
                candidates.extend(inputs.from_excel(upload))
            else:
                return jsonify({"error": "Unsupported file type. Upload a .csv or .xlsx file."}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Could not read file: {exc}"}), 400

    # De-duplicate while preserving order.
    seen, ordered = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)

    if not ordered:
        return jsonify({"error": "No Apple Podcast URLs or ids found in your input."}), 400
    if len(ordered) > MAX_PODCASTS:
        return jsonify({"error": f"Too many inputs ({len(ordered)}). The limit is {MAX_PODCASTS}."}), 400

    parsed_items, parse_errors = [], []
    for c in ordered:
        try:
            parsed_items.append(apple.parse_apple_url(c))
        except apple.AppleError as exc:
            parse_errors.append({"ok": False, "input": c, "error": str(exc)})

    cards = []
    if parsed_items:
        with concurrent.futures.ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as ex:
            cards = list(ex.map(_process_one, parsed_items))

    results = cards + parse_errors
    ok = [c for c in results if c.get("ok")]
    failed = [c for c in results if not c.get("ok")]

    return jsonify({"podcasts": ok, "failed": failed, "count": len(ok)})


# --------------------------------------------------------------------------- #
# API: episodes for a single feed
# --------------------------------------------------------------------------- #
@app.route("/api/episodes", methods=["POST"])
def api_episodes():
    data = request.get_json(silent=True) or {}
    feed_url = (data.get("feed_url") or "").strip()
    apple_url = (data.get("apple_url") or "").strip()

    if not feed_url:
        return jsonify({"error": "This podcast has no RSS feed available."}), 400

    try:
        result = rss.fetch_feed(feed_url)
    except rss.RSSError as exc:
        return jsonify({"error": str(exc)}), 502

    # Best-effort enrichment (rating / review count / long description).
    if apple_url:
        extra = apple.scrape_show_page(apple_url)
        if extra:
            if extra.get("rating"):
                result["feed"]["rating"] = extra["rating"]
            if extra.get("review_count"):
                result["feed"]["review_count"] = extra["review_count"]
            if extra.get("description") and not result["feed"].get("description"):
                result["feed"]["description"] = extra["description"]

    return jsonify(result)


# --------------------------------------------------------------------------- #
# API: RSS view / download
# --------------------------------------------------------------------------- #
@app.route("/api/rss")
def api_rss():
    feed_url = (request.args.get("feed_url") or "").strip()
    download = request.args.get("download") == "1"
    if not _is_http_url(feed_url):
        abort(400, "Invalid feed URL")

    try:
        xml_bytes = rss.raw_feed_xml(feed_url)
    except Exception as exc:  # noqa: BLE001
        abort(502, f"Could not fetch RSS: {exc}")

    headers = {"Content-Type": "application/rss+xml; charset=utf-8"}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="feed.xml"'
    return Response(xml_bytes, headers=headers)


# --------------------------------------------------------------------------- #
# API: full episode download (streamed proxy so we control the filename)
# --------------------------------------------------------------------------- #
@app.route("/api/download")
def api_download():
    audio_url = (request.args.get("audio_url") or "").strip()
    title = request.args.get("title") or "episode"
    if not _is_http_url(audio_url):
        abort(400, "Invalid audio URL")

    try:
        upstream = requests.get(
            audio_url, headers=DOWNLOAD_UA, stream=True, timeout=30
        )
        upstream.raise_for_status()
    except requests.RequestException as exc:
        abort(502, f"Could not fetch audio: {exc}")

    content_type = upstream.headers.get("Content-Type", "audio/mpeg")
    ext = "mp3"
    if "mp4" in content_type or "m4a" in content_type:
        ext = "m4a"
    filename = audio.safe_filename(title, fallback="episode", ext=ext)

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=STREAM_CHUNK):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    headers = {
        "Content-Disposition": _content_disposition(filename),
        "Content-Type": content_type,
    }
    if upstream.headers.get("Content-Length"):
        headers["Content-Length"] = upstream.headers["Content-Length"]
    return Response(generate(), headers=headers)


# --------------------------------------------------------------------------- #
# API: custom clip
# --------------------------------------------------------------------------- #
@app.route("/api/clip")
def api_clip():
    audio_url = (request.args.get("audio_url") or "").strip()
    mode = (request.args.get("mode") or "").strip()
    title = request.args.get("title") or "clip"

    if not _is_http_url(audio_url):
        return jsonify({"error": "Invalid audio URL."}), 400

    try:
        audio.ensure_available()
    except audio.AudioError as exc:
        return jsonify({"error": str(exc)}), 400

    # Total duration: prefer the value the client already knows (from RSS),
    # otherwise probe. Only needed for "last N" clips.
    total = None
    total_arg = request.args.get("total")
    if total_arg:
        try:
            total = float(total_arg)
        except ValueError:
            total = None
    if mode in ("last_5", "last_10") and not total:
        total = audio.probe_duration(audio_url)

    try:
        start_s, length_s = audio.resolve_window(
            mode,
            total=total,
            start=request.args.get("start"),
            end=request.args.get("end"),
            duration=request.args.get("duration"),
        )
        clip_path = audio.cut_clip(audio_url, start_s, length_s)
    except audio.AudioError as exc:
        return jsonify({"error": str(exc)}), 400

    @after_this_request
    def _cleanup(response):  # noqa: ANN001
        try:
            os.remove(clip_path)
        except OSError:
            pass
        return response

    suffix = mode.replace("_", "")
    filename = audio.safe_filename(f"{title}_{suffix}", fallback="clip", ext="mp3")
    return send_file(
        clip_path,
        mimetype="audio/mpeg",
        as_attachment=True,
        download_name=filename,
    )


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "Upload too large (16 MB max)."}), 413


# --------------------------------------------------------------------------- #
# API: bulk download (many audio files as one zip, with progress polling)
# --------------------------------------------------------------------------- #
@app.route("/api/bulk/start", methods=["POST"])
def api_bulk_start():
    data = request.get_json(silent=True) or {}
    raw_items = data.get("items") or []
    zip_name = (data.get("zip_name") or "podcasts.zip").strip() or "podcasts.zip"

    items = []
    for it in raw_items:
        url = (it.get("url") or "").strip()
        path = (it.get("path") or "").strip()
        if _is_http_url(url) and path:
            items.append({"url": url, "path": path})

    if not items:
        return jsonify({"error": "No valid audio files to download."}), 400

    try:
        job_id = bulk.start(items, zip_name=zip_name)
    except bulk.BulkError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"job_id": job_id, "total": len(items)})


@app.route("/api/bulk/status")
def api_bulk_status():
    job_id = (request.args.get("job_id") or "").strip()
    status = bulk.get_status(job_id)
    if not status:
        return jsonify({"error": "Unknown or expired download job."}), 404
    return jsonify(status)


@app.route("/api/bulk/result")
def api_bulk_result():
    job_id = (request.args.get("job_id") or "").strip()
    result = bulk.get_result(job_id)
    if not result:
        return jsonify({"error": "This download is not ready yet."}), 409

    zip_path, zip_name = result
    response = send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_name,
    )

    # Delete the zip only after the whole body has been flushed to the client.
    @response.call_on_close
    def _cleanup():
        bulk.discard(job_id)

    return response


@app.route("/api/bulk/cancel", methods=["POST"])
def api_bulk_cancel():
    data = request.get_json(silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    bulk.discard(job_id)
    return jsonify({"ok": True})


if __name__ == "__main__":
    try:
        audio.ensure_available()
    except audio.AudioError as exc:
        print("=" * 70)
        print("WARNING: " + str(exc))
        print("The app will still run, but the clip feature will not work")
        print("until FFmpeg is available. Full downloads and RSS are unaffected.")
        print("=" * 70)
    app.run(host="127.0.0.1", port=5000, debug=True)
