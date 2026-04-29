# AGENTS.md — Skool Classroom Downloader

## What This Is
A CLI to back up a Skool classroom: videos always, lesson descriptions and
attached files when `--include-extras` is passed. Uses Playwright (browser
automation) to extract Mux video tokens and Skool's file API from the
Next.js data layer, then `ffmpeg` to copy HLS streams to MP4.

## When to Use
- User asks to download Skool course videos.
- User asks to scrape or save Skool classroom content.
- User mentions backing up course content (videos + descriptions + PDFs)
  from Skool.

## Prerequisites
- **Python 3.10+** — `python3 --version`
- **Playwright** — `pip install playwright && playwright install chromium`
- **ffmpeg** — `ffmpeg -version`
- For `--profile` mode (or the visible-window default on Linux): a working
  system Chrome install. For `--server` mode: not needed.

## Quick Start

```bash
cd /path/to/skool-scraper

# First run with a managed profile (login once, cookies persist locally)
python skool_download.py https://www.skool.com/GROUP/classroom \
    --profile-dir ./skool-profile --headed --list

# Use the user's system Chrome (already logged into Skool)
python skool_download.py https://www.skool.com/GROUP/classroom --profile

# Headless (server / CI), reusing cookies from a prior login
python skool_download.py https://www.skool.com/GROUP/classroom \
    --profile-dir ./skool-profile --server

# Download a specific course
python skool_download.py URL --course "Course Name"

# Include lesson descriptions and attached files
python skool_download.py URL --profile-dir ./skool-profile --include-extras

# Dry run / smoke test
python skool_download.py URL --profile-dir ./skool-profile --max-videos 1 --dry-run
```

`--profile` and `--profile-dir` are mutually exclusive. `--server` requires
`--profile-dir`.

## Architecture

### Module layout
- `skool_download.py` — CLI: argparse, browser launch, the per-course /
  per-lesson loop, ffmpeg invocation. Imports helpers from `skool_shared`.
- `skool_shared.py` — pure helpers and Playwright evaluators:
  - `prosemirror_to_markdown()` (with doctests)
  - `is_logged_in()` / `wait_for_login()`
  - `extract_accessible_courses()` / `extract_course_modules()` /
    `fetch_full_module_detail()`
  - `fetch_video_token()` / `fetch_signed_file_url()` /
    `download_file_via_browser()`
  - `open_classroom_page()` and `navigate_to_module()` wrap navigation
    with the right `wait_until` / `state` flags.

### Skool data layout
- Skool is a **Next.js** app — page data lives in `<script id="__NEXT_DATA__">`.
- Videos are on **Mux** with signed JWT tokens; tokens expire in ~15 minutes.
- Mux CDN (`stream.mux.com`) requires a `Referer: https://www.skool.com/`
  header but does **not** restrict by IP.
- Skool itself (`www.skool.com`) blocks datacenter IPs via Cloudflare WAF.

### Course structure
```
Classroom
  └── Course (unitType: "course")
        └── Section (unitType: "set") — optional
              └── Lesson (unitType: "module") — may or may not have videoId
```
Numbering in the output matches the Skool UI lesson order — text-only
intros and non-video lessons keep their slot in the sequence.

### Token / data flows
1. Navigate to `https://www.skool.com/{group}/classroom` → read
   `renderData.allCourses`. (Important: this URL must be the *root*; the
   `?md=` URL only carries data for the selected module.)
2. For each course, navigate to `/classroom/{slug}` → read
   `renderData.course` and walk the tree.
3. For each video module: fetch
   `/_next/data/{buildId}/{group}/classroom/{slug}.json?md={moduleId}&group={group}`
   → returns `renderData.video.{playbackId, playbackToken, expire, duration}`.
4. ffmpeg copies the HLS stream:
   ```
   ffmpeg -headers "Referer: https://www.skool.com/\r\n" \
          -i https://stream.mux.com/{playbackId}.m3u8?token={token} \
          -c copy -bsf:a aac_adtstoasc -movflags +faststart out.mp4
   ```
5. Files (PDFs, etc.) come from `POST
   https://api2.skool.com/files/{file_id}/download-url?expire=28800`. The
   response is a CloudFront signed URL as `text/plain`. `GET` returns 405.
6. Lesson descriptions live in `metadata.desc` as `[v2]<JSON>`
   ProseMirror v2; converted by `prosemirror_to_markdown`.

### Key gotchas
- **Auth gate is data-shaped, not URL-shaped.** Skool serves classroom HTML
  to logged-out users too; check `pp.self?.user?.id` *or*
  `renderData.allCourses` instead of looking for `/login` in the URL.
- **`networkidle` is unreliable.** Skool keeps long-lived sockets open
  for analytics; use `wait_until="domcontentloaded"`.
- **`#__NEXT_DATA__` is never visible.** Wait with `state="attached"`,
  not the default `"visible"`.
- **`renderData.course` only fully populates the *selected* module.**
  Other modules' `desc` and `resources` may be truncated. For the extras
  flow we navigate to each `?md={moduleId}` to read the full metadata.
- **`renderData.allCourses` only appears on the classroom *root* URL**
  (no `?md=`). The auth check after re-navigation should happen on the
  root URL.
- **Course slugs in URLs are hashed** (e.g., `6efd5648`); read them from
  `allCourses[].name`, never construct them.
- **Locked / paid courses** have `metadata.hasAccess !== 1`; we filter
  these out automatically.
- **ffmpeg needs `-bsf:a aac_adtstoasc`** to mux AAC audio into MP4 cleanly.

## Output Structure
```
downloads/
  Course Title/
    _course.md                         # extras only
    001 - Lesson Title.md              # extras only (description)
    001 - Lesson Title.mp4             # video
    002 - Lesson Title - resource.pdf  # extras only (attached file)
    Section Title/
      003 - Lesson Title.mp4
```

## Files
| File | Purpose |
|------|---------|
| `skool_download.py` | CLI entrypoint: argparse, browser launch, download loop |
| `skool_shared.py` | Helpers: ProseMirror converter, page evaluators, file API |
| `README.md` | User-facing setup and usage |
| `AGENTS.md` | This file |

## Validation
- `python skool_shared.py` runs the doctests for `prosemirror_to_markdown`.
- `python -m doctest skool_shared.py -v` for verbose doctest output.
- `python skool_download.py URL --profile-dir DIR --list` is the cheapest
  end-to-end smoke test.
- `python skool_download.py URL --profile-dir DIR --max-videos 1` confirms
  a real download path works.

## Troubleshooting
- **403 from Skool** → Datacenter IP blocked. Use a residential IP, or run
  on a workstation with `--profile`.
- **403 from Mux** → Token expired or missing Referer; re-run.
- **No videos found** → Course may be locked or be a paid add-on
  (`hasAccess !== 1`). Verify with `--list`.
- **Partial downloads** → Re-run; existing files (>10 KiB videos, >1 KiB
  files) are skipped.
- **Login times out** → Make sure you finished login and are on the
  classroom page; the script polls every 3 s and reloads every 30 s.
- **Headless run sees 0 courses** → Skool sometimes minimizes the SSR
  payload for headless contexts. Run interactively once with
  `--profile-dir` to seed cookies, then switch to `--server`.
