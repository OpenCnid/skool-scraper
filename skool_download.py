#!/usr/bin/env python3
"""
Skool Video Downloader
======================
Downloads all course videos from a Skool classroom using Playwright + ffmpeg.

Setup:
    pip install playwright
    playwright install chromium
    brew install ffmpeg   # or: apt install ffmpeg

Usage:
    # Download all courses (opens browser for login)
    python skool_download.py https://www.skool.com/ai-profit-lab-7462/classroom

    # Download a specific course
    python skool_download.py https://www.skool.com/ai-profit-lab-7462/classroom --course "6-Week"

    # List courses only
    python skool_download.py https://www.skool.com/ai-profit-lab-7462/classroom --list

    # Use existing Chrome profile (no login needed)
    python skool_download.py https://www.skool.com/ai-profit-lab-7462/classroom --profile

    # Dry run
    python skool_download.py https://www.skool.com/ai-profit-lab-7462/classroom --dry-run

Architecture:
    1. Playwright opens Skool classroom (with your logged-in session)
    2. Extracts course structure from __NEXT_DATA__ (Next.js SSR)
    3. For each lesson, fetches the Mux video token via Next.js data routes
    4. ffmpeg downloads the HLS stream (Mux CDN doesn't restrict IPs)
    5. Videos saved as: downloads/Course Name/Section/001 - Lesson.mp4
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def sanitize(name: str) -> str:
    """Clean filename."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    return name.strip('. ')[:200]


def download_video(playback_id: str, token: str, output_path: Path) -> str:
    """Download a Mux HLS video via ffmpeg. Returns 'ok', 'skip', or error string."""
    if output_path.exists() and output_path.stat().st_size > 10240:
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
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1024:
            return "ok"
        else:
            if output_path.exists():
                output_path.unlink()
            return f"ffmpeg error: {result.stderr[-300:]}"
    except subprocess.TimeoutExpired:
        if output_path.exists():
            output_path.unlink()
        return "timeout"
    except FileNotFoundError:
        print("\n❌ ffmpeg not found!")
        print("   macOS:  brew install ffmpeg")
        print("   Linux:  sudo apt install ffmpeg")
        print("   Windows: winget install ffmpeg")
        sys.exit(1)


def extract_courses(page) -> list:
    """Extract all accessible courses from the classroom page."""
    return page.evaluate("""() => {
        const data = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
        const courses = data.props.pageProps.renderData.allCourses;
        return courses
            .filter(c => c.metadata?.hasAccess === 1)
            .map(c => ({
                id: c.id,
                name: c.name,
                title: c.metadata.title,
                numModules: c.metadata.numModules
            }));
    }""")


def extract_course_lessons(page, course_name: str) -> list:
    """Extract all lessons with video IDs from a course page."""
    return page.evaluate("""(courseName) => {
        const data = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
        const rd = data.props.pageProps.renderData;
        const courseTree = rd.course;

        function walkTree(node, path) {
            const results = [];
            const info = node.course || {};
            const meta = info.metadata || {};
            const unitType = info.unitType || '';
            const title = meta.title || info.name || '';

            if (unitType === 'module' && meta.videoId) {
                results.push({
                    title, videoId: meta.videoId,
                    moduleId: info.id, moduleName: info.name,
                    section: path.length > 0 ? path[path.length - 1] : ''
                });
            }

            const children = node.children || [];
            const newPath = unitType === 'set' ? [...path, title] : path;
            for (const child of children) results.push(...walkTree(child, newPath));
            return results;
        }

        return walkTree(courseTree, []);
    }""", course_name)


def fetch_video_token(page, course_name: str, module_id: str, group: str):
    """Fetch a video token via the Next.js data route."""
    result = page.evaluate("""async ({courseName, moduleId, group}) => {
        const data = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
        const buildId = data.buildId;
        const url = `/_next/data/${buildId}/${group}/classroom/${courseName}.json?md=${moduleId}&group=${group}`;

        try {
            const resp = await fetch(url, {credentials: 'include'});
            const jdata = await resp.json();
            const video = jdata.pageProps?.renderData?.video;
            if (video) {
                return {
                    playback_id: video.playbackId,
                    token: video.playbackToken,
                    expire: video.expire,
                    duration: video.duration
                };
            }
        } catch(e) {}
        return null;
    }""", {"courseName": course_name, "moduleId": module_id, "group": group})
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Download all videos from a Skool classroom",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python skool_download.py https://www.skool.com/my-group/classroom
  python skool_download.py https://www.skool.com/my-group/classroom --course "Week 1"
  python skool_download.py https://www.skool.com/my-group/classroom --list
  python skool_download.py https://www.skool.com/my-group/classroom --profile
        """
    )
    parser.add_argument("url", help="Skool classroom URL")
    parser.add_argument("--output", "-o", default="downloads", help="Output directory (default: downloads)")
    parser.add_argument("--course", "-c", help="Download only courses matching this text")
    parser.add_argument("--list", action="store_true", help="List courses and exit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would download")
    parser.add_argument("--profile", action="store_true",
                       help="Use existing Chrome user profile (skips login)")
    parser.add_argument("--headed", action="store_true",
                       help="Show browser window (default if not --profile)")
    parser.add_argument("--delay", type=float, default=0.3,
                       help="Delay between API requests in seconds (default: 0.3)")

    args = parser.parse_args()
    output_dir = Path(args.output)

    # Extract group slug from URL
    import re as re_mod
    match = re_mod.search(r'skool\.com/([^/]+)', args.url)
    if not match:
        print("❌ Invalid Skool URL. Expected: https://www.skool.com/GROUP/classroom")
        sys.exit(1)
    group = match.group(1)
    classroom_url = f"https://www.skool.com/{group}/classroom"

    print("🎓 Skool Video Downloader")
    print("=" * 50)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright not installed!")
        print("   pip install playwright")
        print("   playwright install chromium")
        sys.exit(1)

    with sync_playwright() as p:
        # Launch browser
        if args.profile:
            # Use existing Chrome profile
            import pathlib
            if sys.platform == "darwin":
                chrome_path = pathlib.Path.home() / "Library/Application Support/Google/Chrome"
            elif sys.platform == "win32":
                chrome_path = pathlib.Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data"
            else:
                chrome_path = pathlib.Path.home() / ".config/google-chrome"

            context = p.chromium.launch_persistent_context(
                str(chrome_path),
                headless=False,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.new_page()
        else:
            browser = p.chromium.launch(headless=not args.headed)
            context = browser.new_context()
            page = context.new_page()

        # Navigate to classroom
        print(f"\n🌐 Loading {classroom_url}")
        page.goto(classroom_url, wait_until="networkidle")

        # Check if we need to login
        if "/login" in page.url or page.query_selector('input[type="email"]'):
            print("\n🔐 Login required! Please log in to Skool in the browser window...")
            if not args.headed and not args.profile:
                print("   TIP: Run with --headed or --profile to see the browser")
                sys.exit(1)
            # Wait for user to complete login and reach classroom
            page.wait_for_url(f"**/classroom**", timeout=300000)
            print("   ✅ Login successful!")

        # Wait for page data
        page.wait_for_selector("#__NEXT_DATA__", timeout=10000)

        # Get courses
        courses = extract_courses(page)
        print(f"\n📊 Found {len(courses)} accessible courses")

        if args.list:
            for i, c in enumerate(courses, 1):
                print(f"  {i:2}. {c['title']} ({c['numModules']} lessons)")
            return

        # Filter
        if args.course:
            target = args.course.lower()
            courses = [c for c in courses if target in c['title'].lower()]
            if not courses:
                print(f"❌ No course matching '{args.course}'")
                return

        # Process each course
        total_stats = {"ok": 0, "skip": 0, "fail": 0}

        for ci, course in enumerate(courses, 1):
            print(f"\n{'='*50}")
            print(f"📖 [{ci}/{len(courses)}] {course['title']}")

            # Navigate to the first lesson of this course to load its data
            first_lesson_url = f"https://www.skool.com/{group}/classroom/{course['name']}"
            page.goto(first_lesson_url, wait_until="networkidle")
            page.wait_for_selector("#__NEXT_DATA__", timeout=10000)

            # Check if we got redirected to a specific module
            current_url = page.url
            if "md=" not in current_url:
                # Try with the course tree
                time.sleep(1)

            # Extract lessons
            try:
                lessons = extract_course_lessons(page, course['name'])
            except Exception as e:
                print(f"   ❌ Failed to extract lessons: {e}")
                continue

            video_lessons = [l for l in lessons if l.get('videoId')]
            if not video_lessons:
                print(f"   ⏭️  No videos found")
                continue

            print(f"   {len(video_lessons)} videos found")
            course_dir = output_dir / sanitize(course['title'])

            for li, lesson in enumerate(video_lessons, 1):
                section = sanitize(lesson.get('section', ''))
                out_dir = course_dir / section if section else course_dir
                filename = f"{li:03d} - {sanitize(lesson['title'])}.mp4"
                output_path = out_dir / filename

                # Check if already downloaded
                if output_path.exists() and output_path.stat().st_size > 10240:
                    size_mb = output_path.stat().st_size / 1024 / 1024
                    print(f"   [{li}/{len(video_lessons)}] ⏭️  {lesson['title']} ({size_mb:.1f} MB)")
                    total_stats["skip"] += 1
                    continue

                # Fetch video token
                time.sleep(args.delay)
                video = fetch_video_token(page, course['name'], lesson['moduleId'], group)

                if not video or not video.get('playback_id'):
                    print(f"   [{li}/{len(video_lessons)}] ❌ No token: {lesson['title']}")
                    total_stats["fail"] += 1
                    continue

                duration_min = video.get('duration', 0) / 1000 / 60
                print(f"   [{li}/{len(video_lessons)}] ⬇️  {lesson['title']} ({duration_min:.1f} min)")

                if args.dry_run:
                    print(f"      → {output_path}")
                    continue

                result = download_video(video['playback_id'], video['token'], output_path)

                if result == "ok":
                    size_mb = output_path.stat().st_size / 1024 / 1024
                    print(f"      ✅ {size_mb:.1f} MB")
                    total_stats["ok"] += 1
                elif result == "skip":
                    total_stats["skip"] += 1
                else:
                    print(f"      ❌ {result}")
                    total_stats["fail"] += 1

        # Summary
        print(f"\n{'='*50}")
        print(f"📊 Final Summary:")
        print(f"   Downloaded: {total_stats['ok']}")
        print(f"   Skipped:    {total_stats['skip']}")
        print(f"   Failed:     {total_stats['fail']}")
        print(f"   Output:     {output_dir.resolve()}")

        if not args.profile:
            context.close()


if __name__ == "__main__":
    main()
