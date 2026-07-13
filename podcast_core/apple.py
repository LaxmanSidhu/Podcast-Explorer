"""
Apple Podcasts helpers: URL parsing, iTunes Lookup API, and page scraping.

Ported and hardened from the original Jupyter notebooks:
  - Extract podcast / episode IDs from an Apple Podcasts URL
  - Look up podcast metadata (incl. the RSS `feedUrl`) via the iTunes Lookup API
  - Best-effort scrape of the Apple page for description / rating / review count
"""

import re
import requests

USER_AGENT = "Mozilla/5.0 (compatible; PodcastExplorer/1.0)"
HEADERS = {"User-Agent": USER_AGENT}
LOOKUP_URL = "https://itunes.apple.com/lookup"
TIMEOUT = 15

# Matches the trailing `id360084272` in an Apple Podcasts URL path.
_SHOW_ID_RE = re.compile(r"/id(\d+)")
# Matches the `?i=1000123456789` episode id in the query string.
_EPISODE_ID_RE = re.compile(r"[?&]i=(\d+)")
# Matches the storefront country code, e.g. /us/ or /in/.
_STOREFRONT_RE = re.compile(r"podcasts\.apple\.com/([a-z]{2})/")
# A bare numeric id (someone may paste just the id).
_BARE_ID_RE = re.compile(r"^\d+$")


class AppleError(Exception):
    """Raised when an Apple URL cannot be parsed or looked up."""


def parse_apple_url(raw):
    """
    Parse an Apple Podcasts show OR episode URL (or a bare id).

    Returns a dict:
      {
        "show_id":     "360084272"   (str, always present if resolvable),
        "episode_id":  "100012345"   (str or None),
        "storefront":  "us",         (defaults to "us"),
        "kind":        "episode"|"show",
        "raw":         original input,
      }

    Raises AppleError if no id can be found.
    """
    if raw is None:
        raise AppleError("Empty URL")
    text = str(raw).strip()
    if not text:
        raise AppleError("Empty URL")

    # Bare numeric id pasted directly.
    if _BARE_ID_RE.match(text):
        return {
            "show_id": text,
            "episode_id": None,
            "storefront": "us",
            "kind": "show",
            "raw": text,
        }

    show_match = _SHOW_ID_RE.search(text)
    episode_match = _EPISODE_ID_RE.search(text)
    storefront_match = _STOREFRONT_RE.search(text)

    if not show_match:
        raise AppleError(f"Could not find a podcast id in: {text}")

    episode_id = episode_match.group(1) if episode_match else None
    return {
        "show_id": show_match.group(1),
        "episode_id": episode_id,
        "storefront": storefront_match.group(1) if storefront_match else "us",
        "kind": "episode" if episode_id else "show",
        "raw": text,
    }


def lookup_show(show_id, storefront="us"):
    """
    Look up a podcast by its numeric id via the iTunes Lookup API.

    Returns a normalized metadata dict, including `feed_url` (the RSS feed).
    Raises AppleError if the id is not found.
    """
    params = {"id": str(show_id).strip(), "entity": "podcast", "country": storefront}
    resp = requests.get(LOOKUP_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        # Retry without the country filter (some ids are storefront-specific).
        resp = requests.get(
            LOOKUP_URL,
            params={"id": str(show_id).strip()},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    if not results:
        raise AppleError(f"No podcast found for id {show_id}")

    item = results[0]
    artwork = (
        item.get("artworkUrl600")
        or item.get("artworkUrl100")
        or item.get("artworkUrl60")
        or ""
    )
    release = item.get("releaseDate", "") or ""
    return {
        "podcast_id": str(item.get("collectionId", show_id)),
        "podcast_name": item.get("collectionName", ""),
        "artist_name": item.get("artistName", ""),
        "apple_url": item.get("collectionViewUrl", ""),
        "feed_url": item.get("feedUrl", "") or "",
        "artwork": artwork,
        "primary_genre": item.get("primaryGenreName", ""),
        "genres": ", ".join(item.get("genres", []) or []),
        "country": item.get("country", ""),
        "release_date": release.split("T")[0] if release else "",
        "episode_count": item.get("trackCount", "") or "",
    }


def lookup_episode(episode_id, storefront="us"):
    """
    Look up a single episode via the iTunes Lookup API.

    Returns a dict with title / audio_url / guid / release date, or None.
    Used to focus on one episode when an episode URL is supplied.
    """
    params = {"id": str(episode_id).strip(), "entity": "podcastEpisode"}
    try:
        resp = requests.get(LOOKUP_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException:
        return None

    for item in results:
        if item.get("wrapperType") == "podcastEpisode" or item.get("kind") == "podcast-episode":
            return {
                "episode_id": str(episode_id),
                "title": item.get("trackName", ""),
                "audio_url": item.get("episodeUrl", "") or "",
                "guid": item.get("episodeGuid", "") or "",
                "release_date": (item.get("releaseDate", "") or "").split("T")[0],
            }
    return None


def scrape_show_page(apple_url):
    """
    Best-effort scrape of an Apple Podcasts page for fields not in the API:
    long description, star rating, and review count.

    Never raises; returns {} on any failure so it can be treated as optional
    enrichment on top of the Lookup API data.
    """
    if not apple_url:
        return {}
    try:
        from bs4 import BeautifulSoup

        resp = requests.get(apple_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        description = (
            soup.select_one("div.description p.content")
            or soup.select_one("div.description")
            or soup.select_one("meta[name='description']")
        )
        if description is not None and description.name == "meta":
            desc_text = description.get("content", "")
        else:
            desc_text = description.get_text(strip=True) if description else ""

        rating_el = soup.select_one("li[aria-label*='out of']")
        rating_text = rating_el.get("aria-label", "") if rating_el else ""
        rating_value, review_count = None, None
        match = re.search(r"([\d.]+)\s*out of\s*5,\s*([\d,]+)", rating_text, re.I)
        if match:
            rating_value = match.group(1)
            review_count = match.group(2).replace(",", "")

        return {
            "description": _clean_text(desc_text),
            "rating": rating_value,
            "review_count": review_count,
        }
    except Exception:
        return {}


def _clean_text(text):
    """Fix common mojibake and collapse whitespace (from the notebook cleaner)."""
    if not isinstance(text, str):
        return text
    replacements = {
        "\u00e2\u0080\u0099": "'", "\u00e2\u0080\u009c": '"', "\u00e2\u0080\u009d": '"',
        "\u00e2\u0080\u0093": "-", "\u00e2\u0080\u0094": "-", "\u00e2\u0080\u00a6": "...",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    text = re.sub(r"\s+", " ", text).strip()
    return text
