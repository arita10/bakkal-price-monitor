"""
src/inspector/detector.py — Detect which JavaScript framework a page uses.

Detection order (most reliable first):
  1. JavaScript globals  (window.ng, window.__NEXT_DATA__, etc.)
  2. HTML patterns       (id="__next", ng-version attribute, etc.)
  3. Script filenames    (react.production.min.js, jquery.min.js, etc.)
"""

import logging
import re

logger = logging.getLogger("bakkal_monitor.inspector.detector")

# ── JS globals to check ───────────────────────────────────────────────────────
_JS_CHECKS = [
    ("angular",  "!!window.ng"),
    ("nextjs",   "!!window.__NEXT_DATA__"),
    ("react",    "!!(window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || window.React)"),
    ("nuxt",     "!!window.__NUXT__"),
    ("vue",      "!!(window.__vue_app__ || window.Vue)"),
    ("jquery",   "!!window.jQuery"),
]

# ── HTML attribute/id patterns ────────────────────────────────────────────────
_HTML_PATTERNS = [
    ("angular", r'ng-version=|<app-root|<sm-list-page|angular\.js'),
    ("nextjs",  r'id="__next"'),
    ("react",   r'id="root"'),
    ("nuxt",    r'id="__nuxt"'),
    ("vue",     r'id="app"[^>]*data-v-'),
    ("jquery",  r'jquery\.min\.js|jquery-\d'),
]

# ── Script src patterns ───────────────────────────────────────────────────────
_SCRIPT_PATTERNS = [
    ("angular", r'angular|main\.chunk\.js|runtime\.js.*angular'),
    ("nextjs",  r'_next/static'),
    ("react",   r'react\.production\.min|react-dom\.production'),
    ("vue",     r'vue\.min\.js|vue\.runtime'),
    ("jquery",  r'jquery\.min\.js|jquery-\d+\.\d+'),
]


async def detect(page) -> str:
    """
    Detect the JavaScript framework used by the current page.
    Returns one of: 'angular' | 'nextjs' | 'react' | 'nuxt' | 'vue' | 'jquery' | 'static'
    """
    # ── Step 1: JS globals (most reliable) ───────────────────────────────────
    for tech, js_expr in _JS_CHECKS:
        try:
            result = await page.evaluate(js_expr)
            if result:
                logger.debug(f"Detected via JS global: {tech}")
                return tech
        except Exception:
            pass

    # ── Step 2: HTML patterns ─────────────────────────────────────────────────
    try:
        html = await page.content()
        for tech, pattern in _HTML_PATTERNS:
            if re.search(pattern, html, re.IGNORECASE):
                logger.debug(f"Detected via HTML pattern: {tech}")
                return tech
    except Exception:
        pass

    # ── Step 3: Script src filenames ──────────────────────────────────────────
    try:
        scripts = await page.evaluate(
            "() => Array.from(document.scripts).map(s => s.src).filter(Boolean)"
        )
        all_scripts = " ".join(scripts)
        for tech, pattern in _SCRIPT_PATTERNS:
            if re.search(pattern, all_scripts, re.IGNORECASE):
                logger.debug(f"Detected via script src: {tech}")
                return tech
    except Exception:
        pass

    logger.debug("No framework detected — treating as static HTML")
    return "static"


async def needs_wait(technology: str, page) -> tuple[bool, str]:
    """
    Returns (should_wait: bool, wait_selector: str).
    Angular and some React sites need explicit render waiting.
    """
    if technology == "angular":
        # Try common Angular component tag names
        for sel in [
            "app-root > *",
            "sm-list-page-item",
            "[class*='product-card']",
            "[class*='ProductCard']",
        ]:
            try:
                count = await page.evaluate(
                    f"() => document.querySelectorAll('{sel}').length"
                )
                if count and count > 0:
                    return True, sel
            except Exception:
                pass
        return True, "app-root > *"

    if technology in ("nextjs", "react"):
        # React/Next.js usually loads quickly but may need a short sleep
        return True, ""

    return False, ""
