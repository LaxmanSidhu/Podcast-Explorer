"""
Audio helpers: probe duration, and cut clips from a remote audio URL with
FFmpeg without downloading the whole file.

FFmpeg reads over HTTP using range requests, so:
  - "first N minutes"  -> read from the start and stop after N (no full download)
  - "from A to B"       -> input-seek to A, then read the needed span only
  - "last N minutes"    -> compute start from the total duration, then seek

Clips are always re-encoded to MP3 so the output plays everywhere, regardless
of the source container (mp3, m4a/aac, etc.).
"""

import os
import re
import shutil
import tempfile
import subprocess


def _resolve_binary(env_name, name):
    """
    Find an executable. Priority:
      1. An explicit path in the given environment variable (best for Windows,
         where FFmpeg is often installed but not on PATH).
      2. A match on the system PATH.
      3. The bare name as a last resort (subprocess will then raise a clear
         error we translate into an AudioError).
    """
    override = os.environ.get(env_name)
    if override:
        return override
    return shutil.which(name) or name


FFMPEG = _resolve_binary("FFMPEG_BINARY", "ffmpeg")
FFPROBE = _resolve_binary("FFPROBE_BINARY", "ffprobe")
UA = "Mozilla/5.0 (compatible; PodcastExplorer/1.0)"

# Guard rails so a single clip request can't tie up the box indefinitely.
MAX_CLIP_SECONDS = 60 * 60          # 1 hour hard cap per clip
FFMPEG_TIMEOUT = 60 * 10            # 10 minute wall-clock cap


class AudioError(Exception):
    """Raised when probing or clipping fails."""


def _binary_exists(path):
    """True if `path` resolves to a runnable executable."""
    if os.path.isfile(path):
        return True
    return shutil.which(path) is not None


def ensure_available():
    """
    Raise AudioError with install guidance if FFmpeg/ffprobe cannot be found.
    Call this before attempting to probe or clip.
    """
    missing = []
    if not _binary_exists(FFMPEG):
        missing.append("ffmpeg")
    if not _binary_exists(FFPROBE):
        missing.append("ffprobe")
    if missing:
        raise AudioError(
            "FFmpeg is required for clips, but " + " and ".join(missing) +
            " could not be found. Install FFmpeg and make sure it is on your "
            "PATH, or set the FFMPEG_BINARY (and FFPROBE_BINARY) environment "
            "variable to the full path of the executable. On Windows, that path "
            r"usually looks like C:\ffmpeg\bin\ffmpeg.exe."
        )


def parse_timecode(value):
    """
    Parse a user-supplied time into seconds.

    Accepts: "90" (seconds), "1:30" (M:SS), "01:02:03" (H:MM:SS),
    or a float. Returns a float number of seconds. Raises AudioError on junk.
    """
    if value is None or value == "":
        raise AudioError("Empty time value")
    text = str(value).strip()
    if ":" in text:
        parts = text.split(":")
        try:
            parts = [float(p) for p in parts]
        except ValueError as exc:
            raise AudioError(f"Invalid time: {text}") from exc
        seconds = 0.0
        for p in parts:
            seconds = seconds * 60 + p
        return seconds
    try:
        return float(text)
    except ValueError as exc:
        raise AudioError(f"Invalid time: {text}") from exc


def probe_duration(url):
    """
    Return the total duration of a remote audio file in seconds (float),
    or None if it cannot be determined.
    """
    cmd = [
        FFPROBE, "-v", "error",
        "-user_agent", UA,
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        url,
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=True
        )
        value = out.stdout.strip()
        return float(value) if value else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def resolve_window(mode, total=None, start=None, end=None, duration=None):
    """
    Translate a clip request into (start_seconds, length_seconds).

    mode:
      first_2  / first_5           -> fixed first-N-minute clips
      last_5   / last_10           -> last-N-minute clips (needs `total`)
      range                        -> start..end
      duration                     -> start + duration
    """
    presets = {
        "first_2": (0.0, 120.0),
        "first_5": (0.0, 300.0),
        "last_5": ("last", 300.0),
        "last_10": ("last", 600.0),
    }

    if mode in presets:
        start_val, length = presets[mode]
        if start_val == "last":
            if not total or total <= 0:
                raise AudioError("Total duration unknown; cannot cut from the end")
            start_seconds = max(0.0, total - length)
            length = min(length, total)
        else:
            start_seconds = 0.0
        return start_seconds, length

    if mode == "range":
        start_seconds = parse_timecode(start)
        end_seconds = parse_timecode(end)
        if end_seconds <= start_seconds:
            raise AudioError("End time must be after start time")
        return start_seconds, end_seconds - start_seconds

    if mode == "duration":
        start_seconds = parse_timecode(start)
        length = parse_timecode(duration)
        if length <= 0:
            raise AudioError("Duration must be positive")
        return start_seconds, length

    raise AudioError(f"Unknown clip mode: {mode}")


def cut_clip(url, start_seconds, length_seconds):
    """
    Cut a clip and return the path to a temporary .mp3 file.

    The caller is responsible for deleting the returned file when done.
    """
    length_seconds = min(float(length_seconds), MAX_CLIP_SECONDS)
    if length_seconds <= 0:
        raise AudioError("Clip length must be positive")

    fd, out_path = tempfile.mkstemp(suffix=".mp3", prefix="clip_")
    os.close(fd)

    # -ss BEFORE -i does a fast input seek (uses HTTP range requests).
    # The reconnect flags keep the pull alive across the dropped connections
    # and mid-stream redirects that podcast CDNs commonly throw.
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-user_agent", UA,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-ss", f"{max(0.0, float(start_seconds)):.3f}",
        "-i", url,
        "-t", f"{length_seconds:.3f}",
        "-vn",
        "-c:a", "libmp3lame", "-b:a", "128k",
        out_path,
    ]
    try:
        subprocess.run(
            cmd, capture_output=True, timeout=FFMPEG_TIMEOUT, check=True
        )
    except subprocess.CalledProcessError as exc:
        _cleanup(out_path)
        stderr = (exc.stderr or b"").decode("utf-8", "ignore")[-500:]
        raise AudioError(f"FFmpeg failed: {stderr}") from exc
    except subprocess.TimeoutExpired as exc:
        _cleanup(out_path)
        raise AudioError("Clip generation timed out") from exc
    except FileNotFoundError as exc:
        _cleanup(out_path)
        raise AudioError(
            "FFmpeg was not found. Install FFmpeg and make sure it is on your "
            "PATH, or set the FFMPEG_BINARY environment variable to the full "
            "path of ffmpeg.exe."
        ) from exc
    except OSError as exc:
        _cleanup(out_path)
        raise AudioError(f"Could not run FFmpeg: {exc}") from exc

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        _cleanup(out_path)
        raise AudioError("FFmpeg produced an empty clip")

    return out_path


def _cleanup(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def safe_filename(name, fallback="clip", ext="mp3"):
    """Turn an episode title into a safe download filename, keeping spaces."""
    name = re.sub(r"[^\w\s.-]", "", str(name or ""))   # drop illegal chars
    name = re.sub(r"\s+", " ", name).strip()            # collapse whitespace, keep spaces
    name = name[:120].strip(" .-_") or fallback
    return f"{name}.{ext}"
