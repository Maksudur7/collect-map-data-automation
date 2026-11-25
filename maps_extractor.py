"""Async Playwright extractor for Google Maps place URLs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any, Callable

from playwright.async_api import BrowserContext, Page, async_playwright

from config import GOOGLE_CONSENT_COOKIES

logger = logging.getLogger(__name__)


@dataclass
class MapsPlaceResult:
    """Structured business fields extracted from a single Google Maps link."""

    row: int
    maps_url: str
    name: str | None = None
    category: str | None = None
    address: str | None = None
    website: str | None = None
    phone: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _clean_website_url(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    url = raw_url.strip()
    if url.startswith("https://www.google.com/url?") or url.startswith("http://www.google.com/url?"):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        target = qs.get("q") or qs.get("url")
        if target:
            url = target[0]
    return url or None


def _clean_phone(raw_phone: str | None) -> str | None:
    if not raw_phone:
        return None
    val = urllib.parse.unquote(raw_phone).strip()
    for prefix in ("phone:tel:", "tel:", "Phone:"):
        if val.lower().startswith(prefix.lower()):
            val = val[len(prefix):].strip()
    val = re.sub(r"[^\d+]", "", val)
    if val.startswith("00"):
        val = "+" + val[2:]
    return val or None


def _parse_preview_place_json(raw_text: str) -> dict[str, str | None]:
    """Parse Google Maps /maps/preview/place JSON payload (data[6])."""
    if not raw_text:
        return {}
    if raw_text.startswith(")]}'"):
        raw_text = raw_text[4:]
    try:
        data = json.loads(raw_text)
    except Exception:
        return {}
    if not isinstance(data, list) or len(data) <= 6 or not isinstance(data[6], list):
        return {}
    place = data[6]

    def _at(idx: int) -> Any:
        return place[idx] if idx < len(place) else None

    name = _clean_text(_at(11)) if isinstance(_at(11), str) else None

    category = None
    cats = _at(13)
    if isinstance(cats, list) and cats and isinstance(cats[0], str):
        category = _clean_text(cats[0])

    address = None
    for addr_idx in (39, 18):
        candidate = _at(addr_idx)
        if isinstance(candidate, str) and candidate.strip():
            address = _clean_text(candidate)
            break

    website = None
    web_info = _at(7)
    if isinstance(web_info, list) and web_info and isinstance(web_info[0], str):
        website = _clean_website_url(web_info[0])

    phone = None
    phone_info = _at(178)
    if isinstance(phone_info, list) and phone_info and isinstance(phone_info[0], list):
        entry = phone_info[0]
        if len(entry) > 3 and isinstance(entry[3], str):
            phone = _clean_phone(entry[3])
        elif entry and isinstance(entry[0], str):
            phone = _clean_phone(entry[0])

    return {
        "name": name,
        "category": category,
        "address": address,
        "website": website,
        "phone": phone,
    }


async def _extract_dom_fields(page: Page) -> dict[str, str | None]:
    """Read place details from the rendered Google Maps DOM as primary/fallback."""
    name = None
    h1 = page.locator("h1.DUwDvf").first
    if await h1.count():
        name = _clean_text(await h1.inner_text())

    category = None
    cat_btn = page.locator('button[jsaction*="category"]').first
    if await cat_btn.count():
        category = _clean_text(await cat_btn.inner_text())
    else:
        # Hotel pages display star/type in div.TIHn2 without a category button
        header = page.locator("div.TIHn2").first
        if await header.count():
            lines = [ln.strip() for ln in (await header.inner_text()).splitlines() if ln.strip()]
            for ln in reversed(lines):
                if any(k in ln.lower() for k in ("hotel", "hostel", "resort", "inn", "apartment", "lodging", "villa")):
                    category = _clean_text(ln.split("·")[0])
                    break

    address = None
    addr_btn = page.locator('button[data-item-id="address"]').first
    if await addr_btn.count():
        aria = await addr_btn.get_attribute("aria-label")
        if aria:
            address = _clean_text(re.sub(r"^Address:\s*", "", aria, flags=re.IGNORECASE))
        if not address:
            txt = await addr_btn.inner_text()
            # Strip material icon private-use glyph prefix if present
            txt = re.sub(r"^[\ue000-\uf8ff\s]+", "", txt)
            address = _clean_text(txt)

    website = None
    web_el = page.locator('a[data-item-id="authority"]').first
    if await web_el.count():
        website = _clean_website_url(await web_el.get_attribute("href"))

    phone = None
    phone_el = page.locator('button[data-item-id^="phone:tel:"]').first
    if await phone_el.count():
        phone = _clean_phone(await phone_el.get_attribute("data-item-id"))

    return {
        "name": name,
        "category": category,
        "address": address,
        "website": website,
        "phone": phone,
    }


async def _handle_consent_if_redirected(page: Page) -> None:
    """Click through Google consent page if EU cookie prompt ever appears."""
    if "consent.google.com" not in page.url:
        return
    for label in ("Accept all", "Reject all", "I agree"):
        btn = page.get_by_role("button", name=label)
        if await btn.count():
            await btn.first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            return


async def _scrape_single_place(page: Page, row: int, url: str) -> MapsPlaceResult:
    """Extract place fields for one row, retrying once on transient timeout."""
    last_err: str | None = None
    for attempt in range(2):
        preview_payload: dict[str, str | None] = {}
        try:
            # Clear prior place DOM/XHR state so a failed navigation never leaks stale data
            await page.goto("about:blank")

            async def _goto_and_capture() -> None:
                try:
                    async with page.expect_response(
                        lambda r: "/maps/preview/place" in r.url and r.status == 200,
                        timeout=15000,
                    ) as resp_info:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    resp = await resp_info.value
                    txt = await resp.text()
                    preview_payload.update(_parse_preview_place_json(txt))
                except Exception:
                    if page.url == "about:blank":
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            await _goto_and_capture()
            await _handle_consent_if_redirected(page)
            await page.wait_for_selector("h1.DUwDvf", timeout=15000)
            # Brief settle so address/website/phone buttons attach
            await page.wait_for_timeout(250)
            dom_payload = await _extract_dom_fields(page)

            name = preview_payload.get("name") or dom_payload.get("name")
            category = preview_payload.get("category") or dom_payload.get("category")
            address = dom_payload.get("address") or preview_payload.get("address")
            website = dom_payload.get("website") or preview_payload.get("website")
            phone = preview_payload.get("phone") or dom_payload.get("phone")

            if name:
                return MapsPlaceResult(
                    row=row,
                    maps_url=url,
                    name=name,
                    category=category,
                    address=address,
                    website=website,
                    phone=phone,
                )
            last_err = "Empty place name after page load"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            logger.debug("Maps attempt %d failed for row %d: %s", attempt + 1, row, last_err)
            await asyncio.sleep(0.8 * (attempt + 1))

    return MapsPlaceResult(row=row, maps_url=url, error=last_err)


async def extract_maps_batch(
    items: list[tuple[int, str]],
    concurrency: int = 6,
    on_result: Callable[[MapsPlaceResult], None] | None = None,
) -> dict[int, MapsPlaceResult]:
    """Run concurrent Playwright workers over (row, maps_url) items."""
    results: dict[int, MapsPlaceResult] = {}
    if not items:
        return results

    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)
    worker_count = max(1, min(concurrency, len(items)))
    for _ in range(worker_count):
        queue.put_nowait(None)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        await context.add_cookies(GOOGLE_CONSENT_COOKIES)

        async def _abort_heavy_assets(route) -> None:
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()

        async def _worker(worker_id: int) -> None:
            page = await context.new_page()
            await page.route("**/*", _abort_heavy_assets)
            try:
                while True:
                    task = await queue.get()
                    try:
                        if task is None:
                            return
                        row, url = task
                        res = await _scrape_single_place(page, row, url)
                        results[row] = res
                        if on_result is not None:
                            on_result(res)
                    finally:
                        queue.task_done()
            finally:
                await page.close()

        workers = [asyncio.create_task(_worker(i)) for i in range(worker_count)]
        await asyncio.gather(*workers)
        await context.close()
        await browser.close()

    return results
