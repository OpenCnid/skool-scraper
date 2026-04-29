#!/usr/bin/env python3
"""
Shared helpers for the Skool downloader.

This module is imported by ``skool_download.py``; it does not run on its own.

Contents:
    * sanitize() / format_lesson_basename()
    * prosemirror_to_markdown() and its node renderers
    * Auth helpers: is_logged_in(), wait_for_login()
    * Page helpers: open_classroom_page(), extract_accessible_courses(),
      extract_course_modules(), navigate_to_module(), fetch_video_token()
    * File downloads: fetch_signed_file_url(), download_file_via_browser()

The ProseMirror converter has doctests; run ``python -m doctest skool_shared.py -v``.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Module-level constants (all timing / polling values live here)
# --------------------------------------------------------------------------- #

LOGIN_POLL_SEC = 3
LOGIN_RELOAD_SEC = 30
LOGIN_TIMEOUT_SEC = 600

PAGE_GOTO_TIMEOUT_MS = 60_000
NEXT_DATA_WAIT_MS = 15_000
HYDRATION_WAIT_SEC = 2

DEFAULT_FILE_EXPIRE_SEC = 28_800  # 8h, matches Skool's UI default

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
            let resources = [];
            try { resources = JSON.parse(meta.resources || '[]') || []; } catch(e) {}
            out.push({
                id: info.id,
                slug: info.name,
                title: meta.title || info.name || '',
                section: sectionPath.length ? sectionPath[sectionPath.length - 1] : '',
                videoId: meta.videoId || '',
                desc: meta.desc || '',
                resources: resources
            });
        }
        const next = ut === 'set' ? [...sectionPath, meta.title || info.name || ''] : sectionPath;
        for (const child of node.children || []) walk(child, next);
    }
    walk(root, []);
    return out;
}
"""

_EXTRACT_FULL_MODULE_DETAIL_JS = r"""
(targetId) => {
    const d = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
    const root = d?.props?.pageProps?.renderData?.course;
    if (!root) return null;
    let found = null;
    function walk(node) {
        const info = node.course || {};
        if (info.id === targetId) { found = info; return; }
        for (const ch of (node.children || [])) walk(ch);
    }
    walk(root);
    if (!found) return null;
    const meta = found.metadata || {};
    let resources = [];
    try { resources = JSON.parse(meta.resources || '[]') || []; } catch(e) {}
    return {
        id: found.id,
        slug: found.name,
        title: meta.title || found.name || '',
        videoId: meta.videoId || '',
        desc: meta.desc || '',
        resources: resources
    };
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

_FETCH_SIGNED_FILE_URL_JS = r"""
async ({fileId, expire}) => {
    const url = `https://api2.skool.com/files/${fileId}/download-url?expire=${expire}`;
    try {
        const r = await fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers: {'Accept': 'text/plain, application/json, */*'}
        });
        const txt = await r.text();
        if (!r.ok) return {error: `http_${r.status}: ${txt.slice(0, 200)}`};
        return {url: txt.trim()};
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
# ProseMirror "v2" → Markdown converter
# --------------------------------------------------------------------------- #

def prosemirror_to_markdown(raw: str) -> str:
    """Convert Skool's ProseMirror "v2" JSON to plain Markdown.

    Skool stores lesson and course descriptions as ``[v2]`` followed by a JSON
    array of nodes. Empty/whitespace-only descriptions return an empty string
    so callers can decide whether to write a file.

    Supported nodes: paragraph, heading, bulletList/unorderedList, orderedList,
    listItem, blockquote, image, horizontalRule, hardBreak. Marks: bold,
    italic, code, strike, link.

    >>> prosemirror_to_markdown('')
    ''
    >>> prosemirror_to_markdown('[v2][]')
    ''
    >>> prosemirror_to_markdown('[v2][{"type":"paragraph","content":[{"type":"text","text":"Hello"}]}]')
    'Hello'
    >>> prosemirror_to_markdown('[v2][{"type":"heading","attrs":{"level":2},"content":[{"type":"text","text":"Title"}]}]')
    '## Title'
    >>> prosemirror_to_markdown('[v2][{"type":"paragraph","content":[{"type":"text","marks":[{"type":"bold"}],"text":"hi"}]}]')
    '**hi**'
    >>> prosemirror_to_markdown('not-json')
    'not-json'
    """
    if not raw:
        return ""
    s = raw.strip()
    if s.startswith("[v2]"):
        s = s[4:]
    try:
        nodes = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return raw
    if not nodes:
        return ""
    if isinstance(nodes, list) and not any(_node_has_visible_content(n) for n in nodes):
        return ""
    return _render_nodes(nodes).strip()


def _node_has_visible_content(node: Any) -> bool:
    """Return True if the node tree contains anything renderable."""
    if not isinstance(node, dict):
        return False
    if node.get("type") in ("text", "image", "horizontalRule", "hardBreak"):
        return True
    return any(_node_has_visible_content(c) for c in node.get("content", []))


def _render_marks(text: str, marks: list) -> str:
    """Wrap ``text`` with each mark in order. Marks are applied innermost-first."""
    if not marks:
        return text
    out = text
    for mk in marks:
        t = mk.get("type")
        if t == "bold":
            out = f"**{out}**"
        elif t == "italic":
            out = f"*{out}*"
        elif t == "code":
            out = f"`{out}`"
        elif t == "strike":
            out = f"~~{out}~~"
        elif t == "link":
            href = (mk.get("attrs") or {}).get("href", "")
            out = f"[{out}]({href})" if href else out
    return out


def _render_inline(content: Optional[list]) -> str:
    """Render a list of inline nodes (text / hardBreak / image)."""
    parts = []
    for n in content or []:
        t = n.get("type")
        if t == "text":
            parts.append(_render_marks(n.get("text", ""), n.get("marks") or []))
        elif t == "hardBreak":
            parts.append("  \n")
        elif t == "image":
            attrs = n.get("attrs") or {}
            alt = attrs.get("alt", "")
            src = attrs.get("originalSrc") or attrs.get("src", "")
            parts.append(f"![{alt}]({src})")
    return "".join(parts)


def _render_nodes(nodes: Optional[list], list_depth: int = 0,
                  list_marker: Optional[str] = None) -> str:
    """Render a list of block nodes to Markdown, joined by blank lines."""
    out = []
    for n in nodes or []:
        t = n.get("type")
        if t == "paragraph":
            line = _render_inline(n.get("content"))
            if line.strip():
                out.append(line)
        elif t == "heading":
            level = (n.get("attrs") or {}).get("level", 1)
            line = _render_inline(n.get("content"))
            out.append(f"{'#' * level} {line}")
        elif t in ("bulletList", "unorderedList"):
            items = []
            for item in n.get("content") or []:
                items.append(_render_nodes([item], list_depth + 1, "*"))
            out.append("\n".join(items))
        elif t == "orderedList":
            items = []
            for i, item in enumerate(n.get("content") or [], 1):
                items.append(_render_nodes([item], list_depth + 1, f"{i}."))
            out.append("\n".join(items))
        elif t == "listItem":
            indent = "  " * (list_depth - 1) if list_depth > 0 else ""
            inner = _render_nodes(n.get("content"), list_depth, list_marker)
            marker = list_marker or "*"
            lines = inner.splitlines()
            if not lines:
                out.append(f"{indent}{marker} ")
            else:
                first = f"{indent}{marker} {lines[0]}"
                rest = [f"{indent}  {ln}" for ln in lines[1:]]
                out.append("\n".join([first, *rest]))
        elif t == "blockquote":
            inner = _render_nodes(n.get("content"))
            out.append("\n".join("> " + ln for ln in inner.splitlines()))
        elif t == "image":
            attrs = n.get("attrs") or {}
            alt = attrs.get("alt", "")
            src = attrs.get("originalSrc") or attrs.get("src", "")
            out.append(f"![{alt}]({src})")
        elif t == "horizontalRule":
            out.append("---")
        elif t == "hardBreak":
            out.append("")
        else:
            inner = _render_nodes(n.get("content"))
            if inner:
                out.append(inner)
    return "\n\n".join(o for o in out if o)


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
    ``title``, ``section``, ``videoId``, ``desc``, ``resources``. Note:
    Skool only fully populates ``desc`` and ``resources`` for the *selected*
    module; other modules' fields may be truncated. Use
    :func:`fetch_full_module_detail` per-module when you need the full
    description (e.g. for the extras export path).
    """
    return page.evaluate(_EXTRACT_COURSE_MODULES_JS)


def navigate_to_module(page, group: str, course_slug: str, module_id: str) -> None:
    """Open the module-specific URL so its full metadata loads."""
    url = f"https://www.skool.com/{group}/classroom/{course_slug}?md={module_id}"
    open_classroom_page(page, url)
    time.sleep(HYDRATION_WAIT_SEC)


def fetch_full_module_detail(page, module_id: str) -> Optional[dict]:
    """Return the full ``{id, slug, title, videoId, desc, resources}`` for
    the module identified by ``module_id`` on the currently loaded page."""
    return page.evaluate(_EXTRACT_FULL_MODULE_DETAIL_JS, module_id)


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


# --------------------------------------------------------------------------- #
# Resource (PDF / file) downloads
# --------------------------------------------------------------------------- #

def fetch_signed_file_url(page, file_id: str,
                          expire_sec: int = DEFAULT_FILE_EXPIRE_SEC) -> dict:
    """POST to Skool's file API and return ``{url}`` or ``{error}``.

    Skool only accepts POST on this endpoint; GET returns 405. The response
    body is a CloudFront signed URL as ``text/plain`` (not JSON).
    """
    return page.evaluate(_FETCH_SIGNED_FILE_URL_JS, {
        "fileId": file_id,
        "expire": expire_sec,
    })


def download_file_via_browser(page, signed_url: str, output_path: Path) -> str:
    """Download a signed URL using the browser's request context.

    Returns ``"ok"``, ``"skip"`` (already exists), or an error string. We use
    ``page.request.get`` so the browser session and cookies are reused; the
    target is a CloudFront URL and works without cookies, but going through
    the browser keeps the proxy/network stack consistent.
    """
    if output_path.exists() and output_path.stat().st_size > 1024:
        return "skip"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = page.request.get(signed_url, timeout=120_000)
    except Exception as e:
        return f"request_error: {e}"
    if not resp.ok:
        return f"http_{resp.status}"
    body = resp.body()
    output_path.write_bytes(body)
    if output_path.stat().st_size < 1024:
        output_path.unlink(missing_ok=True)
        return "downloaded_too_small"
    return "ok"


if __name__ == "__main__":
    import doctest
    failed, total = doctest.testmod(verbose=False)
    print(f"doctests: {total - failed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)
