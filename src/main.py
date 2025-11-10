thonfrom __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

from utils.logger import get_logger
from utils.deduplicator import (
    dedupe_emails,
    dedupe_phone_numbers,
    dedupe_social_profiles,
)
from crawler.html_parser import extract_contacts
from crawler.playwright_handler import is_playwright_available, render_page
from exporter.output_formatter import write_output

logger = get_logger(__name__)

@dataclass
class CrawlConfig:
    max_pages_per_site: int = 5
    concurrency: int = 10
    request_timeout: int = 20
    use_playwright: bool = True

async def _fetch_with_aiohttp(
    session: aiohttp.ClientSession, url: str, timeout: int
) -> str:
    try:
        async with session.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0 Safari/537.36"
                )
            },
        ) as resp:
            text = await resp.text(errors="ignore")
            logger.debug("Fetched %s with status %s", url, resp.status)
            return text
    except Exception as exc:
        logger.warning("Failed to fetch %s via HTTP: %s", url, exc)
        return ""

async def _fetch_html(
    session: aiohttp.ClientSession, url: str, cfg: CrawlConfig
) -> str:
    html = await _fetch_with_aiohttp(session, url, cfg.request_timeout)

    if not html:
        # Try Playwright if allowed
        if cfg.use_playwright and is_playwright_available():
            try:
                logger.info("Falling back to Playwright for %s", url)
                html = await render_page(url)
            except Exception as exc:  # pragma: no cover - depends on environment
                logger.warning("Playwright failed for %s: %s", url, exc)
        return html

    lowered = html.lower()
    if "enable javascript" in lowered or "please turn on javascript" in lowered:
        if cfg.use_playwright and is_playwright_available():
            try:
                logger.info("Detected JS-heavy page, using Playwright for %s", url)
                html = await render_page(url)
            except Exception as exc:  # pragma: no cover
                logger.warning("Playwright failed for %s: %s", url, exc)

    return html

def _classify_source_type(url: str, is_root: bool) -> str:
    if is_root:
        return "main-page"
    path = urlparse(url).path.lower()
    if "contact" in path or "kontakt" in path:
        return "contact-page"
    if "impressum" in path:
        return "legal-page"
    if "about" in path or "ueber-uns" in path or "uber-uns" in path:
        return "about-page"
    if "team" in path or "crew" in path or "people" in path:
        return "team-page"
    return "secondary-page"

async def _crawl_single_site(
    session: aiohttp.ClientSession, url: str, cfg: CrawlConfig
) -> List[Dict[str, Any]]:
    logger.info("Crawling %s", url)
    records: List[Dict[str, Any]] = []

    root_html = await _fetch_html(session, url, cfg)
    if not root_html:
        logger.error("No HTML retrieved for root URL %s", url)
        return records

    root_contacts = extract_contacts(root_html, url)
    root_record = _build_record(url, root_contacts, is_root=True)
    records.append(root_record)

    candidate_links = root_contacts.get("candidateLinks") or []
    # Limit links per config and ensure same domain
    base_domain = urlparse(url).netloc
    filtered_links: List[str] = []
    seen = set()
    for link in candidate_links:
        parsed = urlparse(link)
        if parsed.netloc and parsed.netloc != base_domain:
            continue
        if link in seen:
            continue
        seen.add(link)
        filtered_links.append(link)
        if len(filtered_links) >= cfg.max_pages_per_site:
            break

    async def process_link(link: str) -> Optional[Dict[str, Any]]:
        html = await _fetch_html(session, link, cfg)
        if not html:
            return None
        contacts = extract_contacts(html, link)
        return _build_record(link, contacts, is_root=False)

    tasks = [asyncio.create_task(process_link(link)) for link in filtered_links]
    for task in asyncio.as_completed(tasks):
        rec = await task
        if rec:
            records.append(rec)

    return records

def _build_record(
    url: str, contacts: Dict[str, Any], is_root: bool
) -> Dict[str, Any]:
    emails = dedupe_emails(contacts.get("emails", []))
    phones = dedupe_phone_numbers(contacts.get("phoneNumbers", []))
    socials = dedupe_social_profiles(contacts.get("socialProfiles", {}))

    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    record: Dict[str, Any] = {
        "url": url,
        "emails": emails,
        "phoneNumbers": phones,
        "socialProfiles": socials,
        "pageTitle": contacts.get("pageTitle", ""),
        "sourceType": _classify_source_type(url, is_root),
        "timestamp": timestamp,
    }
    return record

async def crawl_urls(urls: List[str], cfg: CrawlConfig) -> List[Dict[str, Any]]:
    connector = aiohttp.TCPConnector(limit_per_host=cfg.concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(cfg.concurrency)

        async def bounded_crawl(u: str) -> List[Dict[str, Any]]:
            async with sem:
                return await _crawl_single_site(session, u, cfg)

        tasks = [asyncio.create_task(bounded_crawl(u)) for u in urls]
        results: List[Dict[str, Any]] = []
        for task in asyncio.as_completed(tasks):
            site_records = await task
            results.extend(site_records)
        return results

def load_input_urls(path: str) -> List[str]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")
    with file_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    urls: List[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict) and "url" in item:
                urls.append(str(item["url"]))
    else:
        raise ValueError("Input JSON must be a list of URLs or objects with a 'url' field")
    return urls

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deep Email, Phone, & Social Media Scraper Search"
    )
    parser.add_argument(
        "--input",
        "-i",
        default="data/inputs.sample.json",
        help="Path to JSON file with list of URLs or objects containing a 'url' field.",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="data/output.sample.json",
        help="Path to output file (JSON or CSV).",
    )
    parser.add_argument(
        "--format",
        "-f",
        default="json",
        choices=["json", "csv", "both"],
        help="Output format.",
    )
    parser.add_argument(
        "--max-pages-per-site",
        type=int,
        default=5,
        help="Maximum number of secondary pages to crawl per site.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Maximum number of concurrent requests.",
    )
    parser.add_argument(
        "--no-playwright",
        action="store_true",
        help="Disable Playwright rendering even if installed.",
    )
    return parser.parse_args(argv)

def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    cfg = CrawlConfig(
        max_pages_per_site=args.max_pages_per_site,
        concurrency=args.concurrency,
        request_timeout=20,
        use_playwright=not args.no_playwright,
    )

    try:
        urls = load_input_urls(args.input)
    except Exception as exc:
        logger.error("Failed to load input URLs: %s", exc)
        raise SystemExit(1)

    if not urls:
        logger.error("No URLs provided in input file.")
        raise SystemExit(1)

    logger.info("Loaded %d URLs from %s", len(urls), args.input)
    if cfg.use_playwright and not is_playwright_available():
        logger.warning(
            "Playwright is enabled but not available. Install 'playwright' and run "
            "'playwright install' to enable JS rendering."
        )

    records = asyncio.run(crawl_urls(urls, cfg))

    if not records:
        logger.warning("No records extracted from any URL.")
    else:
        logger.info("Extracted %d records in total.", len(records))

    write_output(records, args.output, args.format)

if __name__ == "__main__":
    main()