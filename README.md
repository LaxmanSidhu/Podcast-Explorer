# Podcast Explorer

Paste one or more Apple Podcasts URLs and instantly get podcast metadata, the
RSS feed, a full episode table with inline playback, full downloads, and
custom-length audio clips — through a clean Flask web app.

It is a web version of the logic in your `Apple_related_Scraping` and
`Get_everything_about_podcasts_from_IDs` notebooks: Apple URL parsing, the
iTunes Lookup API, RSS parsing, and episode metadata extraction.

## Features

- **Single or bulk input** — paste one URL, many URLs (newline or comma
  separated), or upload a **CSV / Excel** file. Show URLs, episode URLs, and
  bare ids all work.
- **Podcast cards** — artwork, title, publisher, genre, episode count, and RSS
  availability. Click a card to open its episodes.
- **Episode table** — title, description, duration, publish date, an inline
  audio player, a full download, and a clip button per episode. Includes a live
  text filter.
- **Custom clips** — download the first 2 / 5 minutes, the last 5 / 10 minutes,
  or any custom start-to-end window. Clips are cut with FFmpeg over HTTP range
  requests, so the whole episode is never downloaded.
- **RSS access** — view, copy, or download the raw feed XML.

## Requirements

- Python 3.9+
- **FFmpeg** and **ffprobe** on your `PATH` (required only for the clip feature;
  full downloads and RSS work without it)

### Installing FFmpeg

- **macOS:** `brew install ffmpeg`
- **Ubuntu/Debian:** `sudo apt install ffmpeg`
- **Windows:**
  1. Download a build from https://www.gyan.dev/ffmpeg/builds/ (the
     "release full" zip) or https://ffmpeg.org/download.html.
  2. Unzip it somewhere permanent, e.g. `C:\ffmpeg`. Inside you will find a
     `bin` folder containing `ffmpeg.exe` and `ffprobe.exe`.
  3. Add that `bin` folder to your PATH: Start menu -> "Edit the system
     environment variables" -> Environment Variables -> select `Path` -> Edit
     -> New -> paste `C:\ffmpeg\bin` -> OK. Then open a **new** terminal.
  4. Verify with `ffmpeg -version`.

If you would rather not touch PATH, point the app straight at the executables
with environment variables before starting it:

```bat
set FFMPEG_BINARY=C:\ffmpeg\bin\ffmpeg.exe
set FFPROBE_BINARY=C:\ffmpeg\bin\ffprobe.exe
python app.py
```

The app prints a warning at startup if it cannot find FFmpeg, and the clip
dialog shows a clear message rather than failing silently.

## Setup

```bash
cd podcast_explorer
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

## Project structure

```
podcast_explorer/
├── app.py                  # Flask app + all API routes
├── requirements.txt
├── README.md
├── podcast_core/           # reusable logic (importable, no Flask needed)
│   ├── apple.py            # URL parsing, iTunes Lookup, page scraping
│   ├── rss.py              # feed fetching + episode parsing
│   ├── audio.py            # ffmpeg clip cutting + duration probing
│   └── inputs.py           # bulk input: text / CSV / Excel
├── templates/
│   └── index.html
└── static/
    ├── css/style.css
    └── js/app.js
```

## API endpoints

| Method | Route            | Purpose                                             |
| ------ | ---------------- | --------------------------------------------------- |
| GET    | `/`              | The web interface                                   |
| POST   | `/api/process`   | Turn URLs / a file into podcast cards               |
| POST   | `/api/episodes`  | Fetch the episode list for one feed                 |
| GET    | `/api/rss`       | View or download raw feed XML (`?download=1`)        |
| GET    | `/api/download`  | Stream a full episode as an attachment              |
| GET    | `/api/clip`      | Cut a clip: `mode=first_2\|first_5\|last_5\|last_10\|range` |

## Notes

- **Rate limits** — the iTunes Lookup API is public but rate limited. Bulk input
  is capped at 100 podcasts per request; lookups run 10 at a time.
- **Only http(s)** audio and feed URLs are fetched; a per-clip time cap and an
  FFmpeg timeout keep a single request from tying up the server.
- This is an internal tool. If you expose it beyond localhost, put it behind
  auth and consider adding SSRF allow-listing for the fetch endpoints.
```

## Deploying to Railway

This repo includes a `Dockerfile` that installs FFmpeg, so clips work on the
host. Railway auto-detects the Dockerfile and builds from it.

1. Push this project to a GitHub repository.
2. On https://railway.app, create a new project -> **Deploy from GitHub repo**
   and pick the repo. Railway detects the Dockerfile and builds it.
3. When the build finishes, open the service -> **Settings -> Networking** ->
   **Generate Domain** to get a public URL.

No start command or `PORT` setting is needed: the Dockerfile binds gunicorn to
Railway's `$PORT` automatically. FFmpeg is installed inside the image, so the
clip feature works without any extra configuration.

Notes for hosting:

- **Password protection (optional but recommended for public URLs).** Set the
  `APP_PASSWORD` environment variable and every request is protected by HTTP
  Basic auth (any username + that password). If `APP_PASSWORD` is not set, the
  app is open. On Railway/Render add it under the service's **Variables** /
  **Environment** tab. Without it, anyone with the URL can use your server to
  proxy downloads and cut clips.
- Railway containers have **no persistent disk**, which is fine here: clips and
  bulk-download zips are written to temp files and deleted after each request.

## Ad removal (original audio only)

Many hosts (Acast, Megaphone, ART19, Libsyn) assemble the MP3 at request time and
stitch pre/mid/post-roll ads into it, so a download can run several minutes longer
than the episode. The RSS feed still declares the *original* runtime, which the app
uses as a reference:

1. Download once, then probe the duration (a single ffprobe, ~50 ms). If it matches
   the feed's runtime the file is already clean and is passed straight through.
   **Podcasts that do not insert ads take this path and pay no extra cost.**
2. Only when the file is too long: fetch the episode a second time under a different
   client identity. Ad servers rotate creatives, so the two captures share the
   episode but differ in the ad slots. The app aligns them and keeps only the audio
   present in both - the original episode, with inserted segments removed.
3. The result is verified against the feed runtime before being returned.

This applies to both the single "Full" download and bulk zips. If anything fails the
original file is returned rather than erroring. Set `REMOVE_ADS=0` to disable and
keep files exactly as the host served them.

Requires FFmpeg (already installed by the Dockerfile).

## Multiple users / concurrency

The app is safe for a shared team link. Browsing, RSS, single downloads and
clips are stateless and isolated per request, and each bulk download is a
separate job with its own temp files. Bulk downloads are throttled by a global
queue so many people clicking at once can't exhaust the server:

- At most `MAX_ACTIVE` (default **3**) bulk jobs download at the same time.
  Everyone else waits in a queue and sees their position; their download starts
  automatically when a slot frees. This caps total bandwidth, disk and threads.
- `RESERVED_SMALL_SLOTS` (default **1**) slot is kept free for "small" jobs
  (`<= SMALL_JOB_FILES`, default **5** files), so quick "grab a few episodes"
  downloads never wait behind someone's giant archive download.
- Each job downloads `DL_WORKERS` (default **8**) files in parallel, so peak
  outbound downloads are `MAX_ACTIVE * DL_WORKERS` (default 3 x 8 = 24).

These constants live at the top of `podcast_core/bulk.py` and can be tuned to
your instance's bandwidth and disk. For a ~10-15 person team the defaults are a
good starting point; give the instance ~2 GB RAM and ~25 GB disk of headroom.

**One instance only.** The job queue and progress live in this process's memory,
so run a **single instance / worker** (the Dockerfile sets `--workers 1`). To
handle more load, give that one instance more CPU/RAM/disk (scale **up**) rather
than adding replicas (scale **out**) - a second replica wouldn't know about the
first's jobs, breaking progress polling. Scaling out would require moving the
job registry to a shared store like Redis.

## Deploying to Render

The same Dockerfile works on Render unchanged (it binds to Render's `$PORT`).

1. Push this project to a GitHub repository.
2. In the Render dashboard: **New -> Web Service**, connect the repo.
3. Render detects the Dockerfile; choose the **Docker** runtime and leave the
   build/start commands blank (the Dockerfile's `CMD` is used). FFmpeg installs
   during the build.
4. Pick a paid instance (avoid the free tier - it sleeps after ~15 min idle,
   which is bad for multi-minute downloads). Keep **instance count = 1**.
5. Add `APP_PASSWORD` (and any other vars) under **Environment**.
6. **Create Web Service.** Render builds and gives you a `*.onrender.com` URL.
