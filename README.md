# Skool Classroom Downloader

Downloads videos from a Skool classroom. With `--include-extras` it also
saves lesson descriptions as Markdown and downloads attached files (PDFs,
slides, etc.).

## How it works

1. **Playwright** opens Skool with your logged-in session.
2. The classroom page exposes the course catalog in `__NEXT_DATA__`
   (Next.js SSR).
3. For each lesson, a fresh Mux playback token is fetched via the internal
   Next.js data route.
4. **ffmpeg** copies the HLS stream to MP4 (no re-encoding).
5. With `--include-extras` we additionally read each module's full metadata
   from its module page (`?md={moduleId}`), write the lesson description
   as Markdown, and download attached files via Skool's file API.

## Setup

```bash
pip install playwright
playwright install chromium

# ffmpeg
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Linux
winget install ffmpeg          # Windows
```

The system Chrome browser is only required for the `--profile` mode and
for the visible-window default on Linux desktops; `--server` and the
ephemeral default both use Playwright's bundled Chromium.

## Usage

```bash
# First run: dedicated managed profile (login once, cookies persist)
python skool_download.py https://www.skool.com/GROUP/classroom \
    --profile-dir ./skool-profile --headed

# Reuse your system Chrome profile (already logged in)
python skool_download.py https://www.skool.com/GROUP/classroom --profile

# Headless server mode (after a prior login populated the profile)
python skool_download.py https://www.skool.com/GROUP/classroom \
    --profile-dir ./skool-profile --server

# Download only a specific course
python skool_download.py URL --course "Week 1"

# Also export descriptions and attached files
python skool_download.py URL --profile-dir ./skool-profile --include-extras

# Smoke tests
python skool_download.py URL --profile-dir ./skool-profile --list
python skool_download.py URL --profile-dir ./skool-profile --max-videos 1 --dry-run
```

### Profile modes

| Flag | What it uses | Best for |
|------|--------------|----------|
| (none) | Ephemeral browser context | One-off runs; you'll need to log in interactively each time |
| `--profile` | Your system Chrome profile | You're already logged in to Skool in Chrome and want zero setup |
| `--profile-dir PATH` | A managed Playwright profile at PATH | Repeatable / scripted use; first run logs in, future runs reuse cookies |
| `--profile-dir PATH --server` | The same managed profile, headless via bundled Chromium | VPS / CI / no-display environments |

`--profile` and `--profile-dir` are mutually exclusive. `--server`
requires `--profile-dir`.

## Output structure

```
downloads/
└── Course Title/
    ├── _course.md                          # course overview (extras)
    ├── 001 - Lesson Title.md               # lesson description (extras)
    ├── 001 - Lesson Title.mp4              # video
    ├── 002 - Lesson Title.md               # extras
    ├── 002 - Lesson Title.mp4
    ├── 002 - Lesson Title - resource.pdf   # attached file (extras)
    └── ...
```

Lesson numbering follows the **Skool UI lesson order** for the course, so
that a video, its description, and any attached files for the same lesson
share the `NNN -` prefix. Modules without a video (e.g. text-only intros,
exercise guides, or PDF-only lessons) keep their position; with
`--include-extras` they show up as `.md` / file outputs at the same index.

If a course is organized into sections, lessons are written under
`Course Title/Section Title/`.

## Features

- **Resume support** — re-running skips already-downloaded videos and files.
- **Per-course filtering** via `--course`.
- **No re-encoding** — `ffmpeg -c copy` muxes the HLS stream straight into MP4.
- **Robust auth detection** — Skool serves classroom HTML to logged-out users,
  so the URL is not a reliable gate; we look for the actual logged-in
  signals in `__NEXT_DATA__`.
- **Rate limiting** via `--delay` (default 0.3s between API calls).
- **Optional extras** — lesson descriptions and attached files behind
  `--include-extras`.
- **Server mode** for VPS/CI use without a system Chrome.

## Files

| File | Purpose |
|------|---------|
| `skool_download.py` | CLI entrypoint: argparse, browser launch, download loop |
| `skool_shared.py` | Helpers: ProseMirror→Markdown, Skool page evaluators, file API |
| `AGENTS.md` | Internal notes: architecture, gotchas, contribution guide |

## Development

The ProseMirror→Markdown converter ships with doctests:

```bash
python -m doctest skool_shared.py -v
# or:
python skool_shared.py
```

Quick smoke test against a real classroom (does not download anything):

```bash
python skool_download.py URL --profile-dir ./skool-profile --list
python skool_download.py URL --profile-dir ./skool-profile --max-videos 1 --dry-run
```

## Troubleshooting

- **403 from Skool** → Datacenter IP blocked by Cloudflare; use a residential IP
  or run on a workstation with `--profile`.
- **403 from Mux** → Token expired (15 min lifetime). Re-run; tokens are
  fetched per-lesson.
- **No videos found** → Course might be locked (`hasAccess !== 1`) or
  text-only. Check `--list` output.
- **Login times out** → Make sure you completed login and returned to the
  classroom page; the script polls every 3s and reloads every 30s.
- **Headless run sees fewer courses than headed** → Skool sometimes minimizes
  the SSR payload for headless requests. Run with `--profile-dir` interactively
  once to seed cookies, then use `--server` for unattended runs.
