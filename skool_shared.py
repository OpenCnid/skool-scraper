#!/usr/bin/env python3
"""
Shared helpers for the Skool downloader.

This module is imported by ``skool_download.py``; it does not run on its own.

Contents:
    * sanitize() / format_lesson_basename()
    * Auth helpers: is_logged_in(), wait_for_login()
    * Page helpers: open_classroom_page(), extract_accessible_courses(),
      extract_course_modules(), fetch_video_token()
"""

from __future__ import annotations

import re
import time

# --------------------------------------------------------------------------- #
# Module-level constants (all timing / polling values live here)
# --------------------------------------------------------------------------- #

LOGIN_POLL_SEC = 3
LOGIN_RELOAD_SEC = 30
LOGIN_TIMEOUT_SEC = 600

PAGE_GOTO_TIMEOUT_MS = 60_000
NEXT_DATA_WAIT_MS = 15_000

# Logged-in users carry self.user.id; logged-out pages do not.
# allCourses is populated on the /classroom root URL only.
_AUTH_CHECK_JS = r"""
() => {
    try {
        const el = document.getElementById('__NEXT_DATA__');
        if (!el) return false;
        const d = JSON.parse(el.textContent);
        const pp = d?.props?.pageProps || {};
        const rd = pp.renderData || {};
        const hasUser = !!(pp.self?.user?.id || rd.currentUser?.id);
        const hasCourses = Array.isArray(rd.allCourses);
        return hasUser || hasCourses;
    } catch (e) { return false; }
}
"""

_EXTRACT_ACCESSIBLE_COURSES_JS = r"""
() => {
    const d = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
    const all = d?.props?.pageProps?.renderData?.allCourses || [];
    return all
        .filter(c => c.metadata?.hasAccess === 1)
        .map(c => ({
            id: c.id,
            slug: c.name,
            title: c.metadata?.title || c.name,
            numModules: c.metadata?.numModules || 0
        }));
}
"""

_EXTRACT_COURSE_MODULES_JS = r"""
() => {
    const d = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
    const root = d?.props?.pageProps?.renderData?.course;
    if (!root) return [];
    const out = [];
    function walk(node, sectionPath) {
        const info = node.course || {};
        const meta = info.metadata || {};
        const ut = info.unitType || '';
        if (ut === 'module') {
            out.push({
                id: info.id,
                slug: info.name,
                title: meta.title || info.name || '',
                section: sectionPath.length ? sectionPath[sectionPath.length - 1] : '',
                videoId: meta.videoId || ''
            });
        }
        const next = ut === 'set' ? [...sectionPath, meta.title || info.name || ''] : sectionPath;
        for (const child of node.children || []) walk(child, next);
    }
    walk(root, []);
    return out;
}
"""

_FETCH_VIDEO_TOKEN_JS = r"""
async ({courseSlug, moduleId, group}) => {
    const data = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
    const buildId = data.buildId;
    const url = `/_next/data/${buildId}/${group}/classroom/${courseSlug}.json?md=${moduleId}&group=${group}`;
    try {
        const resp = await fetch(url, {credentials: 'include'});
        if (!resp.ok) return {error: `http_${resp.status}`};
        const jdata = await resp.json();
        const video = jdata.pageProps?.renderData?.video;
        if (!video) return {error: 'no_video_in_response'};
        return {
            playbackId: video.playbackId,
            token: video.playbackToken,
            expire: video.expire,
            duration: video.duration
        };
    } catch (e) {
        return {error: e.toString()};
    }
}
"""


# --------------------------------------------------------------------------- #
# Filename helpers
# --------------------------------------------------------------------------- #

def sanitize(name: str) -> str:
    """Strip filesystem-unsafe characters and trim length.

    >>> sanitize("Lesson 1: What's New?")
    "Lesson 1_ What's New_"
    >>> sanitize("  trailing  ")
    'trailing'
    >>> sanitize("a/b\\\\c")
    'a_b_c'
    """
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', '_', name)
    return name.strip('. ')[:200]


def format_lesson_basename(position: int, title: str) -> str:
    """Render the ``NNN - Title`` prefix used for lesson files.

    >>> format_lesson_basename(1, 'Intro')
    '001 - Intro'
    >>> format_lesson_basename(42, 'Q & A!')
    '042 - Q & A!'
    """
    return f"{position:03d} - {sanitize(title)}"


# --------------------------------------------------------------------------- #
# Page navigation + auth (Playwright Page object passed in by caller)
# --------------------------------------------------------------------------- #

def open_classroom_page(page, classroom_url: str) -> None:
    """Navigate to ``classroom_url`` and wait for ``__NEXT_DATA__`` to attach.

    Uses ``domcontentloaded`` because Skool keeps long-lived sockets/analytics
    open, so ``networkidle`` regularly times out. Uses ``state="attached"``
    because ``<script id="__NEXT_DATA__">`` is never visible.
    """
    page.goto(classroom_url, wait_until="domcontentloaded",
              timeout=PAGE_GOTO_TIMEOUT_MS)
    page.wait_for_selector("#__NEXT_DATA__", state="attached",
                           timeout=NEXT_DATA_WAIT_MS)


def is_logged_in(page) -> bool:
    """Return True if the current page shows an authenticated Skool session.

    Skool serves classroom HTML to logged-out users too, so the URL is not a
    reliable gate. We check ``self.user.id`` (always present when logged in)
    and ``renderData.allCourses`` (present on the classroom root URL).
    """
    return bool(page.evaluate(_AUTH_CHECK_JS))


def wait_for_login(page, classroom_url: str,
                   timeout_sec: int = LOGIN_TIMEOUT_SEC) -> bool:
    """Block until the user logs in or ``timeout_sec`` elapses.

    Polls ``is_logged_in`` every ``LOGIN_POLL_SEC`` seconds and reloads the
    classroom URL every ``LOGIN_RELOAD_SEC`` seconds so cookies set during
    login are picked up.
    """
    print(f"\n🔐 Login required. Complete login in the browser; "
          f"waiting up to {timeout_sec}s.")
    deadline = time.monotonic() + timeout_sec
    last_reload = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(LOGIN_POLL_SEC)
        try:
            if is_logged_in(page):
                print("   ✅ Login detected.")
                return True
        except Exception:
            pass  # transient page/eval errors during navigation are fine
        if time.monotonic() - last_reload >= LOGIN_RELOAD_SEC:
            last_reload = time.monotonic()
            remaining = int(deadline - time.monotonic())
            print(f"   ... still waiting (~{remaining}s left); reloading classroom.")
            try:
                open_classroom_page(page, classroom_url)
            except Exception as e:
                print(f"   (reload failed, continuing: {e})")
    return False


def extract_accessible_courses(page) -> list[dict]:
    """Return a list of ``{id, slug, title, numModules}`` for accessible courses."""
    return page.evaluate(_EXTRACT_ACCESSIBLE_COURSES_JS)


def extract_course_modules(page) -> list[dict]:
    """Return all module nodes for the course currently loaded on ``page``.

    The result preserves Skool UI order. Each item has ``id``, ``slug``,
    ``title``, ``section``, ``videoId``.
    """
    return page.evaluate(_EXTRACT_COURSE_MODULES_JS)


def fetch_video_token(page, course_slug: str, module_id: str, group: str) -> dict:
    """Fetch a fresh Mux playback token via Skool's Next.js data route.

    Returns ``{playbackId, token, expire, duration}`` on success or
    ``{error: ...}`` on failure.
    """
    return page.evaluate(_FETCH_VIDEO_TOKEN_JS, {
        "courseSlug": course_slug,
        "moduleId": module_id,
        "group": group,
    })


if __name__ == "__main__":
    import doctest
    failed, total = doctest.testmod(verbose=False)
    print(f"doctests: {total - failed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)
