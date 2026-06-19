"""
Optional headless rendering via Playwright/Chromium.

AI crawlers and our static fetch don't run JavaScript, so JS-only job boards
(Duda/Shazamme widgets, SPAs) hide their real content. When ENABLE_HEADLESS=1
and Chromium is available, we render the page and hand the post-JS DOM back to
the grader so apply/search/AEO/crawlability are judged on what actually renders.

Fully optional and best-effort: returns None on any failure, so grading never
depends on it.
"""

import os
import sys


def enabled() -> bool:
    return os.environ.get('ENABLE_HEADLESS', '0') == '1'


def _log(msg):
    print(f'[renderer] {msg}', file=sys.stderr, flush=True)


async def render_html(url: str, timeout_ms: int = 25000):
    if not enabled():
        return None
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        _log(f'playwright import failed: {e}')
        return None

    ua = ('Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) '
          'Chrome/124.0 Mobile Safari/537.36')
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                      '--single-process', '--no-zygote'])
            try:
                page = await browser.new_page(user_agent=ua, viewport={'width': 390, 'height': 844})
                # domcontentloaded is far more reliable than networkidle for SPAs;
                # then give the JS a few seconds to populate the DOM.
                await page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
                await page.wait_for_timeout(3500)
                html = await page.content()
                _log(f'rendered {url} -> {len(html)} chars')
                return html
            finally:
                await browser.close()
    except Exception as e:
        _log(f'render failed for {url}: {type(e).__name__}: {e}')
        return None
