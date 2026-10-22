from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


CATALOGUE_URL = "https://books.toscrape.com/"
USER_AGENT = os.getenv(
    "SCRAPER_USER_AGENT",
    "PoliteScraperA9/1.0 (+https://books.toscrape.com/)",
)
REQUEST_TIMEOUT_SECONDS = 10
MIN_REQUEST_INTERVAL_SECONDS = 0.5
RETRY_WAIT_SECONDS = 1
CATALOGUE_PAGE_LIMIT = 3
LOGGER = logging.getLogger("polite_scraper")


class ScrapeError(RuntimeError):
    pass


class PageFetchError(ScrapeError):
    pass


class RecordExtractionError(ValueError):
    pass


class BookRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    product_url: str
    price_text: str
    availability_text: str
    rating_text: str
    description: str | None
    source_page: str
    fetched_at: str
    price_gbp: float

    @field_validator("title", "price_text", "availability_text", "rating_text")
    @classmethod
    def require_nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("product_url", "source_page")
    @classmethod
    def require_https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("must be an absolute HTTPS URL")
        return value

    @field_validator("fetched_at")
    @classmethod
    def require_timestamp(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("must be an ISO 8601 timestamp") from exc
        return value

    @field_validator("price_gbp")
    @classmethod
    def require_finite_nonnegative_price(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("must be a finite, non-negative number")
        return value


@dataclass(frozen=True)
class FetchResult:
    html: str
    fetched_at: str
    from_cache: bool


@dataclass
class RunStats:
    started_at: str
    started_clock: float
    catalogue_pages: int = 0
    discovered_urls: int = 0
    unique_urls: int = 0
    pages_fetched: int = 0
    cache_hits: int = 0
    valid_records: int = 0
    invalid_records: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def add_failure(self, url: str, reason: str, kind: str) -> None:
        self.failures.append({"url": url, "reason": reason, "kind": kind})

    def as_dict(self) -> dict[str, Any]:
        duration = max(0.0, time.monotonic() - self.started_clock)
        return {
            "started_at": self.started_at,
            "duration_seconds": round(duration, 3),
            "catalogue_pages": self.catalogue_pages,
            "discovered_urls": self.discovered_urls,
            "unique_urls": self.unique_urls,
            "pages_fetched": self.pages_fetched,
            "cache_hits": self.cache_hits,
            "valid_records": self.valid_records,
            "invalid_records": self.invalid_records,
            "failed_pages": len(self.failures),
            "failures": self.failures,
        }


class PoliteFetcher:
    def __init__(
        self,
        cache_dir: Path,
        session: requests.Session | None = None,
        user_agent: str = USER_AGENT,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        minimum_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self.cache_dir = cache_dir
        self.session = session or requests.Session()
        self.user_agent = user_agent
        self.timeout = timeout
        self.minimum_interval = minimum_interval
        self._last_request_at: float | None = None

    def fetch(self, url: str) -> FetchResult:
        html_path, metadata_path = self._cache_paths(url)
        if html_path.exists() or metadata_path.exists():
            if not html_path.is_file() or not metadata_path.is_file():
                raise PageFetchError(f"Incomplete cache entry for {url}")
            try:
                html = html_path.read_text(encoding="utf-8")
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                fetched_at = metadata["fetched_at"]
                if metadata["url"] != url or not isinstance(fetched_at, str):
                    raise ValueError("cache metadata does not match the URL")
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PageFetchError(f"Invalid cache entry for {url}: {exc}") from exc
            print(f"CACHE HIT {url} ({len(html.encode('utf-8'))} bytes)")
            return FetchResult(html=html, fetched_at=fetched_at, from_cache=True)

        for attempt in range(2):
            self._wait_for_request_slot()
            try:
                response = self.session.get(
                    url,
                    headers={"User-Agent": self.user_agent},
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.Timeout as exc:
                self._last_request_at = time.monotonic()
                if attempt == 0:
                    LOGGER.warning("Request timed out; retrying once: %s", url)
                    time.sleep(RETRY_WAIT_SECONDS)
                    continue
                raise PageFetchError(f"Request timed out after one retry: {url}") from exc
            except requests.RequestException as exc:
                self._last_request_at = time.monotonic()
                raise PageFetchError(f"Request failed for {url}: {exc}") from exc

            self._last_request_at = time.monotonic()
            if 500 <= response.status_code <= 599 and attempt == 0:
                LOGGER.warning(
                    "Server returned %s; retrying once: %s",
                    response.status_code,
                    url,
                )
                time.sleep(RETRY_WAIT_SECONDS)
                continue
            if response.status_code != 200:
                raise PageFetchError(
                    f"Unexpected HTTP status {response.status_code} for {url}"
                )

            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
            html = response.content.decode(response.encoding or "utf-8", errors="replace")
            self._write_cache(html_path, metadata_path, url, html, fetched_at)
            print(f"FETCH {url} ({len(response.content)} bytes)")
            return FetchResult(html=html, fetched_at=fetched_at, from_cache=False)

        raise PageFetchError(f"Server error after one retry for {url}")

    def _wait_for_request_slot(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.minimum_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        parsed = urlsplit(url)
        catalogue_match = re.fullmatch(r"/catalogue/page-(\d+)\.html", parsed.path)
        if parsed.netloc == "books.toscrape.com" and parsed.path == "/":
            filename = "catalogue-page-1.html"
        elif parsed.netloc == "books.toscrape.com" and catalogue_match:
            filename = f"catalogue-page-{catalogue_match.group(1)}.html"
        else:
            filename = f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.html"
        html_path = self.cache_dir / filename
        return html_path, html_path.with_suffix(".meta.json")

    @staticmethod
    def _write_cache(
        html_path: Path,
        metadata_path: Path,
        url: str,
        html: str,
        fetched_at: str,
    ) -> None:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(html_path, html)
        metadata = {"url": url, "fetched_at": fetched_at}
        _atomic_write_text(
            metadata_path,
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        newline="\n",
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def absolute_url(href: str, page_url: str) -> str:
    return urljoin(page_url, href.strip())


def _require_target_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "books.toscrape.com":
        raise ValueError(f"URL is outside the approved sandbox: {url}")
    return url


def deduplicate_book_urls(
    book_pages: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    unique: dict[str, str] = {}
    for product_url, source_page in book_pages:
        unique.setdefault(product_url, source_page)
    return list(unique.items())


def parse_catalogue_page(
    html: str, page_url: str
) -> tuple[list[str], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for anchor in soup.select("article.product_pod h3 a"):
        href = anchor.get("href")
        if href:
            links.append(_require_target_url(absolute_url(href, page_url)))
    if not links:
        raise RecordExtractionError("No book links found in catalogue page")

    next_anchor = soup.select_one("li.next a")
    next_url = None
    if next_anchor and next_anchor.get("href"):
        next_url = _require_target_url(
            absolute_url(next_anchor["href"], page_url)
        )
    return links, next_url


def discover_book_pages(
    fetcher: PoliteFetcher,
    stats: RunStats,
) -> list[tuple[str, str]]:
    page_url = CATALOGUE_URL
    seen_catalogue_pages: set[str] = set()
    discovered: list[tuple[str, str]] = []

    for page_number in range(1, CATALOGUE_PAGE_LIMIT + 1):
        if page_url in seen_catalogue_pages:
            stats.add_failure(page_url, "Catalogue pagination loop detected", "catalogue")
            break
        seen_catalogue_pages.add(page_url)

        try:
            result = fetcher.fetch(page_url)
            _count_fetch(result, stats)
            product_urls, next_url = parse_catalogue_page(result.html, page_url)
        except (PageFetchError, RecordExtractionError, ValueError) as exc:
            stats.add_failure(page_url, str(exc), "catalogue")
            LOGGER.error("Could not process catalogue page %s: %s", page_url, exc)
            break

        stats.catalogue_pages += 1
        discovered.extend((url, page_url) for url in product_urls)
        if page_number < CATALOGUE_PAGE_LIMIT:
            if next_url is None:
                stats.add_failure(
                    page_url,
                    f"Catalogue has no next link before page {page_number + 1}",
                    "catalogue",
                )
                break
            page_url = next_url

    stats.discovered_urls = len(discovered)
    unique_pages = deduplicate_book_urls(discovered)
    stats.unique_urls = len(unique_pages)
    return unique_pages


def _count_fetch(result: FetchResult, stats: RunStats) -> None:
    if result.from_cache:
        stats.cache_hits += 1
    else:
        stats.pages_fetched += 1


def extract_raw_record(
    html: str,
    product_url: str,
    source_page: str,
    fetched_at: str,
) -> dict[str, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    product = soup.select_one(".product_page")
    if product is None:
        raise RecordExtractionError("Product area .product_page was not found")

    title_element = product.select_one("h1")
    price_element = product.select_one(".price_color")
    availability_element = product.select_one(".availability")
    rating_element = product.select_one(".star-rating")
    missing = [
        name
        for name, element in (
            ("title", title_element),
            ("price", price_element),
            ("availability", availability_element),
            ("rating", rating_element),
        )
        if element is None
    ]
    if missing:
        raise RecordExtractionError(f"Missing product fields: {', '.join(missing)}")

    rating_classes = rating_element.get("class", [])
    rating_text = next(
        (class_name for class_name in rating_classes if class_name != "star-rating"),
        "",
    )
    if not rating_text:
        raise RecordExtractionError("Product rating was empty")

    description_heading = product.select_one("#product_description")
    description_element = (
        description_heading.find_next_sibling("p") if description_heading else None
    )
    description = (
        description_element.get_text(" ", strip=True)
        if description_element is not None
        else None
    )
    if description == "":
        description = None

    return {
        "title": title_element.get_text(" ", strip=True),
        "product_url": product_url,
        "price_text": price_element.get_text(" ", strip=True),
        "availability_text": availability_element.get_text(" ", strip=True),
        "rating_text": rating_text,
        "description": description,
        "source_page": source_page,
        "fetched_at": fetched_at,
    }


def normalize_price(price_text: str) -> float:
    match = re.search(r"\d+(?:\.\d{1,2})?", price_text.replace(",", ""))
    if not match:
        raise ValueError(f"Could not parse a GBP price from {price_text!r}")
    return float(Decimal(match.group()))


def validate_record(raw_record: dict[str, str | None]) -> BookRecord:
    price_text = raw_record.get("price_text")
    if not isinstance(price_text, str):
        raise ValueError("price_text must be a string")
    return BookRecord.model_validate(
        {**raw_record, "price_gbp": normalize_price(price_text)}
    )


def _write_json(path: Path, value: Any) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )


def run_scraper(
    project_dir: Path,
    test_broken_page: bool = False,
) -> dict[str, Any]:
    output_dir = project_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    stats = RunStats(started_at=started_at, started_clock=time.monotonic())
    fetcher = PoliteFetcher(project_dir / "cache")
    valid_records: dict[str, dict[str, Any]] = {}
    invalid_records: list[dict[str, Any]] = []

    try:
        book_pages = discover_book_pages(fetcher, stats)
        pages_to_fetch = list(book_pages)
        if test_broken_page:
            pages_to_fetch.append(
                (
                    "https://books.toscrape.com/catalogue/polite-scraper-deliberate-404.html",
                    CATALOGUE_URL,
                )
            )

        for product_url, source_page in pages_to_fetch:
            try:
                _require_target_url(product_url)
                result = fetcher.fetch(product_url)
                _count_fetch(result, stats)
                raw_record = extract_raw_record(
                    result.html,
                    product_url,
                    source_page,
                    result.fetched_at,
                )
                try:
                    validated = validate_record(raw_record)
                except (ValidationError, ValueError) as exc:
                    stats.invalid_records += 1
                    invalid_records.append(
                        {"product_url": product_url, "record": raw_record, "reason": str(exc)}
                    )
                    LOGGER.error("Invalid record for %s: %s", product_url, exc)
                    continue

                valid_records[validated.product_url] = validated.model_dump(mode="json")
                stats.valid_records = len(valid_records)
            except (PageFetchError, RecordExtractionError, ValueError) as exc:
                stats.add_failure(product_url, str(exc), "detail")
                LOGGER.error("Skipping product page %s: %s", product_url, exc)
                if isinstance(exc, RecordExtractionError):
                    stats.invalid_records += 1
                    invalid_records.append(
                        {"product_url": product_url, "reason": str(exc)}
                    )
    finally:
        stats.valid_records = len(valid_records)
        _write_json(output_dir / "books.json", list(valid_records.values()))
        _write_json(output_dir / "errors.json", invalid_records)
        report = stats.as_dict()
        _write_json(output_dir / "run-report.json", report)

    print(
        "SUMMARY "
        f"catalogue_pages={stats.catalogue_pages} "
        f"discovered={stats.discovered_urls} "
        f"unique_urls={stats.unique_urls} "
        f"detail_pages={stats.unique_urls} "
        f"valid_records={stats.valid_records} "
        f"invalid_records={stats.invalid_records} "
        f"failed_pages={len(stats.failures)} "
        f"cache_hits={stats.cache_hits}"
    )
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Polite Books to Scrape catalogue scraper")
    parser.add_argument(
        "--test-broken-page",
        action="store_true",
        help="Request one deliberate sandbox 404 to verify failure handling",
    )
    arguments = parser.parse_args()
    project_dir = Path(__file__).resolve().parents[1]
    report = run_scraper(project_dir, test_broken_page=arguments.test_broken_page)
    return 1 if report["failed_pages"] or report["invalid_records"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
