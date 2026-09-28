"""Product page extraction for SKU/URL Excel workbooks."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import pandas as pd
from bs4 import BeautifulSoup

BASE_COLUMNS = [
    "SKU",
    "URL",
    "Title",
    "Bullet Points",
    "Category",
    "Product Description",
    "Specifications",
    "Status",
    "Error",
]
TAB_NAMES = ("Specifications", "Product Description")
AUTO_ENGINE = "Auto (Crawl4AI + CloakBrowser + Scrapling)"
MAX_CONCURRENCY = 16
ProgressCallback = Callable[[int, int, str, int, dict[str, Any]], None]


def _clean_cell(value: Any) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def validate_input(frame: pd.DataFrame) -> list[dict[str, str]]:
    """Validate required workbook columns and return normalized SKU/URL rows."""
    columns = {str(column).strip().casefold(): column for column in frame.columns}
    missing = [name for name in ("sku", "url") if name not in columns]
    if missing:
        raise ValueError(
            "The input workbook must have SKU and URL columns. Missing: "
            + ", ".join(name.upper() for name in missing)
        )

    items = []
    for row_number, (_, row) in enumerate(frame.iterrows(), start=2):
        sku = _clean_cell(row[columns["sku"]])
        url = _clean_cell(row[columns["url"]])
        if not sku and not url:
            continue
        items.append({"sku": sku, "url": url, "row_number": str(row_number)})
    if not items:
        raise ValueError("The workbook does not contain any SKU/URL rows.")
    return items


def _section_content(soup: BeautifulSoup, title: str):
    heading = next(
        (
            element
            for element in soup.find_all("h2")
            if element.get_text(" ", strip=True).casefold() == title.casefold()
        ),
        None,
    )
    if heading is None or heading.parent is None:
        return None
    return heading.parent.find_next_sibling("div")


def _normalize_spec_label(value: str) -> str:
    label = re.sub(r"\s+", " ", value).strip()
    return label[:1].upper() + label[1:] if label else ""


def parse_product_html(html: str, sku: str, url: str) -> dict[str, Any]:
    """Extract fields from one rendered product page snapshot."""
    soup = BeautifulSoup(html or "", "html.parser")
    title_element = soup.select_one('h1[data-testid="product-name"]')
    title = title_element.get_text(" ", strip=True) if title_element else ""

    highlight_content = _section_content(soup, "Product Highlights")
    bullet_points: list[str] = []
    if highlight_content is not None:
        bullet_points = [
            text
            for element in highlight_content.select("span.text-v2-body")
            if (text := element.get_text(" ", strip=True))
        ]
        if not bullet_points:
            bullet_points = [
                text
                for element in highlight_content.select("li")
                if (text := element.get_text(" ", strip=True))
            ]

    specification_content = _section_content(soup, "Specifications")
    specifications: dict[str, str] = {}
    if specification_content is not None:
        for row in specification_content.find_all("div"):
            fields = row.find_all("span", recursive=False)
            if len(fields) < 2:
                continue
            label = _normalize_spec_label(fields[0].get_text(" ", strip=True))
            value = fields[1].get_text(" ", strip=True)
            if label and value:
                specifications[label] = value

    description_content = _section_content(soup, "Product Description")
    description_element = (
        description_content.select_one(".prose")
        if description_content is not None
        else None
    )
    description = (
        description_element.get_text(" ", strip=True) if description_element else ""
    )

    breadcrumb_links = soup.select("div.text-v2-link a")
    category = " > ".join(
        text
        for element in breadcrumb_links
        if (text := element.get_text(" ", strip=True)) and text.casefold() != "home"
    )
    has_content = bool(title or bullet_points or specifications or description or category)

    return {
        "SKU": sku,
        "URL": url,
        "Title": title,
        "Bullet Points": "\n".join(bullet_points),
        "Category": category,
        "Product Description": description,
        "Status": "Success" if has_content else "No data",
        "Error": "",
        "_specifications": specifications,
    }


def _merge_snapshots(
    sku: str,
    url: str,
    initial_html: str,
    tab_html: dict[str, str],
) -> dict[str, Any]:
    result = parse_product_html(initial_html, sku, url)
    specifications = result["_specifications"]
    for tab_name, html in tab_html.items():
        extracted = parse_product_html(html, sku, url)
        specifications.update(extracted["_specifications"])
        if tab_name == "Product Description" and extracted["Product Description"]:
            result["Product Description"] = extracted["Product Description"]
    result["_specifications"] = specifications
    has_content = bool(
        result["Title"]
        or result["Bullet Points"]
        or specifications
        or result["Product Description"]
        or result["Category"]
    )
    result["Status"] = "Success" if has_content else "No data"
    return result


def _tab_click_script(tab_name: str) -> str:
    encoded_name = json.dumps(tab_name)
    return (
        "(() => { const name = "
        + encoded_name
        + "; const button = [...document.querySelectorAll('button')].find("
        "item => item.textContent.trim() === name); if (button) button.click(); })()"
    )


async def _scrape_crawl4ai(
    items: list[dict[str, str]],
    progress: ProgressCallback | None,
    concurrency: int,
    auto_fallback: bool = False,
) -> list[dict[str, Any]]:
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    except ImportError as exc:
        raise RuntimeError(
            "Crawl4AI is not installed. Install the project requirements first."
        ) from exc

    results: list[dict[str, Any] | None] = [None] * len(items)
    semaphore = asyncio.Semaphore(min(max(1, concurrency), MAX_CONCURRENCY))
    queue: asyncio.Queue[tuple[int, dict[str, str]]] = asyncio.Queue()
    for index, item in enumerate(items, start=1):
        queue.put_nowait((index, item))
    completed = 0
    async with AsyncWebCrawler(
        config=BrowserConfig(headless=True, verbose=False)
    ) as crawler:
        async def scrape_one(index: int, item: dict[str, str]) -> None:
            nonlocal completed
            sku, url = item["sku"], item["url"]
            async with semaphore:
                session_id = f"product-{index}"
                try:
                    parsed_url = urlparse(url)
                    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                        raise ValueError("URL must start with http:// or https://")

                    initial = await crawler.arun(
                        url=url,
                        config=CrawlerRunConfig(
                            session_id=session_id,
                            wait_until="domcontentloaded",
                            page_timeout=60_000,
                        ),
                    )
                    if not initial.success:
                        raise RuntimeError(initial.error_message or "Page navigation failed")

                    tab_html: dict[str, str] = {}
                    for tab_name in TAB_NAMES:
                        tab_result = await crawler.arun(
                            url=url,
                            config=CrawlerRunConfig(
                                session_id=session_id,
                                js_code=[_tab_click_script(tab_name)],
                                delay_before_return_html=1.0,
                                wait_until="domcontentloaded",
                                page_timeout=60_000,
                            ),
                        )
                        if tab_result.success:
                            tab_html[tab_name] = tab_result.html or ""
                    result = _merge_snapshots(sku, url, initial.html or "", tab_html)
                    if result["Status"] == "No data":
                        result["Error"] = "No product fields were found on the page."
                except Exception as exc:
                    result = _error_result(item, str(exc))
                finally:
                    try:
                        await crawler.crawler_strategy.kill_session(session_id)
                    except Exception:
                        pass

                if auto_fallback and result["Status"] != "Success":
                    errors = [result["Error"] or result["Status"]]
                    for browser_name, browser_scrape in (
                        ("CloakBrowser", _scrape_cloakbrowser),
                        ("Scrapling", _scrape_scrapling),
                    ):
                        try:
                            fallback_result = await asyncio.to_thread(browser_scrape, item)
                            if fallback_result["Status"] == "Success":
                                result = fallback_result
                                break
                            errors.append(
                                f"{browser_name}: {fallback_result['Error'] or 'No data'}"
                            )
                        except Exception as exc:
                            errors.append(f"{browser_name}: {exc}")
                    if result["Status"] != "Success":
                        result["Error"] = "; ".join(errors)

                results[index - 1] = result
                completed += 1
                if progress:
                    progress(
                        completed,
                        len(items),
                        f"Processed {sku or url}",
                        index - 1,
                        result,
                    )

        async def worker() -> None:
            while True:
                try:
                    index, item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await scrape_one(index, item)

        worker_count = min(max(1, concurrency), MAX_CONCURRENCY, len(items))
        await asyncio.gather(*(worker() for _ in range(worker_count)))
    return [result for result in results if result is not None]


def _scrape_cloakbrowser(item: dict[str, str]) -> dict[str, Any]:
    try:
        from cloakbrowser import launch
    except ImportError as exc:
        raise RuntimeError(
            "CloakBrowser is not installed. Install the project requirements first."
        ) from exc

    url = item["url"]
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("URL must start with http:// or https://")

    snapshots: dict[str, str] = {}
    browser = launch(headless=True)
    try:
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1_000)
        snapshots["initial"] = page.content()
        for tab_name in TAB_NAMES:
            button = page.get_by_role("button", name=tab_name, exact=True)
            if button.count():
                button.click(timeout=5_000)
                page.wait_for_timeout(900)
                snapshots[tab_name] = page.content()
    finally:
        browser.close()

    result = _merge_snapshots(
        item["sku"],
        url,
        snapshots.get("initial", ""),
        {key: value for key, value in snapshots.items() if key != "initial"},
    )
    if result["Status"] == "No data":
        result["Error"] = "No product fields were found on the page."
    return result


def _scrape_scrapling(item: dict[str, str]) -> dict[str, Any]:
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError as exc:
        raise RuntimeError(
            "Scrapling is not installed. Install the project requirements first."
        ) from exc

    url = item["url"]
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("URL must start with http:// or https://")

    snapshots: dict[str, str] = {}

    def capture_tabs(page: Any) -> None:
        snapshots["initial"] = page.content()
        for tab_name in TAB_NAMES:
            button = page.get_by_role("button", name=tab_name, exact=True)
            if button.count():
                button.click(timeout=5_000)
                page.wait_for_timeout(900)
                snapshots[tab_name] = page.content()

    StealthyFetcher.fetch(
        url,
        headless=True,
        network_idle=False,
        timeout=60_000,
        wait=500,
        page_action=capture_tabs,
    )
    initial_html = snapshots.get("initial", "")
    if not initial_html:
        raise RuntimeError("Scrapling did not return rendered page content.")
    result = _merge_snapshots(
        item["sku"],
        url,
        initial_html,
        {key: value for key, value in snapshots.items() if key != "initial"},
    )
    if result["Status"] == "No data":
        result["Error"] = "No product fields were found on the page."
    return result


def _error_result(item: dict[str, str], error: str) -> dict[str, Any]:
    return {
        "SKU": item["sku"],
        "URL": item["url"],
        "Title": "",
        "Bullet Points": "",
        "Category": "",
        "Product Description": "",
        "Status": "Error",
        "Error": error,
        "_specifications": {},
    }


async def _scrape_sync_batch(
    items: list[dict[str, str]],
    scrape_one: Callable[[dict[str, str]], dict[str, Any]],
    concurrency: int,
    progress: ProgressCallback | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any] | None] = [None] * len(items)
    queue: asyncio.Queue[tuple[int, dict[str, str]]] = asyncio.Queue()
    for index, item in enumerate(items):
        queue.put_nowait((index, item))
    completed = 0

    async def worker() -> None:
        nonlocal completed
        while not queue.empty():
            index, item = await queue.get()
            try:
                result = await asyncio.to_thread(scrape_one, item)
            except Exception as exc:
                result = _error_result(item, str(exc))
            results[index] = result
            completed += 1
            if progress:
                progress(
                    completed,
                    len(items),
                    f"Processed {item['sku'] or item['url']}",
                    index,
                    result,
                )

    worker_count = min(max(1, concurrency), MAX_CONCURRENCY, len(items))
    await asyncio.gather(*(worker() for _ in range(worker_count)))
    return [result for result in results if result is not None]


def _scrape_auto_fallback(item: dict[str, str]) -> dict[str, Any]:
    errors = []
    for browser_name, browser_scrape in (
        ("CloakBrowser", _scrape_cloakbrowser),
        ("Scrapling", _scrape_scrapling),
    ):
        try:
            result = browser_scrape(item)
            if result["Status"] == "Success":
                return result
            errors.append(f"{browser_name}: {result['Error'] or 'No data'}")
        except Exception as exc:
            errors.append(f"{browser_name}: {exc}")
    return _error_result(item, "; ".join(errors))


def results_to_frame(results: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for result in results:
        row = {column: result.get(column, "") for column in BASE_COLUMNS}
        specifications = result.get("_specifications", {})
        row["Specifications"] = "\n".join(
            f"{label}: {value}" for label, value in specifications.items()
        )
        rows.append(row)
    return pd.DataFrame(rows, columns=BASE_COLUMNS)


def scrape_workbook_rows(
    frame: pd.DataFrame,
    engine: str = AUTO_ENGINE,
    concurrency: int = 4,
    progress: ProgressCallback | None = None,
) -> pd.DataFrame:
    """Scrape validated workbook rows with Crawl4AI, Scrapling, or fallback mode."""
    items = validate_input(frame)
    concurrency = min(max(1, int(concurrency)), MAX_CONCURRENCY)
    if engine == "Scrapling Stealth":
        results = asyncio.run(
            _scrape_sync_batch(items, _scrape_scrapling, concurrency, progress)
        )
    elif engine == "CloakBrowser":
        results = asyncio.run(
            _scrape_sync_batch(items, _scrape_cloakbrowser, concurrency, progress)
        )
    elif engine == "Crawl4AI":
        results = asyncio.run(_scrape_crawl4ai(items, progress, concurrency))
    elif engine == AUTO_ENGINE:
        try:
            results = asyncio.run(
                _scrape_crawl4ai(items, progress, concurrency, auto_fallback=True)
            )
        except Exception:
            results = asyncio.run(
                _scrape_sync_batch(items, _scrape_auto_fallback, concurrency, progress)
            )
    else:
        raise ValueError(f"Unknown browser engine: {engine}")
    return results_to_frame(results)