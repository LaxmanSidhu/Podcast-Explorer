"""
RSS feed helpers: fetch a feed, parse episodes, normalize durations,
clean HTML descriptions, and return the raw XML for download.

Ported from the notebook `rss_to_dataframe` / `clean_html` logic and extended
with robust duration handling and per-episode audio metadata.
"""

import re
import requests
import feedparser
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PodcastExplorer/1.0)"}
TIMEOUT = 20


class RSSError(Exception):
    """Raised when a feed cannot be fetched or parsed."""


def clean_html(text):
    """Strip HTML tags and collapse whitespace to a readable plain-text string."""
    if not text:
        return ""
    try:
        soup = BeautifulSoup(text, "html.parser")
        clean = soup.get_text(separator=" ", strip=True)
    except Exception:
        clean = re.sub(r"<[^>]+>", " ", str(text))
    return re.sub(r"\s+", " ", clean).strip()


def parse_duration(raw):
    """
    Normalize an itunes:duration value into total seconds.

    Handles "HH:MM:SS", "MM:SS", plain integer seconds, and floats.
    Returns an int number of seconds, or None if unparseable.
    """
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    if ":" in raw:
        parts = raw.split(":")
        try:
            parts = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for p in parts:
            seconds = seconds * 60 + p
        return int(seconds)
    try:
        return int(float(raw))
    except ValueError:
        return None


def format_duration(seconds):
    """Turn a seconds count into a compact H:MM:SS / M:SS label."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fetch_raw(feed_url):
    """Fetch the raw feed bytes with a browser-like UA (avoids HTML previews)."""
    resp = requests.get(feed_url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.content


def raw_feed_xml(feed_url):
    """Return the raw RSS XML bytes for a feed, for download/copy."""
    if not feed_url:
        raise RSSError("No feed URL provided")
    return _fetch_raw(feed_url)


def fetch_feed(feed_url, limit=None):
    """
    Fetch and parse a podcast RSS feed.

    Returns:
      {
        "feed": {title, author, image, description, link, rss_url, episode_count},
        "episodes": [ {index, guid, title, description, duration_seconds,
                       duration, published, published_iso, audio_url,
                       audio_type, size, link}, ... ]
      }

    `limit` optionally caps the number of episodes returned (newest first).
    """
    if not feed_url:
        raise RSSError("No feed URL provided")

    try:
        raw = _fetch_raw(feed_url)
        parsed = feedparser.parse(raw)
    except requests.RequestException as exc:
        raise RSSError(f"Could not fetch feed: {exc}") from exc

    if parsed.bozo and not parsed.entries:
        raise RSSError("Feed could not be parsed or contains no episodes")

    fmeta = parsed.feed
    image = ""
    if fmeta.get("image"):
        image = fmeta.image.get("href", "") if hasattr(fmeta.image, "get") else ""
    if not image:
        image = fmeta.get("itunes_image", "") or ""

    feed_info = {
        "title": fmeta.get("title", ""),
        "author": fmeta.get("author", "") or fmeta.get("itunes_author", ""),
        "image": image,
        "description": clean_html(fmeta.get("summary", "") or fmeta.get("subtitle", "")),
        "link": fmeta.get("link", ""),
        "rss_url": feed_url,
        "episode_count": len(parsed.entries),
    }

    episodes = []
    for i, entry in enumerate(parsed.entries):
        enclosure = entry.enclosures[0] if entry.get("enclosures") else None
        audio_url = enclosure.get("href", "") if enclosure else ""
        audio_type = enclosure.get("type", "") if enclosure else ""
        size = enclosure.get("length", "") if enclosure else ""

        duration_seconds = parse_duration(
            entry.get("itunes_duration") or entry.get("duration")
        )

        published_iso = ""
        if entry.get("published_parsed"):
            t = entry.published_parsed
            published_iso = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
        elif entry.get("updated_parsed"):
            t = entry.updated_parsed
            published_iso = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"

        raw_desc = entry.get("summary", "") or entry.get("description", "")

        episodes.append({
            "index": i + 1,
            "guid": entry.get("id", "") or entry.get("guid", ""),
            "title": entry.get("title", "").strip(),
            "description": clean_html(raw_desc),
            "duration_seconds": duration_seconds,
            "duration": format_duration(duration_seconds),
            "published": entry.get("published", "") or entry.get("updated", ""),
            "published_iso": published_iso,
            "audio_url": audio_url,
            "audio_type": audio_type,
            "size": size,
            "link": entry.get("link", ""),
        })

    if limit is not None:
        episodes = episodes[:limit]

    return {"feed": feed_info, "episodes": episodes}
