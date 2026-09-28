# GitHub Codespaces ♥️ Jupyter Notebooks

# Product Catalog Extractor

Upload an Excel workbook with `SKU` and `URL` columns to extract product titles, highlights, category breadcrumbs, descriptions, and specifications into a downloadable workbook. All specification key/value pairs are combined in one multiline cell, and each row includes a status and error message.

## Run locally

```bash
python -m pip install -r requirements.txt
crawl4ai-setup
scrapling install
streamlit run app.py
```

The app accepts `.xlsx` input. Use the built-in template for the expected columns. The automatic mode tries Crawl4AI first, then CloakBrowser, then Scrapling Stealth for rows that could not be extracted. Use the concurrency slider to set 1-16 active products; start at 4 and lower it if you see rate limits or resource pressure. While scraping, the app previews the latest 50 completed products and keeps the full result set for export. Browser binaries are downloaded separately during setup or first launch.

The provided Codespaces image uses Ubuntu 20.04, which current Playwright browser installers no longer support. Run this app in a supported Linux image (Ubuntu 22.04 or newer) or another supported desktop environment to enable browser extraction. The workbook interface and parser tests can still run without installed browsers.

Use the scraper only for sites you are authorized to access and in accordance with their terms. Anti-bot handling is best-effort; CAPTCHAs and access restrictions may still prevent extraction.

Run the parser tests with:

```bash
python -m unittest discover -s tests
```
