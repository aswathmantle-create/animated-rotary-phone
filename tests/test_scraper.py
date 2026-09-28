import unittest
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from scraper import (
    _scrape_crawl4ai,
    _scrape_sync_batch,
    parse_product_html,
    results_to_frame,
    validate_input,
)


PRODUCT_HTML = """
<h1 data-testid="product-name">Samsung Phone</h1>
<section>
  <div><span></span><h2>Product Highlights</h2></div>
  <div><div><span class="text-v2-body">Bright display</span></div>
       <div><span class="text-v2-body">Long battery life</span></div></div>
</section>
<section>
  <div><span></span><h2>Specifications</h2></div>
  <div><div><span>ram</span><span>12 GB</span></div>
       <div><span>Product Type</span><span>Smartphone</span></div></div>
</section>
<section>
  <div><span></span><h2>Product Description</h2></div>
  <div><div class="prose">A phone for everyday use.</div></div>
</section>
<div class="text-v2-link"><a href="/">Home</a><a href="/electronics">Electronics</a>
  <a href="/phones">Mobiles</a></div>
"""


class ScraperParserTests(unittest.TestCase):
    def test_extracts_product_fields_and_specifications(self):
        result = parse_product_html(PRODUCT_HTML, "SKU-1", "https://example.com/item")

        self.assertEqual(result["Title"], "Samsung Phone")
        self.assertEqual(result["Bullet Points"], "Bright display\nLong battery life")
        self.assertEqual(result["Category"], "Electronics > Mobiles")
        self.assertEqual(result["Product Description"], "A phone for everyday use.")
        self.assertEqual(result["_specifications"], {"Ram": "12 GB", "Product Type": "Smartphone"})
        self.assertEqual(result["Status"], "Success")

    def test_specifications_are_combined_into_one_cell(self):
        first = parse_product_html(PRODUCT_HTML, "SKU-1", "https://example.com/1")
        second = parse_product_html("<h1 data-testid='product-name'>Other</h1>", "SKU-2", "https://example.com/2")
        output = results_to_frame([first, second])

        self.assertIn("Specifications", output.columns)
        self.assertNotIn("Ram", output.columns)
        self.assertEqual(
            output.loc[0, "Specifications"],
            "Ram: 12 GB\nProduct Type: Smartphone",
        )
        self.assertEqual(output.loc[1, "Specifications"], "")

    def test_input_columns_are_case_insensitive_and_blank_rows_are_skipped(self):
        items = validate_input(
            pd.DataFrame(
                {" sku ": ["001", None], "URL": ["https://example.com/item", None]}
            )
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sku"], "001")

    def test_input_requires_sku_and_url_columns(self):
        with self.assertRaisesRegex(ValueError, "SKU and URL"):
            validate_input(pd.DataFrame({"SKU": ["001"]}))

    def test_progress_callback_receives_partial_results_after_each_item(self):
        items = [
            {"sku": "SKU-1", "url": "https://example.com/1"},
            {"sku": "SKU-2", "url": "https://example.com/2"},
        ]
        scraped_rows = [
            {
                "SKU": item["sku"],
                "URL": item["url"],
                "Title": item["sku"],
                "Bullet Points": "",
                "Category": "",
                "Product Description": "",
                "Status": "Success",
                "Error": "",
                "_specifications": {},
            }
            for item in items
        ]
        updates = []

        scraped_by_sku = {row["SKU"]: row for row in scraped_rows}
        asyncio.run(
            _scrape_sync_batch(
                items,
                lambda item: scraped_by_sku[item["sku"]],
                2,
                lambda done, total, message, row_index, result: updates.append(
                    (done, total, row_index, result["SKU"])
                ),
            )
        )

        self.assertEqual([update[:2] for update in updates], [(1, 2), (2, 2)])
        self.assertEqual({update[2] for update in updates}, {0, 1})

    def test_sync_workers_honor_configured_concurrency(self):
        items = [
            {"sku": f"SKU-{index}", "url": f"https://example.com/{index}"}
            for index in range(6)
        ]
        barrier = threading.Barrier(3, timeout=5)
        lock = threading.Lock()
        active = 0
        peak_active = 0

        def scrape_one(item):
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            barrier.wait()
            with lock:
                active -= 1
            return {
                "SKU": item["sku"],
                "URL": item["url"],
                "Title": item["sku"],
                "Bullet Points": "",
                "Category": "",
                "Product Description": "",
                "Status": "Success",
                "Error": "",
                "_specifications": {},
            }

        results = asyncio.run(_scrape_sync_batch(items, scrape_one, 3, None))

        self.assertEqual(len(results), len(items))
        self.assertEqual(peak_active, 3)

    def test_crawl4ai_workers_use_the_configured_limit(self):
        items = [
            {"sku": f"SKU-{index}", "url": f"https://example.com/{index}"}
            for index in range(6)
        ]

        class FakeCrawler:
            def __init__(self):
                self.active = 0
                self.peak_active = 0
                self.crawler_strategy = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def arun(self, **_kwargs):
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
                await asyncio.sleep(0.005)
                self.active -= 1
                return SimpleNamespace(
                    success=True,
                    html='<h1 data-testid="product-name">Test item</h1>',
                    error_message="",
                )

            async def kill_session(self, _session_id):
                return None

        crawler = FakeCrawler()
        with (
            patch("crawl4ai.AsyncWebCrawler", side_effect=lambda **_kwargs: crawler),
            patch("crawl4ai.BrowserConfig", side_effect=lambda **kwargs: kwargs),
            patch("crawl4ai.CrawlerRunConfig", side_effect=lambda **kwargs: kwargs),
        ):
            results = asyncio.run(_scrape_crawl4ai(items, None, concurrency=3))

        self.assertEqual(len(results), len(items))
        self.assertGreater(crawler.peak_active, 1)
        self.assertLessEqual(crawler.peak_active, 3)


if __name__ == "__main__":
    unittest.main()