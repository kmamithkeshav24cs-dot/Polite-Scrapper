import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.main import (
    PageFetchError,
    PoliteFetcher,
    RecordExtractionError,
    absolute_url,
    deduplicate_book_urls,
    extract_raw_record,
    normalize_price,
)


class ScraperUnitTests(unittest.TestCase):
    def test_normalizes_gbp_price_to_number(self):
        self.assertEqual(normalize_price("£1,234.50"), 1234.5)

    def test_resolves_relative_url_against_page_url(self):
        self.assertEqual(
            absolute_url("../book/index.html", "https://books.toscrape.com/catalogue/page-1.html"),
            "https://books.toscrape.com/book/index.html",
        )

    def test_missing_description_is_null(self):
        html = """
        <div class="product_page">
          <h1>A book</h1>
          <p class="price_color">£12.00</p>
          <p class="availability">In stock</p>
          <p class="star-rating Three"></p>
        </div>
        """
        record = extract_raw_record(
            html,
            "https://books.toscrape.com/catalogue/book/index.html",
            "https://books.toscrape.com/",
            "2026-10-22T00:00:00Z",
        )
        self.assertIsNone(record["description"])
        self.assertEqual(set(record), {
            "title",
            "product_url",
            "price_text",
            "availability_text",
            "rating_text",
            "description",
            "source_page",
            "fetched_at",
        })

    def test_duplicate_product_urls_keep_first_source_page(self):
        pages = [
            ("https://books.toscrape.com/book/1", "https://books.toscrape.com/page-1"),
            ("https://books.toscrape.com/book/1", "https://books.toscrape.com/page-2"),
            ("https://books.toscrape.com/book/2", "https://books.toscrape.com/page-2"),
        ]
        self.assertEqual(
            deduplicate_book_urls(pages),
            [
                ("https://books.toscrape.com/book/1", "https://books.toscrape.com/page-1"),
                ("https://books.toscrape.com/book/2", "https://books.toscrape.com/page-2"),
            ],
        )

    def test_malformed_product_fixture_is_rejected(self):
        with self.assertRaisesRegex(RecordExtractionError, "Missing product fields"):
            extract_raw_record(
                '<div class="product_page"><h1>Incomplete</h1></div>',
                "https://books.toscrape.com/catalogue/broken/index.html",
                "https://books.toscrape.com/",
                "2026-10-22T00:00:00Z",
            )

    def test_404_is_not_retried_and_request_is_identified(self):
        response = Mock(status_code=404)
        session = Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            fetcher = PoliteFetcher(Path(directory), session=session, user_agent="test-agent")
            with self.assertRaisesRegex(PageFetchError, "HTTP status 404"):
                fetcher.fetch("https://books.toscrape.com/missing.html")

        session.get.assert_called_once_with(
            "https://books.toscrape.com/missing.html",
            headers={"User-Agent": "test-agent"},
            timeout=10,
            allow_redirects=False,
        )


if __name__ == "__main__":
    unittest.main()
