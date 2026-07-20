"""
Bulk audio download engine (with a global job queue).

A browser can only download one file, so to hand the user many episodes at once
we download them server-side and stream back a single zip. That can take a while,
so each job runs in a background thread and the frontend polls for progress.

Because many people may share one hosted instance, downloads are throttled by a
GLOBAL scheduler rather than all running at once:

  * At most MAX_ACTIVE jobs download at the same time. Everyone else waits in a
    queue and is told their position; their download starts automatically when a
    slot frees up. This caps total bandwidth, disk and threads no matter how many
    people click at once.
  * RESERVED_SMALL_SLOTS slot(s) are kept available for "small" jobs (<= 
    SMALL_JOB_FILES files), so quick "grab a few episodes" downloads never wait
    behind someone's giant archive download.

Flow:
    start(items, zip_name)  -> job_id      (queues the job; may start immediately)
    get_status(job_id)      -> dict        (poll this: status/position/progress)
    get_result(job_id)      -> (path,name) (once status == "ready")
    discard(job_id)                        (cancel/forget + delete the zip)

`items` is a list of {"url": <audio url>, "path": <path inside the zip>}.
The path may contain a single "/" for one folder level, e.g.
"The Joe Rogan Experience/Ep2412 - Some Title.mp3".

IMPORTANT: the job registry + queue live in process memory, so the app must run
as a SINGLE instance / worker (threads are fine). To handle more load, give that
one instance more resources (scale up) rather than adding replicas (scale out).
The Dockerfile sets gunicorn to --workers 1.
"""

import os
import re
import time
import uuid
import zipfile
import tempfile
import threading
import concurrent.futures

import requests

from . import clean

# Second identity used only for the comparison fetch when ads are detected;
# a different client identity makes the ad server rotate in other creatives,
# which is what lets us separate the ads from the episode (see clean.py).
ALT_UA = {
    "User-Agent": "AppleCoreMedia/1.0.0.21G93 (iPhone; U; CPU OS 17_6 like Mac OS X)",
    "Accept": "*/*",
}

# Strip dynamically inserted ads from bulk downloads (REMOVE_ADS=0 disables).
REMOVE_ADS = os.environ.get("REMOVE_ADS", "1").strip() not in ("0", "false", "no")

# Identify as an ordinary browser: hosts stitch ads per request, and a browser
# identity tends to receive the unmonetised file, which keeps the common case
# fast (no second fetch needed).
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "audio/webm,audio/ogg,audio/wav,audio/*;q=0.9,application/ogg;q=0.7,video/*;q=0.6,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
}

# --- per-file / per-job limits -------------------------------------------------
MAX_FILES = 200                     # files per zip
MAX_TOTAL_BYTES = 3 * 1024 ** 3     # 3 GB total safety cap per job
DL_WORKERS = 8                      # parallel downloads WITHIN one job
PER_FILE_TIMEOUT = 300              # seconds per file
JOB_TTL = 30 * 60                   # reap finished/abandoned jobs after 30 min

# --- global scheduling (shared across all users) ------------------------------
MAX_ACTIVE = 3                      # max jobs downloading at the same time
RESERVED_SMALL_SLOTS = 1           # slots kept free so small jobs never wait
SMALL_JOB_FILES = 5                # a job with <= this many files is "small"
# Peak concurrent outbound downloads = MAX_ACTIVE * DL_WORKERS (here 3 * 8 = 24).

_jobs = {}
_queue = []          # job_ids waiting to start, in arrival order
_active = set()      # job_ids currently downloading
_lock = threading.Lock()


class BulkError(Exception):
    """Raised for invalid bulk requests (too many files, nothing valid, ...)."""


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
def _active_big():
    return sum(1 for jid in _active if not _jobs[jid]["is_small"])


def _can_start(job):
    """Decide, under _lock, whether this queued job may start right now."""
    if len(_active) >= MAX_ACTIVE:
        return False
    if job["is_small"]:
        return True
    # A big job may not consume the slot(s) reserved for small jobs.
    return len(_active) < (MAX_ACTIVE - RESERVED_SMALL_SLOTS)


def _ordered_queue():
    """Queued job_ids in the order we'd like to start them: small jobs first
    (each group keeps arrival order)."""
    small = [jid for jid in _queue if _jobs[jid]["is_small"]]
    big = [jid for jid in _queue if not _jobs[jid]["is_small"]]
    return small + big


def _pump():
    """Start as many queued jobs as the rules allow. Call under _lock."""
    started = True
    while started:
        started = False
        for jid in _ordered_queue():
            if _can_start(_jobs[jid]):
                _queue.remove(jid)
                _active.add(jid)
                _jobs[jid]["status"] = "running"
                _jobs[jid]["position"] = 0
                threading.Thread(target=_run, args=(jid,), daemon=True).start()
                started = True
                break
    _recompute_positions()


def _recompute_positions():
    """Assign 1-based queue positions in the order jobs will actually start."""
    for pos, jid in enumerate(_ordered_queue(), start=1):
        _jobs[jid]["position"] = pos


def _finish(jid):
    """Called when a job's worker ends: free its slot and start the next one."""
    with _lock:
        _active.discard(jid)
        _pump()


# --------------------------------------------------------------------------- #
# Job registry
# --------------------------------------------------------------------------- #
def _update(jid, **kw):
    with _lock:
        j = _jobs.get(jid)
        if j:
            j.update(kw)


def get_status(jid):
    with _lock:
        j = _jobs.get(jid)
        if not j:
            return None
        return {
            "status": j["status"],          # queued | running | ready | error
            "position": j["position"],
            "queued_total": len(_queue),
            "done": j["done"],
            "total": j["total"],
            "current": j["current"],
            "error": j["error"],
            "bytes": j["bytes"],
            "skipped": j["skipped"],
            "zip_name": j["zip_name"],
        }


def get_result(jid):
    with _lock:
        j = _jobs.get(jid)
        if not j or j["status"] != "ready":
            return None
        return j["zip_path"], j["zip_name"], j.get("mimetype", "application/zip")


def discard(jid):
    """Cancel (if queued/running) and forget a job, deleting any zip."""
    with _lock:
        j = _jobs.pop(jid, None)
        if jid in _queue:
            _queue.remove(jid)
        _active.discard(jid)
        _pump()  # a freed slot may let a waiting job start
    if j and j.get("zip_path"):
        try:
            os.remove(j["zip_path"])
        except OSError:
            pass


def _reap():
    now = time.time()
    stale = []
    with _lock:
        for jid, j in list(_jobs.items()):
            if now - j["created"] > JOB_TTL:
                stale.append(j.get("zip_path"))
                _jobs.pop(jid, None)
                if jid in _queue:
                    _queue.remove(jid)
                _active.discard(jid)
        _pump()
    for path in stale:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Filenames
# --------------------------------------------------------------------------- #
def _safe_arcname(path):
    """
    Sanitize a client-supplied zip path. Keeps at most one folder level (the
    first segment as the folder, the last as the file name), strips characters
    that are illegal on common filesystems, and preserves spaces and dashes so
    names like "Ep2412 - Title.mp3" survive intact.
    """
    parts = [p for p in re.split(r"[\\/]+", str(path)) if p not in ("", ".", "..")]
    if not parts:
        return "file"
    if len(parts) >= 2:
        parts = [parts[0], parts[-1]]
    clean = []
    for p in parts:
        p = re.sub(r'[<>:"|?*\x00-\x1f]', "", p).strip().strip(".")
        clean.append(p[:150] or "file")
    return "/".join(clean)


def _dedupe_name(arc, used):
    """Ensure unique names inside the zip (two episodes can share a title)."""
    if arc not in used:
        used.add(arc)
        return arc
    base, dot, ext = arc.rpartition(".")
    stem = base if dot else arc
    suffix = ("." + ext) if dot else ""
    i = 2
    while True:
        candidate = f"{stem} ({i}){suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def _fetch(url, dest, headers):
    with requests.get(url, headers=headers, stream=True, timeout=PER_FILE_TIMEOUT) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    f.write(chunk)
    return os.path.getsize(dest)


def _download_one(item):
    url = item["url"]
    path = item["path"]
    expected = item.get("duration") or 0
    fd, tmp_path = tempfile.mkstemp(suffix=".part")
    os.close(fd)
    try:
        size = _fetch(url, tmp_path, UA)

        # Strip dynamically inserted ads so the zip holds the original episode
        # only. Episodes whose runtime already matches the feed cost a single
        # ffprobe and are left untouched.
        if REMOVE_ADS and expected:
            try:
                def _refetch(dest):
                    _fetch(url, dest, ALT_UA)
                tmp_path, _info = clean.clean_file(tmp_path, float(expected), _refetch)
                size = os.path.getsize(tmp_path)
            except Exception:  # noqa: BLE001 - never fail the download itself
                pass

        return {"ok": True, "path": path, "tmp": tmp_path, "size": size}
    except Exception as exc:  # noqa: BLE001 - report per-file failures, don't crash the job
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return {"ok": False, "path": path, "error": str(exc)}


def _run(jid):
    with _lock:
        job = _jobs.get(jid)
        items = list(job["items"]) if job else None
        single = bool(job["single"]) if job else False
    if items is None:
        _finish(jid)
        return

    # ---- single file: no zip, hand back the audio itself --------------------
    if single:
        try:
            res = _download_one(items[0])
            _update(jid, done=1, current=items[0]["path"])
            if not res["ok"]:
                raise BulkError(res.get("error") or "Download failed")
            name = os.path.basename(_safe_arcname(res["path"]))
            ext = os.path.splitext(name)[1].lower()
            mime = "audio/mp4" if ext in (".m4a", ".mp4") else "audio/mpeg"
            _update(jid, status="ready", zip_path=res["tmp"], zip_name=name,
                    mimetype=mime, bytes=res["size"])
        except Exception as exc:  # noqa: BLE001
            _update(jid, status="error", error=str(exc))
        finally:
            _finish(jid)
        return

    fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    total_bytes = 0
    done = 0
    skipped = 0
    failures = []
    used_names = set()
    try:
        # mp3/m4a are already compressed, so ZIP_STORED keeps CPU low and is fast.
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            with concurrent.futures.ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
                futures = {ex.submit(_download_one, it): it for it in items}
                for fut in concurrent.futures.as_completed(futures):
                    res = fut.result()
                    done += 1
                    if res["ok"]:
                        total_bytes += res["size"]
                        if total_bytes > MAX_TOTAL_BYTES:
                            try:
                                os.remove(res["tmp"])
                            except OSError:
                                pass
                            raise BulkError(
                                "Download exceeded the "
                                f"{MAX_TOTAL_BYTES // (1024 ** 3)} GB size limit. "
                                "Select fewer episodes."
                            )
                        arc = _dedupe_name(_safe_arcname(res["path"]), used_names)
                        zf.write(res["tmp"], arcname=arc)
                        try:
                            os.remove(res["tmp"])
                        except OSError:
                            pass
                    else:
                        skipped += 1
                        failures.append(res["path"] + "  ->  " + res["error"])
                    _update(jid, done=done, current=res["path"],
                            bytes=total_bytes, skipped=skipped)

            if failures:
                zf.writestr(
                    "_skipped.txt",
                    "These files could not be downloaded:\n\n" + "\n".join(failures),
                )

        if skipped == len(items):
            raise BulkError("None of the selected files could be downloaded.")

        _update(jid, status="ready", zip_path=zip_path, done=len(items))
    except Exception as exc:  # noqa: BLE001
        try:
            os.remove(zip_path)
        except OSError:
            pass
        _update(jid, status="error", error=str(exc))
    finally:
        _finish(jid)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def start(items, zip_name="podcasts.zip", single=False):
    _reap()
    if not items:
        raise BulkError("Nothing to download.")
    if len(items) > MAX_FILES:
        raise BulkError(
            f"Too many files ({len(items)}). The limit is {MAX_FILES} per "
            "download. Narrow your selection and try again."
        )
    single = bool(single) and len(items) == 1
    if not single and not zip_name.lower().endswith(".zip"):
        zip_name += ".zip"

    jid = uuid.uuid4().hex
    with _lock:
        _jobs[jid] = {
            "status": "queued",         # queued | running | ready | error
            "position": 0,
            "is_small": len(items) <= SMALL_JOB_FILES,
            "single": single,
            "mimetype": "application/zip",
            "items": list(items),
            "done": 0,
            "total": len(items),
            "current": "",
            "error": None,
            "zip_path": None,
            "zip_name": zip_name,
            "bytes": 0,
            "skipped": 0,
            "created": time.time(),
        }
        _queue.append(jid)
        _pump()
    return jid
