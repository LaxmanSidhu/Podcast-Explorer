"""
Remove dynamically inserted ads so downloads contain the original episode only.

Some hosts (Acast, Megaphone, ART19, Libsyn, ...) assemble the MP3 at request
time, stitching pre/mid/post-roll ads into the episode. The RSS feed still
declares the *original* runtime, which gives us a cheap, reliable signal:

    downloaded duration  >  RSS duration  =>  ads were inserted

Strategy (cheapest step first, so shows without ads cost almost nothing):

  1. Download once. Probe its duration (~50 ms). If it matches the RSS runtime,
     the file is already clean - DONE. This is the path every non-DAI podcast
     takes, and it adds no downloads and no audio processing.

  2. Only if the file is too long: fetch the episode a second time. Ad servers
     rotate creatives, so the two captures share the episode but differ in the
     ad slots. Align them and keep only the audio present in BOTH - that is the
     original episode, with every inserted segment dropped.

  3. Verify the result against the RSS runtime before returning it.

Alignment works on a loudness envelope (8 kHz mono, 20 fps) via normalised
cross-correlation, so it tolerates the re-encoding that stitching introduces.
"""

import os
import re
import shutil
import subprocess
import tempfile

import numpy as np

ENV_HZ = 20          # envelope frames per second
WIN = 6.0            # seconds per comparison window
STEP = 2.0           # seconds between windows
MATCH_SCORE = 0.60   # NCC above this = this window exists in the other capture
TOLERANCE = 20.0     # seconds a file may exceed the RSS runtime before we act
MIN_SEGMENT = 5.0    # ignore kept fragments shorter than this


class CleanError(Exception):
    pass


# --------------------------------------------------------------------------- #
# ffprobe / ffmpeg helpers
# --------------------------------------------------------------------------- #
def _bin(name):
    return os.environ.get(name.upper() + "_BINARY") or shutil.which(name) or name


def probe_duration(path):
    """Duration in seconds, or None if it cannot be determined."""
    try:
        out = subprocess.run(
            [_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=120,
        ).stdout.strip()
        return float(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def envelope(path, hz=ENV_HZ):
    """Loudness envelope: mono 8 kHz, mean |amplitude| per frame."""
    try:
        pcm = subprocess.run(
            [_bin("ffmpeg"), "-v", "error", "-i", path,
             "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
            capture_output=True, timeout=900,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise CleanError(f"Could not decode audio: {exc}")
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = 8000 // hz
    m = len(x) // n
    if m == 0:
        raise CleanError("Empty audio")
    return np.abs(x[:m * n].reshape(m, n)).mean(axis=1)


# --------------------------------------------------------------------------- #
# Alignment
# --------------------------------------------------------------------------- #
def _ncc_max(win, ref):
    """
    Best normalised cross-correlation of `win` at any offset within `ref`.
    Uses FFT so this stays fast on hour-long files.
    """
    L = len(win)
    if len(ref) < L:
        return -1.0
    w = win - win.mean()
    nrm = np.linalg.norm(w)
    if nrm == 0:
        return -1.0
    w = w / nrm

    # numerator: cross-correlation via FFT
    size = 1 << int(np.ceil(np.log2(len(ref) + L)))
    num = np.fft.irfft(np.fft.rfft(ref, size) * np.conj(np.fft.rfft(w, size)), size)
    num = num[:len(ref) - L + 1]

    # denominator: local standard deviation over each window of ref
    c1 = np.concatenate(([0.0], np.cumsum(ref, dtype=np.float64)))
    c2 = np.concatenate(([0.0], np.cumsum(ref.astype(np.float64) ** 2)))
    s1 = c1[L:] - c1[:-L]
    s2 = c2[L:] - c2[:-L]
    var = s2 - (s1 * s1) / L
    den = np.sqrt(np.maximum(var, 1e-9))

    return float(np.max(num / den))


def common_ranges(path_a, path_b, hz=ENV_HZ, win=WIN, step=STEP,
                  score=MATCH_SCORE, min_seg=MIN_SEGMENT):
    """
    Time ranges of A whose audio also appears somewhere in B.
    Everything else in A is an inserted segment (ads).
    """
    A = envelope(path_a, hz)
    B = envelope(path_b, hz)
    wlen = int(win * hz)
    slen = max(1, int(step * hz))
    if len(A) < wlen or len(B) < wlen:
        raise CleanError("Audio too short to align")

    keep = []
    for i in range(0, len(A) - wlen + 1, slen):
        s = _ncc_max(A[i:i + wlen], B)
        keep.append((i / hz, (i + wlen) / hz, s >= score))

    # merge consecutive matching windows into ranges
    ranges = []
    cur = None
    for start, end, ok in keep:
        if ok:
            if cur is None:
                cur = [start, end]
            else:
                cur[1] = end
        elif cur is not None:
            ranges.append(tuple(cur))
            cur = None
    if cur is not None:
        ranges.append(tuple(cur))

    return [(a, b) for a, b in ranges if (b - a) >= min_seg]


def extract_ranges(src, ranges, dest):
    """Concatenate the given time ranges of `src` into `dest` without re-encoding."""
    if not ranges:
        raise CleanError("Nothing left after removing inserted audio")
    tmpdir = tempfile.mkdtemp(prefix="cut_")
    parts = []
    try:
        for idx, (start, end) in enumerate(ranges):
            part = os.path.join(tmpdir, f"p{idx:04d}.mp3")
            subprocess.run(
                [_bin("ffmpeg"), "-v", "error", "-ss", f"{start:.3f}",
                 "-t", f"{end - start:.3f}", "-i", src, "-c", "copy", part, "-y"],
                capture_output=True, timeout=900,
            )
            if os.path.exists(part) and os.path.getsize(part) > 0:
                parts.append(part)
        if not parts:
            raise CleanError("Could not cut the audio")
        listfile = os.path.join(tmpdir, "list.txt")
        with open(listfile, "w") as f:
            for p in parts:
                f.write(f"file '{p}'\n")
        subprocess.run(
            [_bin("ffmpeg"), "-v", "error", "-f", "concat", "-safe", "0",
             "-i", listfile, "-c", "copy", dest, "-y"],
            capture_output=True, timeout=900,
        )
        if not os.path.exists(dest) or os.path.getsize(dest) == 0:
            raise CleanError("Could not reassemble the audio")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return dest


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def has_inserted_audio(path, expected_seconds, tolerance=TOLERANCE):
    """True when the file runs materially longer than the RSS runtime."""
    if not expected_seconds or expected_seconds <= 0:
        return False
    actual = probe_duration(path)
    if actual is None:
        return False
    return actual > (expected_seconds + tolerance)


def clean_file(path, expected_seconds, refetch, tolerance=TOLERANCE):
    """
    Ensure `path` contains only the original episode.

    `refetch(dest)` must download the same episode again to `dest` (ideally
    identifying differently, so the ad server rotates in other creatives).

    Returns (path, info). `path` is unchanged when no ads were found - the
    common case, which costs a single ffprobe.
    """
    info = {"checked": False, "ads_found": False, "removed": 0.0,
            "before": None, "after": None, "note": ""}

    if not expected_seconds or expected_seconds <= 0:
        info["note"] = "no runtime in feed; left untouched"
        return path, info

    actual = probe_duration(path)
    info["checked"] = True
    info["before"] = actual
    if actual is None:
        info["note"] = "could not read duration; left untouched"
        return path, info

    # ---- fast path: matches the feed, nothing inserted -----------------------
    if actual <= expected_seconds + tolerance:
        info["after"] = actual
        info["note"] = "clean"
        return path, info

    # ---- ads present: second capture, then keep only the shared audio --------
    info["ads_found"] = True
    second = path + ".alt.mp3"
    cleaned = path + ".clean.mp3"
    try:
        refetch(second)
        if not os.path.exists(second) or os.path.getsize(second) == 0:
            raise CleanError("second capture failed")

        alt_dur = probe_duration(second)
        # If the second capture is itself clean, just use it.
        if alt_dur and alt_dur <= expected_seconds + tolerance:
            os.replace(second, path)
            info["after"] = alt_dur
            info["removed"] = actual - alt_dur
            info["note"] = "ads removed (clean capture)"
            return path, info

        ranges = common_ranges(path, second)
        extract_ranges(path, ranges, cleaned)
        new_dur = probe_duration(cleaned)
        if new_dur and new_dur <= expected_seconds + max(tolerance, 45):
            os.replace(cleaned, path)
            info["after"] = new_dur
            info["removed"] = actual - new_dur
            info["note"] = "ads removed (aligned)"
        else:
            info["note"] = "ads detected but could not be removed reliably"
            info["after"] = actual
    except Exception as exc:  # noqa: BLE001 - never fail the download itself
        info["note"] = f"ad removal skipped: {exc}"
        info["after"] = actual
    finally:
        for p in (second, cleaned):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    return path, info
