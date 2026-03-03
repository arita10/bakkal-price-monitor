"""
tools/inspect_site.py — CLI for the Universal Web Inspector.

Usage examples:
    # Inspect a single URL and print results
    python tools/inspect_site.py --url https://www.migros.com.tr/meyve-sebze-c-2

    # Inspect and generate a scraper file
    python tools/inspect_site.py --url https://www.migros.com.tr/meyve-sebze-c-2 --generate

    # Inspect multiple URLs from a file
    python tools/inspect_site.py --urls urls.txt

    # Inspect multiple URLs from a file and generate scrapers for each
    python tools/inspect_site.py --urls urls.txt --generate

    # Increase concurrency for multi-URL mode
    python tools/inspect_site.py --urls urls.txt --generate --concurrency 5

    # Save JSON report
    python tools/inspect_site.py --url https://... --output report.json
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.inspector.inspector import WebInspector, InspectionResult
from src.inspector import generator


# ── Logging setup ──────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence noisy third-party loggers unless verbose
    if not verbose:
        for name in ("playwright", "asyncio", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)


# ── Pretty printer ────────────────────────────────────────────────────────────

def _print_result(r: InspectionResult) -> None:
    OK   = "\033[32m✓\033[0m"
    WARN = "\033[33m⚠\033[0m"
    ERR  = "\033[31m✗\033[0m"

    print(f"\n{'─' * 60}")
    print(f"  URL:   {r.url}")
    print(f"  Site:  {r.site_name}")

    if r.error:
        print(f"  {ERR}  Inspection FAILED: {r.error}")
        return

    print(f"  Tech:  {r.technology}  "
          + (f"(needs_wait={r.wait_selector!r})" if r.needs_wait else ""))
    print(f"  Cookie dialog: {'yes → ' + r.cookie_dismiss_selector if r.has_cookie_dialog else 'no'}")
    print()

    sel = r.selectors
    def _mark(v):
        return OK if v else WARN

    print(f"  {_mark(sel.card)}  Card:   {sel.card!r}  ({sel.card_count} elements)")
    print(f"  {_mark(sel.name)}  Name:   {sel.name!r}")
    print(f"  {_mark(sel.price)}  Price:  {sel.price!r}")
    print(f"  {_mark(sel.link)}  Link:   {sel.link!r}"
          + (" (via parent <a>)" if sel.link_via_parent else ""))

    pag = r.pagination
    print(f"\n  Pagination: {pag.type}"
          + (f" (?{pag.param_name}=N, max={pag.max_pages})" if pag.param_name else f" (max={pag.max_pages})"))

    if r.sample_products:
        print(f"\n  Sample products ({len(r.sample_products)}):")
        for i, prod in enumerate(r.sample_products, 1):
            name  = prod.get("name",  "—")[:50]
            price = prod.get("price", "—")
            href  = prod.get("href",  "")[:60]
            print(f"    {i}. {name}")
            print(f"       price={price}  href={href or '—'}")
    else:
        print(f"\n  {WARN}  No sample products extracted")

    print()


def _result_to_dict(r: InspectionResult) -> dict:
    """Convert InspectionResult to a JSON-serializable dict."""
    return {
        "url":                    r.url,
        "site_name":              r.site_name,
        "technology":             r.technology,
        "needs_wait":             r.needs_wait,
        "wait_selector":          r.wait_selector,
        "has_cookie_dialog":      r.has_cookie_dialog,
        "cookie_dismiss_selector": r.cookie_dismiss_selector,
        "selectors": {
            "card":            r.selectors.card,
            "name":            r.selectors.name,
            "price":           r.selectors.price,
            "link":            r.selectors.link,
            "link_via_parent": r.selectors.link_via_parent,
            "card_count":      r.selectors.card_count,
        },
        "pagination": {
            "type":       r.pagination.type,
            "param_name": r.pagination.param_name,
            "max_pages":  r.pagination.max_pages,
        },
        "sample_products": r.sample_products,
        "error":           r.error,
    }


# ── Main logic ────────────────────────────────────────────────────────────────

async def _run(args) -> int:
    """Core async runner. Returns exit code."""
    # Collect URLs
    urls: list[str] = []
    if args.url:
        urls = [args.url]
    elif args.urls:
        path = Path(args.urls)
        if not path.exists():
            print(f"Error: file not found: {args.urls}", file=sys.stderr)
            return 1
        lines = path.read_text(encoding="utf-8").splitlines()
        urls = [l.strip() for l in lines if l.strip() and not l.startswith("#")]

    if not urls:
        print("Error: no URLs provided.", file=sys.stderr)
        return 1

    # Inspect
    if len(urls) == 1:
        results = [await WebInspector.inspect(urls[0])]
    else:
        print(f"Inspecting {len(urls)} URLs (concurrency={args.concurrency})...")
        results = await WebInspector.inspect_many(urls, concurrency=args.concurrency)

    # Print results
    for r in results:
        _print_result(r)

    # Generate scrapers
    generated_paths = []
    if args.generate:
        print("\nGenerating scrapers...")
        for r in results:
            if r.error:
                print(f"  Skipping {r.site_name} (inspection failed)")
                continue
            if not r.selectors.card:
                print(f"  Skipping {r.site_name} (no card selector found)")
                continue
            try:
                out_path = generator.generate(r)
                generated_paths.append(out_path)
                print(f"  Generated: {out_path}")
            except Exception as exc:
                print(f"  Error generating {r.site_name}: {exc}", file=sys.stderr)

    # Save JSON report
    if args.output:
        report = [_result_to_dict(r) for r in results]
        out = Path(args.output)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nReport saved: {out}")

    # Summary
    ok    = sum(1 for r in results if not r.error)
    fail  = len(results) - ok
    print(f"\n{'─' * 60}")
    print(f"Done: {ok} inspected OK, {fail} failed"
          + (f", {len(generated_paths)} scrapers generated" if args.generate else ""))

    if generated_paths:
        print("\nTo test a generated scraper:")
        p = generated_paths[0]
        module = str(p).replace("\\", "/").replace("/", ".").removesuffix(".py")
        # Simplify to relative module path
        if "src.parsers." in module or "src/parsers/" in str(p):
            fname = p.stem
            print(f"  python -c \"import asyncio; from src.parsers.{fname} import scrape; print(asyncio.run(scrape())[:3])\"")

    return 0 if fail == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Universal Web Inspector — inspect websites and optionally generate scrapers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url",  metavar="URL",  help="Single URL to inspect")
    group.add_argument("--urls", metavar="FILE", help="Text file with one URL per line")

    parser.add_argument(
        "--generate", "-g",
        action="store_true",
        help="Generate a scraper Python file after inspection",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Save full inspection report as JSON",
    )
    parser.add_argument(
        "--concurrency", "-c",
        type=int,
        default=3,
        metavar="N",
        help="Max parallel browser sessions for multi-URL mode (default: 3)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()
    _setup_logging(args.verbose)

    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
