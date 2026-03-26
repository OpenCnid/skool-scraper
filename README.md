# Skool Video Downloader

Downloads all course videos from a Skool classroom.

## How it works

1. **Playwright** opens Skool in a real browser (with your login session)
2. Extracts the course structure from Next.js server data (`__NEXT_DATA__`)
3. For each lesson, fetches a signed Mux video token via internal API
4. **ffmpeg** downloads the HLS stream and saves as MP4

## Setup

```bash
# Python deps
pip install playwright
playwright install chromium

# ffmpeg
brew install ffmpeg    # macOS
sudo apt install ffmpeg  # Linux
```

## Usage

```bash
# Download all courses (opens browser window for login)
python skool_download.py https://www.skool.com/YOUR-GROUP/classroom --headed

# Use existing Chrome profile (no login needed if already logged in)
python skool_download.py https://www.skool.com/YOUR-GROUP/classroom --profile

# List available courses
python skool_download.py https://www.skool.com/YOUR-GROUP/classroom --list

# Download specific course only
python skool_download.py https://www.skool.com/YOUR-GROUP/classroom --course "6-Week"

# Dry run (show what would download)
python skool_download.py https://www.skool.com/YOUR-GROUP/classroom --dry-run
```

## Output structure

```
downloads/
  Course Name/
    Section Name/
      001 - Lesson Title.mp4
      002 - Lesson Title.mp4
    Another Section/
      003 - Lesson Title.mp4
```

## Features

- **Resume support** — skips already-downloaded files
- **Per-course filtering** — download just what you need
- **Token management** — fetches fresh tokens per-lesson (they expire in ~15 min)
- **No re-encoding** — ffmpeg copies streams directly (fast)
- **Rate limiting** — configurable delay between API calls (default 0.3s)

## Files

| File | Purpose |
|------|---------|
| `skool_download.py` | Main script — Playwright + ffmpeg, fully self-contained |
| `download_course.py` | Downloads from a pre-extracted JSON manifest |
| `scrape_via_browser.py` | Alternative: downloads from browser-extracted manifest |

## For OpenClaw users

This can be run via OpenClaw's browser automation — the browser extracts tokens
and ffmpeg downloads on the server. See the orchestration approach in the codebase.
