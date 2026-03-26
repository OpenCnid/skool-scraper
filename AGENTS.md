# AGENTS.md — Skool Video Downloader

## What This Is
A tool to download all video content from a Skool classroom. It uses Playwright (browser automation) to extract video tokens from Skool's Next.js data layer, then ffmpeg to download the Mux HLS streams.

## When to Use
- User asks to download Skool course videos
- User asks to scrape or save Skool classroom content
- User mentions backing up course content from Skool

## Prerequisites
Check these before running:
- **Python 3.10+** — `python3 --version`
- **Playwright** — `pip install playwright && playwright install chromium`
- **ffmpeg** — `ffmpeg -version` (install: `brew install ffmpeg` / `apt install ffmpeg`)

## Quick Start

### Option A: Standalone (Playwright — recommended for first run)
```bash
cd /path/to/skool-scraper

# List available courses (uses existing Chrome login)
python skool_download.py https://www.skool.com/GROUP/classroom --profile --list

# Download all courses
python skool_download.py https://www.skool.com/GROUP/classroom --profile --headed

# Download specific course
python skool_download.py https://www.skool.com/GROUP/classroom --profile --course "Course Name"

# Dry run first
python skool_download.py https://www.skool.com/GROUP/classroom --profile --dry-run
```

### Option B: OpenClaw Browser + Server ffmpeg (hybrid)
If running via OpenClaw with browser access:
1. Open the Skool classroom in the browser (user must be logged in)
2. Use the browser `evaluate` to extract course data from `__NEXT_DATA__`
3. For each lesson, fetch video tokens via Next.js data routes: `/_next/data/{buildId}/{group}/classroom/{courseName}.json?md={moduleId}&group={group}`
4. Download with ffmpeg: `ffmpeg -headers "Referer: https://www.skool.com/\r\n" -i "https://stream.mux.com/{playbackId}.m3u8?token={token}" -c copy -bsf:a aac_adtstoasc -movflags +faststart output.mp4`

## Architecture Notes

### How Skool stores videos
- Skool is a **Next.js** app — all page data is in `<script id="__NEXT_DATA__">`
- Videos are hosted on **Mux** (not Vimeo) with signed JWT tokens
- Tokens expire in **~15 minutes** and are tied to a `playback_restriction_id`
- Mux CDN (`stream.mux.com`) requires a `Referer: https://www.skool.com/` header but does **not** restrict by IP
- Skool itself (`www.skool.com`) blocks datacenter IPs via CloudFront WAF — browser or residential IP required for page access

### Course structure
```
Classroom
  └── Course (unitType: "course")
        └── Section (unitType: "set") — e.g., "Week 1"
              └── Lesson (unitType: "module") — has videoId in metadata
```

### Token extraction flow
1. Navigate to classroom page → get `allCourses` from `renderData`
2. Navigate to a course page (`/classroom/{courseName}?md={moduleId}`) → get `course` tree with all lessons
3. For each lesson's `moduleId`, fetch `/_next/data/{buildId}/{group}/classroom/{courseName}.json?md={moduleId}&group={group}` → get `renderData.video` with `playbackId` and `playbackToken`

### Key gotchas
- Course names in URLs are **hashed** (e.g., `6efd5648`), not human-readable — get them from `allCourses[].name`
- The `__NEXT_DATA__` JSON route requires the `md` query param or it redirects
- Some courses are locked (`hasAccess !== 1`) or private (`privacy > 0`) — skip these
- ffmpeg needs `-bsf:a aac_adtstoasc` to properly mux AAC audio into MP4

## Output Structure
```
downloads/
  Course Title/
    Section Name/
      001 - Lesson Title.mp4
      002 - Lesson Title.mp4
```

## Files
| File | Purpose |
|------|---------|
| `skool_download.py` | Standalone Playwright + ffmpeg script |
| `download_course.py` | Download from a pre-extracted JSON manifest |
| `orchestrate.py` | Batch download from multiple manifest files |
| `README.md` | Human-readable setup guide |

## Troubleshooting
- **403 from Skool** → VPN/datacenter IP blocked. Use `--profile` with local Chrome or residential IP.
- **403 from Mux** → Token expired or missing Referer header. Re-extract tokens.
- **No videos found** → Course may be text-only or locked. Check `hasAccess` field.
- **Partial downloads** → Script has resume support. Re-run and it skips completed files.
- **Login required** → Run with `--headed` so user can log in visually, or use `--profile` to reuse existing Chrome session.
