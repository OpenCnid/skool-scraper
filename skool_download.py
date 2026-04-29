#!/usr/bin/env python3
"""
Skool Classroom Downloader
==========================
Downloads videos (and, optionally, lesson descriptions and attached files)
from a Skool classroom using Playwright + ffmpeg.

Setup
-----
    pip install playwright
    playwright install chromium
    # ffmpeg: brew install ffmpeg / sudo apt install ffmpeg

Usage
-----
    # First run with a dedicated managed profile (login once, cookies persist)
    python skool_download.py URL --profile-dir ./skool-profile --headed

    # Use your existing system Chrome profile instead
    python skool_download.py URL --profile

    # Headless on a server (requires cookies in --profile-dir from a prior run)
    python skool_download.py URL --profile-dir ./skool-profile --server

    # Also write lesson descriptions (.md) and download attached files
    python skool_download.py URL --profile-dir ./skool-profile --include-extras

    # Smoke test: list courses, download one video
    python skool_download.py URL --profile-dir ./skool-profile --list
    python skool_download.py URL --profile-dir ./skool-profile --max-videos 1

Architecture
------------
1. Playwright opens Skool with the user's session.
2. ``__NEXT_DATA__`` (Next.js SSR) on ``/classroom`` exposes ``allCourses``.
3. For each course we visit ``/classroom/{slug}`` and walk the module tree.
4. For each video module we fetch a fresh Mux token via the Next.js data
   route, then ffmpeg copies the HLS stream to MP4.
5. With ``--include-extras`` we additionally visit each module's ``?md=``
   URL (Skool only fully populates ``desc``/``resources`` for the selected
   module), write the lesson description as Markdown, and download attached
   files via Skool's file API.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import skool_shared as ss


# --------------------------------------------------------------------------- #
# Browser launch
# --------------------------------------------------------------------------- #

def _system_chrome_profile_path() -> Path:
    """Return the OS-specific path to the user's existing Chrome profile."""
    import os, pathlib
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library/Application Support/Google/Chrome"
    if sys.platform == "win32":
        return pathlib.Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data"
    return pathlib.Path.home() / ".config/google-chrome"


def launch_browser(playwright, args):
    """Return a (context, page) tuple based on the chosen browser mode.

    Mutually exclusive modes (validated in main()):
      * ``--profile``        : reuse the user's system Chrome profile
      * ``--profile-dir DIR``: managed Playwright profile (created on demand)
      * default              : ephemeral context (no persistence)

    ``--server`` requires ``--profile-dir`` and forces headless using the
    bundled chromium (no system Chrome dependency).
    """
    common_args = ["--disable-blink-features=AutomationControlled"]

    if args.profile:
        chrome_path = _system_chrome_profile_path()
        context = playwright.chromium.launch_persistent_context(
            str(chrome_path),
            headless=False,
            channel="chrome",
            args=common_args,
        )
        return context, (context.pages[0] if context.pages else context.new_page())

    if args.profile_dir:
        profile_path = Path(args.profile_dir).expanduser().resolve()
        profile_path.mkdir(parents=True, exist_ok=True)
        first_run = not any(profile_path.iterdir())
        if first_run and not (args.headed or not args.server):
            print(f"   ℹ️  Empty profile dir: {profile_path}")
        launch_kwargs = {"args": common_args}
        if args.server:
            # bundled headless chromium; no system Chrome needed.
            launch_kwargs["headless"] = True
        else:
            # system Chrome lets the user actually see the window on Linux.
            launch_kwargs["channel"] = "chrome"
            launch_kwargs["headless"] = False
        context = playwright.chromium.launch_persistent_context(
            str(profile_path), **launch_kwargs)
        return context, (context.pages[0] if context.pages else context.new_page())

    # Ephemeral
    browser = playwright.chromium.launch(headless=not args.headed)
    context = browser.new_context()
    return context, context.new_page()


# --------------------------------------------------------------------------- #
# Video download
# --------------------------------------------------------------------------- #

def download_video_with_ffmpeg(playback_id: str, token: str,
                               output_path: Path) -> str:
    """ffmpeg copies the Mux HLS stream to ``output_path``.

    Returns ``"ok"``, ``"skip"``, or an error string.
    """
    if output_path.exists() and output_path.stat().st_size > 10 * 1024:
        return "skip"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    hls_url = f"https://stream.mux.com/{playback_id}.m3u8?token={token}"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-headers", "Referer: https://www.skool.com/\r\n",
        "-i", hls_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(output_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        output_path.unlink(missing_ok=True)
        return "timeout"
    except FileNotFoundError:
        print("\n❌ ffmpeg not found. Install via brew/apt/winget and retry.")
        sys.exit(1)
    if (result.returncode == 0 and output_path.exists()
            and output_path.stat().st_size > 1024):
        return "ok"
    output_path.unlink(missing_ok=True)
    return f"ffmpeg_error: {result.stderr[-300:].strip()}"


# --------------------------------------------------------------------------- #
# Per-course processing
# --------------------------------------------------------------------------- #

def process_course(page, args, group: str, course: dict, output_dir: Path,
                   stats: dict) -> bool:
    """Download every lesson of one course. Returns False if the global
    ``--max-videos`` limit is reached so the outer loop can stop."""
    print(f"\n{'=' * 50}")
    print(f"📖 {course['title']}")

    course_url = f"https://www.skool.com/{group}/classroom/{course['slug']}"
    try:
        ss.open_classroom_page(page, course_url)
    except Exception as e:
        print(f"   ❌ Failed to open course page: {e}")
        return True

    try:
        modules = ss.extract_course_modules(page)
    except Exception as e:
        print(f"   ❌ Failed to extract modules: {e}")
        return True

    if not modules:
        print("   ⏭️  No modules found")
        return True

    course_dir = output_dir / ss.sanitize(course["title"])
    course_dir.mkdir(parents=True, exist_ok=True)

    # Number every module by its full-tree position so that videos, files,
    # and descriptions for the same lesson share a prefix that matches the
    # Skool UI lesson order.
    for position, module in enumerate(modules, 1):
        section = ss.sanitize(module.get("section", ""))
        out_dir = course_dir / section if section else course_dir
        basename = ss.format_lesson_basename(position, module["title"])

        if args.include_extras:
            _process_extras(page, args, group, course["slug"], module,
                            out_dir, basename, stats)

        if module.get("videoId"):
            if not _process_video(page, args, group, course["slug"], module,
                                  out_dir, basename, stats):
                # max-videos reached
                return False
    return True


def _process_video(page, args, group: str, course_slug: str, module: dict,
                   out_dir: Path, basename: str, stats: dict) -> bool:
    """Download a single video. Returns False once ``--max-videos`` is hit."""
    output_path = out_dir / f"{basename}.mp4"
    if output_path.exists() and output_path.stat().st_size > 10 * 1024:
        size_mb = output_path.stat().st_size / 1024 / 1024
        print(f"   ⏭️  {basename} ({size_mb:.1f} MB)")
        stats["skip"] += 1
        return True

    time.sleep(args.delay)
    token = ss.fetch_video_token(page, course_slug, module["id"], group)
    if not token or token.get("error") or not token.get("playbackId"):
        err = (token or {}).get("error", "no token")
        print(f"   ❌ {basename}: {err}")
        stats["fail"] += 1
        return True

    duration_min = (token.get("duration") or 0) / 1000 / 60
    print(f"   ⬇️  {basename} ({duration_min:.1f} min)")
    if args.dry_run:
        print(f"      → {output_path}")
        return True

    result = download_video_with_ffmpeg(
        token["playbackId"], token["token"], output_path)
    if result == "ok":
        size_mb = output_path.stat().st_size / 1024 / 1024
        print(f"      ✅ {size_mb:.1f} MB")
        stats["ok"] += 1
    elif result == "skip":
        stats["skip"] += 1
    else:
        print(f"      ❌ {result}")
        stats["fail"] += 1

    if args.max_videos and stats["ok"] >= args.max_videos:
        print(f"\n⏹  Reached --max-videos={args.max_videos}, stopping.")
        return False
    return True


def _process_extras(page, args, group: str, course_slug: str, module: dict,
                    out_dir: Path, basename: str, stats: dict) -> None:
    """Write lesson description as Markdown and download attached files.

    Skool only populates ``desc``/``resources`` fully for the *selected*
    module, so we navigate to ``?md={moduleId}`` first to fetch full
    metadata.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        ss.navigate_to_module(page, group, course_slug, module["id"])
        detail = ss.fetch_full_module_detail(page, module["id"])
    except Exception as e:
        print(f"   ⚠️  extras navigation failed for {basename}: {e}")
        return
    if not detail:
        return

    # 1) lesson description
    md_text = ss.prosemirror_to_markdown(detail.get("desc") or "")
    if md_text:
        md_path = out_dir / f"{basename}.md"
        if not md_path.exists():
            if args.dry_run:
                print(f"   📝 {basename}.md ({len(md_text)} chars) → {md_path}")
            else:
                md_path.write_text(f"# {detail['title']}\n\n{md_text}\n",
                                   encoding="utf-8")
                stats["md"] += 1
                print(f"   📝 {basename}.md ({len(md_text)} chars)")

    # 2) attached files
    for resource in detail.get("resources") or []:
        file_id = resource.get("file_id")
        if not file_id:
            continue
        file_name = resource.get("file_name", "file")
        title = resource.get("title", file_name)
        suffix = Path(file_name).suffix or ".bin"
        out_path = out_dir / f"{basename} - {ss.sanitize(title)}{suffix}"
        if out_path.exists() and out_path.stat().st_size > 1024:
            stats["file_skip"] += 1
            continue
        if args.dry_run:
            print(f"   📎 {basename} - {title}{suffix} → {out_path}")
            continue

        time.sleep(args.delay)
        signed = ss.fetch_signed_file_url(page, file_id)
        if signed.get("error") or not signed.get("url"):
            print(f"   ⚠️  signed-url failed for {title}: "
                  f"{signed.get('error', 'unknown')}")
            stats["file_fail"] += 1
            continue
        result = ss.download_file_via_browser(page, signed["url"], out_path)
        if result == "ok":
            size_kb = out_path.stat().st_size / 1024
            print(f"   📎 {basename} - {title} ({size_kb:.0f} KB)")
            stats["files"] += 1
        elif result == "skip":
            stats["file_skip"] += 1
        else:
            print(f"   ⚠️  download failed for {title}: {result}")
            stats["file_fail"] += 1


# --------------------------------------------------------------------------- #
# Course-level description (_course.md)
# --------------------------------------------------------------------------- #

def write_course_description(page, course: dict, course_dir: Path,
                             stats: dict, dry_run: bool = False) -> None:
    """Write the course overview to ``_course.md`` if non-empty.

    The classroom page exposes a short course description in
    ``allCourses[].metadata.description``; we capture it once before
    walking modules.
    """
    desc = page.evaluate("""(slug) => {
        const d = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
        const all = d?.props?.pageProps?.renderData?.allCourses || [];
        const c = all.find(x => x.name === slug);
        return c?.metadata?.description || '';
    }""", course["slug"])
    md = ss.prosemirror_to_markdown(desc)
    if not md:
        return
    out = course_dir / "_course.md"
    if out.exists():
        return
    if dry_run:
        print(f"   📝 _course.md ({len(md)} chars) → {out}")
        return
    course_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# {course['title']}\n\n{md}\n", encoding="utf-8")
    stats["md"] += 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download all videos from a Skool classroom",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python skool_download.py URL --profile-dir ./skool-profile --headed
  python skool_download.py URL --profile
  python skool_download.py URL --profile-dir ./skool-profile --server
  python skool_download.py URL --include-extras
  python skool_download.py URL --max-videos 1 --dry-run
""",
    )
    parser.add_argument("url", help="Skool classroom URL "
                        "(e.g. https://www.skool.com/GROUP/classroom)")
    parser.add_argument("--output", "-o", default="downloads",
                        help="Output directory (default: ./downloads)")
    parser.add_argument("--course", "-c",
                        help="Download only courses matching this text (case-insensitive)")
    parser.add_argument("--list", action="store_true",
                        help="List accessible courses and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would download without writing files")

    profile = parser.add_mutually_exclusive_group()
    profile.add_argument("--profile", action="store_true",
                         help="Reuse your system Chrome profile (already logged in)")
    profile.add_argument("--profile-dir",
                         help="Use a managed Playwright profile at this path "
                              "(first run requires login; cookies persist)")

    parser.add_argument("--headed", action="store_true",
                        help="Show the browser window (auto-on with --profile or --profile-dir)")
    parser.add_argument("--server", action="store_true",
                        help="Headless server mode using Playwright's bundled chromium "
                             "(requires --profile-dir with existing cookies)")
    parser.add_argument("--include-extras", action="store_true",
                        help="Also download lesson descriptions (.md) and attached files (.pdf, …)")
    parser.add_argument("--max-videos", type=int, default=0, metavar="N",
                        help="Stop after N successful video downloads (default: unlimited)")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="Delay between API calls in seconds (default: 0.3)")
    args = parser.parse_args()

    if args.server and not args.profile_dir:
        parser.error("--server requires --profile-dir to point to a profile with cookies.")
    return args


def main() -> int:
    args = parse_args()

    match = re.search(r"skool\.com/([^/?#]+)", args.url)
    if not match:
        print("❌ Invalid Skool URL. Expected: https://www.skool.com/GROUP/classroom")
        return 2
    group = match.group(1)
    classroom_url = f"https://www.skool.com/{group}/classroom"
    output_dir = Path(args.output)

    print("🎓 Skool Classroom Downloader")
    print("=" * 50)
    if args.include_extras:
        print("   extras: lesson markdown + attached files enabled")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright not installed. Run: pip install playwright "
              "&& playwright install chromium")
        return 1

    with sync_playwright() as p:
        context, page = launch_browser(p, args)
        try:
            print(f"\n🌐 Loading {classroom_url}")
            ss.open_classroom_page(page, classroom_url)

            if not ss.is_logged_in(page):
                if args.server:
                    print("❌ Not logged in. --server cannot perform an interactive "
                          "login; run once without --server to populate the profile.")
                    return 1
                if not ss.wait_for_login(page, classroom_url):
                    print("❌ Login timed out.")
                    return 1
                # Re-load classroom root so allCourses is populated.
                ss.open_classroom_page(page, classroom_url)

            courses = ss.extract_accessible_courses(page)
            print(f"\n📊 Found {len(courses)} accessible course(s)")

            if args.list:
                for i, c in enumerate(courses, 1):
                    print(f"  {i:2}. {c['title']} ({c['numModules']} lessons)")
                return 0

            if args.course:
                target = args.course.lower()
                courses = [c for c in courses if target in c["title"].lower()]
                if not courses:
                    print(f"❌ No course matches '{args.course}'")
                    return 1

            stats = {"ok": 0, "skip": 0, "fail": 0,
                     "md": 0, "files": 0, "file_skip": 0, "file_fail": 0}

            for course in courses:
                # _course.md is cheap and useful, write it before extras run.
                if args.include_extras:
                    write_course_description(
                        page, course,
                        output_dir / ss.sanitize(course["title"]),
                        stats, dry_run=args.dry_run)
                if not process_course(page, args, group, course,
                                       output_dir, stats):
                    break

            print(f"\n{'=' * 50}")
            print("📊 Final Summary:")
            print(f"   Videos:      {stats['ok']} downloaded, "
                  f"{stats['skip']} skipped, {stats['fail']} failed")
            if args.include_extras:
                print(f"   Markdown:    {stats['md']} written")
                print(f"   Files:       {stats['files']} downloaded, "
                      f"{stats['file_skip']} skipped, {stats['file_fail']} failed")
            print(f"   Output:      {output_dir.resolve()}")
        finally:
            context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
