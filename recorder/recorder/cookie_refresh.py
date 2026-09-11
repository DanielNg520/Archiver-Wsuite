"""Keep a TikTok session cookie file alive via simulated headless browsing.

Converts Playwright cookie dictionaries into Netscape cookie-file text.
"""

import asyncio
import logging
import random
from pathlib import Path

log = logging.getLogger(__name__)


def _playwright_to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = cookie["domain"]
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie["secure"] else "FALSE"
        expires = "0" if cookie["expires"] == -1 else str(int(cookie["expires"]))
        lines.append(
            "\t".join(
                (
                    domain,
                    include_subdomains,
                    cookie["path"],
                    secure,
                    expires,
                    cookie["name"],
                    cookie["value"],
                )
            )
        )
    return "\n".join(lines) + "\n"


def simulate_human_browsing(config) -> bool:
    cookies_file = config.tiktok_cookies_file
    if not cookies_file or not Path(cookies_file).exists() or Path(cookies_file).stat().st_size == 0:
        log.info("Skipping TikTok cookie refresh: no valid cookies file found")
        return False
    return asyncio.run(_refresh_async(cookies_file))


async def _refresh_async(cookies_file: str) -> bool:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "cookie refresh needs the browser fallback, but playwright is not installed. Run: python -m playwright install chromium"
        ) from exc

    from .platforms.tiktok_browser import _launch_chromium, _netscape_to_playwright

    async with async_playwright() as pw:
        browser = await _launch_chromium(pw)
        try:
            ctx = await browser.new_context()
            await ctx.add_cookies(_netscape_to_playwright(cookies_file))
            page = await ctx.new_page()
            await page.goto(
                "https://www.tiktok.com/foryou",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            for _ in range(random.randint(4, 8)):
                await page.mouse.wheel(0, random.randint(300, 1200))
                await asyncio.sleep(random.uniform(3.0, 7.0))
            updated = await ctx.cookies()
            Path(cookies_file).write_text(_playwright_to_netscape(updated))
            return True
        finally:
            await browser.close()
