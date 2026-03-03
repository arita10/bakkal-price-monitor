"""
src/inspector/generator.py — Generate a ready-to-use scraper from InspectionResult.

Selects the correct template based on detected technology, fills in
all detected selectors and pagination logic, then writes the output to
src/parsers/generated_<site_name>.py.
"""

import logging
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("bakkal_monitor.inspector.generator")

# Where generated scrapers are written
_OUTPUT_DIR = Path(__file__).parent.parent / "parsers"

# Template directory
_TPL_DIR = Path(__file__).parent / "templates"

# Map technology → template filename
_TPL_MAP = {
    "angular": "angular.py.tpl",
    "react":   "react.py.tpl",
    "nextjs":  "react.py.tpl",   # Next.js uses same template as React
    "nuxt":    "react.py.tpl",   # Nuxt is Vue/SSR — similar pattern
    "vue":     "react.py.tpl",
    "jquery":  "jquery.py.tpl",
    "static":  "jquery.py.tpl",
}


# ── Pagination code builders ──────────────────────────────────────────────────

def _build_pagination_blocks(pagination, url: str, is_sync: bool = False):
    """
    Returns (pagination_const, loop_start, loop_end) strings
    for insertion into templates.
    """
    ptype = pagination.type
    param = pagination.param_name
    max_p = pagination.max_pages

    if ptype == "url_param":
        const = f"PAGINATION_PARAM = {param!r}\nMAX_PAGES = {max_p}"
        if is_sync:
            loop_start = (
                f"        for page_num in range(1, MAX_PAGES + 1):\n"
                f"            page_url = f\"{{base_url}}?{param}={{page_num}}\" if page_num > 1 else base_url"
            )
            loop_end  = ""
        else:
            loop_start = (
                f"                for page_num in range(1, MAX_PAGES + 1):\n"
                f"                    url = f\"{{base_url}}?{param}={{page_num}}\" if page_num > 1 else base_url\n"
                f"                    if page_num > 1:\n"
                f"                        await page.goto(url, wait_until=\"networkidle\", timeout=30_000)\n"
                f"                        await asyncio.sleep(2)"
            )
            loop_end  = ""
    elif ptype == "next_button":
        sel_escaped = param.replace('"', '\\"')
        const = f"NEXT_BTN_SEL = {param!r}\nMAX_PAGES = {max_p}"
        if is_sync:
            # No easy next-button for sync — just do one page
            loop_start = "        for page_num in range(1, 2):  # next-button: single page\n            page_url = base_url"
            loop_end   = ""
        else:
            loop_start = (
                f"                for page_num in range(1, MAX_PAGES + 1):\n"
                f"                    # Next-button pagination — click to advance"
            )
            loop_end = (
                f"\n                    # Try to click next button\n"
                f"                    try:\n"
                f"                        btn = await page.query_selector(NEXT_BTN_SEL)\n"
                f"                        if not btn:\n"
                f"                            break\n"
                f"                        css = await btn.get_attribute('class') or ''\n"
                f"                        if 'disabled' in css.lower():\n"
                f"                            break\n"
                f"                        await btn.click()\n"
                f"                        await page.wait_for_load_state('networkidle', timeout=15_000)\n"
                f"                        await asyncio.sleep(2)\n"
                f"                    except Exception:\n"
                f"                        break"
            )
    elif ptype == "infinite_scroll":
        const = "MAX_SCROLLS = 20"
        if is_sync:
            loop_start = "        for page_num in range(1, 2):  # infinite-scroll: single page\n            page_url = base_url"
            loop_end   = ""
        else:
            loop_start = (
                f"                for scroll_n in range(1, MAX_SCROLLS + 1):\n"
                f"                    page_num = scroll_n  # alias for break logic below"
            )
            loop_end = (
                f"\n                    # Scroll down and wait for new items\n"
                f"                    prev_count = len(cards)\n"
                f"                    await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')\n"
                f"                    await asyncio.sleep(2.5)\n"
                f"                    new_cards = await page.query_selector_all(CARD_SEL)\n"
                f"                    if len(new_cards) <= prev_count:\n"
                f"                        break  # no new items loaded"
            )
    else:
        # "none" — single page
        const = ""
        if is_sync:
            loop_start = "        for page_num in range(1, 2):  # single page\n            page_url = base_url"
            loop_end   = ""
        else:
            loop_start = (
                "                for page_num in range(1, 2):  # single page, no pagination\n"
                "                    pass"
            )
            loop_end = ""

    return const, loop_start, loop_end


def _build_link_extraction(selectors) -> str:
    """Return the link extraction code block for Playwright templates."""
    if selectors.link_via_parent:
        return (
            "                            href = await card.evaluate(\n"
            "                                \"el => el.closest('a') ? el.closest('a').getAttribute('href') : ''\"\n"
            "                            ) or ''"
        )
    elif selectors.link:
        return (
            "                            link_el = await card.query_selector(LINK_SEL)\n"
            "                            href = (await link_el.get_attribute('href') or '') if link_el else ''"
        )
    else:
        return "                            href = ''"


def _build_cookie_dismiss(cookie_sel: str) -> str:
    """Return cookie-dismiss code block (indented for Playwright templates)."""
    if not cookie_sel:
        return ""
    return (
        f"\n                # Dismiss cookie dialog\n"
        f"                try:\n"
        f"                    btn = await page.query_selector({cookie_sel!r})\n"
        f"                    if btn:\n"
        f"                        await btn.click()\n"
        f"                        await asyncio.sleep(1.5)\n"
        f"                except Exception:\n"
        f"                    pass\n"
    )


def _build_base_url(url: str) -> str:
    """Return scheme+host from URL, e.g. 'https://www.migros.com.tr'."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _build_target_urls(url: str) -> str:
    """Return a Python string literal for TARGET_URLS list content."""
    return f'"{url}"'


# ── Main public function ──────────────────────────────────────────────────────

def generate(result, output_dir: Path | None = None) -> Path:
    """
    Generate a scraper file from an InspectionResult.

    Args:
        result:     InspectionResult from WebInspector.inspect()
        output_dir: Where to write the file (defaults to src/parsers/)

    Returns:
        Path to the generated file.
    """
    if result.error:
        raise ValueError(f"Cannot generate scraper: inspection failed — {result.error}")

    # ── Choose template ───────────────────────────────────────────────────────
    tech = result.technology
    tpl_name = _TPL_MAP.get(tech, "generic.py.tpl")
    tpl_path = _TPL_DIR / tpl_name
    if not tpl_path.exists():
        tpl_path = _TPL_DIR / "generic.py.tpl"

    tpl_text = tpl_path.read_text(encoding="utf-8")

    # ── Build pagination snippets ─────────────────────────────────────────────
    is_sync = tech in ("jquery", "static")
    pag_const, loop_start, loop_end = _build_pagination_blocks(
        result.pagination, result.url, is_sync=is_sync
    )

    # ── Build link extraction ─────────────────────────────────────────────────
    link_block = _build_link_extraction(result.selectors)

    # ── Build cookie dismiss ──────────────────────────────────────────────────
    cookie_block = _build_cookie_dismiss(result.cookie_dismiss_selector)

    # ── Build base URL + relative URL addition ────────────────────────────────
    base_url = _build_base_url(result.url)

    # For link hrefs: add BASE_URL if relative
    # We inject this inline after href is extracted (non-sync templates)
    if not is_sync:
        link_block += (
            "\n                            if href and not href.startswith('http'):\n"
            f"                                href = {base_url!r} + '/' + href.lstrip('/')"
        )

    # ── Fill template ─────────────────────────────────────────────────────────
    selectors = result.selectors
    pagination = result.pagination

    substitutions = {
        "{url}":                 result.url,
        "{site_name}":           result.site_name,
        "{technology}":          tech,
        "{generated_date}":      str(date.today()),
        "{base_url}":            base_url,
        "{target_urls}":         _build_target_urls(result.url),
        "{card_selector}":       selectors.card or "",
        "{name_selector}":       selectors.name or "",
        "{price_selector}":      selectors.price or "",
        "{link_selector}":       selectors.link or "",
        "{link_via_parent}":     str(selectors.link_via_parent),
        "{card_count}":          str(selectors.card_count),
        "{pagination_type}":     pagination.type,
        "{pagination_param}":    pagination.param_name,
        "{pagination_const}":    pag_const,
        "{pagination_loop_start}":      loop_start,
        "{pagination_loop_start_sync}": loop_start,   # used in jquery.py.tpl
        "{pagination_loop_end}":        loop_end,
        "{pagination_loop_end_sync}":   loop_end,
        "{link_extraction_block}":      link_block,
        "{cookie_dismiss_block}":       cookie_block,
    }

    output = tpl_text
    for key, val in substitutions.items():
        output = output.replace(key, val)

    # ── Write output file ─────────────────────────────────────────────────────
    out_dir = output_dir or _OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"generated_{result.site_name}.py"
    out_path.write_text(output, encoding="utf-8")

    logger.info(f"Generated scraper: {out_path}")
    return out_path
