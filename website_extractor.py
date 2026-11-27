"""Business website scraper for email addresses, phone numbers, and social links."""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import urllib.parse
import warnings
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import requests
import urllib3
from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, async_playwright

from config import (
    BROWSER_HEADERS,
    EMAIL_REGEX,
    IGNORED_EMAIL_DOMAINS,
    IGNORED_EMAIL_EXACT,
    IGNORED_EMAIL_PREFIXES,
    INVALID_EMAIL_EXTENSIONS,
    NOISE_SOCIAL_PATTERNS,
    SKIP_SCRAPE_DOMAINS,
    SOCIAL_DOMAINS,
    SOCIAL_URL_REGEX,
    SUBPAGE_KEYWORDS,
    VALID_LONG_TLDS,
)

warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)


@dataclass
class WebsiteResult:
    """Contact and social media details extracted from a business website."""

    row: int
    website: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    social_links: list[str] = field(default_factory=list)
    used_browser_fallback: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decode_cf_email(hex_str: str) -> str | None:
    """Decode Cloudflare email protection hex string."""
    try:
        raw = bytes.fromhex(hex_str.strip())
        key = raw[0]
        return "".join(chr(b ^ key) for b in raw[1:])
    except Exception:
        return None


def _clean_glued_email(email: str) -> str:
    """Strip uppercase text glued after common TLDs (e.g. foo@bar.comDATOS -> foo@bar.com)."""
    clean = email.strip().strip(".,;:)>\"'")
    m = re.match(
        r"^([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.(?:com|es|org|net|eu|info|biz|co|io))([A-Z][A-Za-z]+)$",
        clean,
    )
    if m:
        return m.group(1)
    return clean


def _is_valid_email(email: str) -> bool:
    low = email.strip().lower()
    if not low or "@" not in low or len(low) > 120:
        return False
    if low in IGNORED_EMAIL_EXACT:
        return False
    if any(low.startswith(p) for p in IGNORED_EMAIL_PREFIXES):
        return False
    if low.endswith(INVALID_EMAIL_EXTENSIONS):
        return False
    local, _, domain = low.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if local.startswith(("dpo.", "dpd.", "lopd.", "rgpd.")):
        return False
    if any(k in domain for k in ("casino", "poker", "togel", "gacor", "betting")):
        return False
    if re.fullmatch(r"x+", local):
        return False
    if any(domain == d or domain.endswith("." + d) for d in IGNORED_EMAIL_DOMAINS):
        return False
    tld = domain.rsplit(".", 1)[-1]
    if not tld.isalpha() or len(tld) < 2 or len(tld) > 12:
        return False
    if len(tld) >= 4 and tld not in VALID_LONG_TLDS:
        return False
    return True


def _rank_emails(emails: list[str], site_host: str) -> list[str]:
    """Deduplicate case-insensitively and rank domain-matched contact emails first."""
    base_host = site_host.lower().removeprefix("www.")
    seen: dict[str, str] = {}
    for email in emails:
        clean = _clean_glued_email(email)
        if not _is_valid_email(clean):
            continue
        key = clean.lower()
        if key not in seen:
            seen[key] = key

    # Drop any longer string glued onto an already-extracted valid email
    deduped = [
        e for e in seen.values()
        if not any(other != e and e.startswith(other) for other in seen.values())
    ]

    priority_prefixes = ("info@", "reservas@", "reserva@", "contacto@", "hola@", "contact@", "admin@", "booking@")
    gdpr_prefixes = ("dpd@", "dpo@", "protecciondatos@", "protecciondedatos@", "privacidad@", "privacy@", "datos@")

    def _score(e: str) -> tuple[int, int, str]:
        _, _, dom = e.partition("@")
        dom_match = 0 if (base_host and (dom == base_host or base_host in dom)) else 1
        if e.startswith(priority_prefixes):
            prefix_rank = 0
        elif e.startswith(gdpr_prefixes):
            prefix_rank = 2
        else:
            prefix_rank = 1
        return (dom_match, prefix_rank, e)

    return sorted(deduped, key=_score)


DUMMY_PHONE_SEQUENCES = {"123456789", "987654321", "012345678", "1234567890"}


def normalize_phone(raw: str | None) -> str | None:
    """Normalize phone strings into clean international digits (e.g. +34965204233)."""
    if not raw:
        return None
    s = urllib.parse.unquote(raw).strip()
    s = re.sub(r"^(tel:|phone:|tlf:|tfno:)", "", s, flags=re.IGNORECASE).strip()
    digits = re.sub(r"[^\d+]", "", s)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if "+" in digits[1:]:
        digits = digits[0] + digits[1:].replace("+", "")
    pure = digits.lstrip("+")
    if len(pure) < 9 or len(pure) > 15:
        return None
    # Reject invalid leading zero country codes (e.g. +04067529214) and dummy digits
    if digits.startswith("+0") or (not digits.startswith("+") and pure.startswith("0")):
        return None
    if pure in DUMMY_PHONE_SEQUENCES or re.search(r"(\d)\1{5,}", pure):
        return None
    # Handle 9-digit Spanish local numbers cleanly
    if not digits.startswith("+") and len(pure) == 9 and pure[0] in ("6", "7", "8", "9"):
        digits = "+34" + pure
    elif not digits.startswith("+") and pure.startswith("34") and len(pure) == 11:
        digits = "+" + pure
    elif not digits.startswith("+"):
        digits = "+" + pure
    return digits


def _is_spanish_local_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"\+34[6789]\d{8}", phone))


def merge_phones(*phone_groups: list[str] | str | None) -> list[str]:
    """Combine and deduplicate phone numbers preserving order."""
    seen_digits: set[str] = set()
    merged: list[str] = []
    for group in phone_groups:
        items = [group] if isinstance(group, str) else (group or [])
        for raw in items:
            norm = normalize_phone(raw)
            if not norm:
                continue
            key = norm.lstrip("+")
            if key not in seen_digits:
                seen_digits.add(key)
                merged.append(norm)
    return merged


def merge_phones_smart(
    maps_phone: str | None,
    web_phones: list[str] | None,
    address: str | None = None,
) -> list[str]:
    """Prioritize verified Google Maps phone and filter website extras against spam/foreign noise."""
    is_spain = bool(
        (address and "spain" in address.lower())
        or (maps_phone and normalize_phone(maps_phone) and normalize_phone(maps_phone).startswith("+34"))
    )
    filtered_web: list[str] = []
    for raw in web_phones or []:
        norm = normalize_phone(raw)
        if not norm:
            continue
        # When operating on Spanish businesses, only accept valid Spanish numbers from websites
        if is_spain and not _is_spanish_local_phone(norm):
            continue
        filtered_web.append(norm)
    return merge_phones(maps_phone, filtered_web)


SPAM_SOCIAL_KEYWORDS = ("slot", "casino", "poker", "togel", "gacor", "judi", "cuan", "betting")


def normalize_social_url(raw_url: str, allow_groups: bool = False) -> str | None:
    """Normalize and filter social media URLs to genuine business profile/page links."""
    if not raw_url:
        return None
    url = html_lib.unescape(raw_url).strip()
    url = re.sub(r"(?:&quot|&amp|[\\/,;\"'])+$", "", url)
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = parsed.netloc.lower()
    if not any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS):
        return None
    low = url.lower()
    if any(pat in low for pat in NOISE_SOCIAL_PATTERNS):
        return None

    path = parsed.path.rstrip("/")
    path_low = path.lower()
    # Reject bare homepages, Facebook pixel endpoint (/tr), and injected spam groups
    if not path or path_low in ("/home", "/es", "/en", "/tr"):
        return None
    if "/groups/" in path_low and not allow_groups:
        return None
    decoded_path = urllib.parse.unquote(path)
    if any(k in decoded_path.lower() for k in SPAM_SOCIAL_KEYWORDS):
        return None
    # Reject non-Latin script spam paths (e.g. Thai/Arabic/CJK gambling injections)
    if not allow_groups and re.search(r"[^\x00-\x7F\u00C0-\u024F]", decoded_path):
        return None

    # Keep identifying query parameters (e.g. Facebook profile.php?id=...)
    qs = urllib.parse.parse_qs(parsed.query)
    keep_qs = {}
    if "id" in qs and "profile.php" in path_low:
        keep_qs["id"] = qs["id"][0]
    clean_query = urllib.parse.urlencode(keep_qs)

    clean_host = host
    if clean_host.startswith(("m.", "es-es.", "es.")):
        clean_host = "www." + clean_host.split(".", 1)[1]
    elif clean_host in SOCIAL_DOMAINS and clean_host != "x.com":
        clean_host = "www." + clean_host
    if clean_host == "www.fb.com":
        clean_host = "www.facebook.com"

    return urllib.parse.urlunparse(("https", clean_host, path, "", clean_query, ""))


def _extract_from_html(html: str, page_url: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """Extract (emails, phones, social_links, candidate_subpages) from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    emails: list[str] = []
    phones: list[str] = []
    socials: list[str] = []
    subpages: list[str] = []

    base_host = urllib.parse.urlparse(page_url).netloc.lower().removeprefix("www.")

    # Decode Cloudflare email spans
    for cf in soup.select("[data-cfemail]"):
        decoded = _decode_cf_email(cf.get("data-cfemail", ""))
        if decoded:
            emails.append(decoded)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            raw_mail = urllib.parse.unquote(href[7:].split("?")[0]).strip()
            emails.append(raw_mail)
        elif low.startswith("tel:"):
            phones.append(href[4:].split("?")[0])
        elif "wa.me/" in low or "api.whatsapp.com/send" in low or "whatsapp.com/send" in low:
            try:
                wa_parsed = urllib.parse.urlparse(href)
            except ValueError:
                continue
            qs = urllib.parse.parse_qs(wa_parsed.query)
            if "phone" in qs and qs["phone"]:
                phones.append(qs["phone"][0])
            else:
                m_wa = re.search(r"wa\.me/(\+?\d{9,15})", href)
                if m_wa:
                    phones.append(m_wa.group(1))
        else:
            try:
                abs_url = urllib.parse.urljoin(page_url, href)
                parsed = urllib.parse.urlparse(abs_url)
            except ValueError:
                continue
            norm_soc = normalize_social_url(abs_url)
            if norm_soc:
                socials.append(norm_soc)
                continue
            if parsed.scheme in ("http", "https"):
                cand_host = parsed.netloc.lower().removeprefix("www.")
                if cand_host == base_host and not abs_url.lower().endswith(INVALID_EMAIL_EXTENSIONS):
                    path_low = parsed.path.lower()
                    if any(k in path_low for k in SUBPAGE_KEYWORDS):
                        clean_sub = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))
                        if clean_sub.rstrip("/") != page_url.rstrip("/"):
                            subpages.append(clean_sub)

    # Visible text scan avoids glued DOM tags like '.comDATOS'
    visible_text = soup.get_text(" ", strip=True)
    emails.extend(EMAIL_REGEX.findall(visible_text))
    # Unescape HTML entities & JSON-LD escaped slashes before regex scanning
    unescaped_html = html_lib.unescape(html).replace("\\/", "/")
    emails.extend(EMAIL_REGEX.findall(unescaped_html))
    for match in SOCIAL_URL_REGEX.findall(unescaped_html):
        norm_soc = normalize_social_url(match)
        if norm_soc:
            socials.append(norm_soc)

    # Extract explicit labeled/prefixed phone numbers in visible text
    phone_pattern = re.compile(
        r"(?:\+34|00\s?34)\s*[6789](?:[\s.\-]?\d){8}\b"
        r"|(?:tel(?:éfono|efono)?|tfno|tlf|llámanos|phone|reservas|whatsapp)[\s:.\-]*([6789](?:[\s.\-]?\d){8})\b",
        re.IGNORECASE,
    )
    for m in phone_pattern.finditer(visible_text):
        candidate = m.group(1) or m.group(0)
        phones.append(candidate)

    return emails, phones, socials, subpages


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower().rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _prioritize_subpages(subpages: list[str]) -> list[str]:
    def _rank(u: str) -> tuple[int, str]:
        low = u.lower()
        if any(k in low for k in ("contact", "contacto", "contacta")):
            return (0, u)
        if any(k in low for k in ("aviso-legal", "aviso", "privacidad", "privacy")):
            return (1, u)
        if any(k in low for k in ("reserva", "reservas")):
            return (2, u)
        return (3, u)

    return sorted(_dedupe_preserve(subpages), key=_rank)[:5]


def _fetch_html_sync(session: requests.Session, url: str) -> tuple[str | None, str, int | None, str | None]:
    """Fetch HTML over HTTP(S), retrying transient 5xx/connection drops and broken SSL certs."""
    import time

    for attempt in range(2):
        verify_ssl = attempt == 0
        try:
            resp = session.get(url, timeout=12, allow_redirects=True, verify=verify_ssl)
            ctype = resp.headers.get("content-type", "").lower()
            if resp.status_code in (502, 503, 504) and attempt == 0:
                time.sleep(1.2)
                continue
            if resp.status_code >= 400:
                return None, resp.url, resp.status_code, f"HTTP {resp.status_code}"
            if ctype and "html" not in ctype and "text" not in ctype and "xml" not in ctype:
                return None, resp.url, resp.status_code, f"Non-HTML content-type: {ctype}"
            resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
            return resp.text, resp.url, resp.status_code, None
        except requests.exceptions.SSLError as exc:
            if attempt == 0:
                continue
            return None, url, None, f"SSLError: {exc}"
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == 0:
                time.sleep(1.0)
                continue
            return None, url, None, f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            return None, url, None, f"{type(exc).__name__}: {exc}"
    return None, url, None, "Unknown HTTP error"


def _scrape_website_http_sync(row: int, website_url: str) -> tuple[WebsiteResult, bool]:
    """Scrape homepage + up to 5 contact/legal subpages via HTTP. Returns (result, needs_browser)."""
    norm_self_social = normalize_social_url(website_url, allow_groups=True)
    if norm_self_social:
        # Maps website itself points to Facebook/Instagram/etc.
        return WebsiteResult(row=row, website=website_url, social_links=[norm_self_social]), False

    try:
        site_netloc = urllib.parse.urlparse(website_url).netloc.lower()
    except ValueError:
        site_netloc = ""
    if any(d in site_netloc for d in SKIP_SCRAPE_DOMAINS):
        # Skip third-party OTA/delivery directories so OTA contacts aren't attributed to the venue
        return WebsiteResult(row=row, website=website_url), False

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    html, final_url, status, err = _fetch_html_sync(session, website_url)
    if html is None:
        needs_browser = status in (403, 429, 503) or (err is not None and "SSLError" not in err)
        return WebsiteResult(row=row, website=website_url, error=err), needs_browser

    all_emails, all_phones, all_socials, subpages = _extract_from_html(html, final_url)
    site_host = urllib.parse.urlparse(final_url).netloc

    for sub_url in _prioritize_subpages(subpages):
        sub_html, sub_final, _, _ = _fetch_html_sync(session, sub_url)
        if sub_html:
            e2, p2, s2, _ = _extract_from_html(sub_html, sub_final)
            all_emails.extend(e2)
            all_phones.extend(p2)
            all_socials.extend(s2)

    ranked_emails = _rank_emails(all_emails, site_host)
    merged_phones = merge_phones(all_phones)
    unique_socials = _dedupe_preserve(all_socials)

    soup = BeautifulSoup(html, "html.parser")
    visible_len = len(soup.get_text(" ", strip=True))
    # Trigger browser fallback if page looks like an unrendered JS shell with no contacts
    needs_browser = (visible_len < 250 and not ranked_emails and not unique_socials)

    return (
        WebsiteResult(
            row=row,
            website=website_url,
            emails=ranked_emails,
            phones=merged_phones,
            social_links=unique_socials,
        ),
        needs_browser,
    )


async def _safe_content(page) -> str:
    """Read page HTML safely even if a delayed client redirect occurs."""
    for _ in range(2):
        try:
            return await page.content()
        except Exception as exc:
            if "navigating" in str(exc).lower():
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                await page.wait_for_timeout(800)
                continue
            raise
    return await page.content()


async def _safe_goto(page, url: str, timeout_ms: int = 20000) -> None:
    """Navigate safely, tolerating immediate JS/meta client redirects."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        if "interrupted by another navigation" in str(exc):
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        else:
            raise


async def _scrape_websites_playwright(
    targets: list[tuple[int, str]],
    concurrency: int = 4,
) -> dict[int, WebsiteResult]:
    """Render JS-heavy or bot-protected websites via headless Chromium fallback."""
    out: dict[int, WebsiteResult] = {}
    if not targets:
        return out

    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
    for t in targets:
        queue.put_nowait(t)
    worker_count = max(1, min(concurrency, len(targets)))
    for _ in range(worker_count):
        queue.put_nowait(None)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--disable-http2"])
        context: BrowserContext = await browser.new_context(
            locale="en-US",
            user_agent=BROWSER_HEADERS["User-Agent"],
            ignore_https_errors=True,
        )

        async def _abort_assets(route) -> None:
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()

        async def _worker() -> None:
            page = await context.new_page()
            await page.route("**/*", _abort_assets)
            try:
                while True:
                    item = await queue.get()
                    try:
                        if item is None:
                            return
                        row, url = item
                        try:
                            await _safe_goto(page, url, timeout_ms=20000)
                            await page.wait_for_timeout(1500)
                            html = await _safe_content(page)
                            final_url = page.url
                            e, p, s, subpages = _extract_from_html(html, final_url)
                            for sub_url in _prioritize_subpages(subpages)[:3]:
                                try:
                                    await _safe_goto(page, sub_url, timeout_ms=15000)
                                    await page.wait_for_timeout(800)
                                    sub_html = await _safe_content(page)
                                    e2, p2, s2, _ = _extract_from_html(sub_html, page.url)
                                    e.extend(e2)
                                    p.extend(p2)
                                    s.extend(s2)
                                except Exception:
                                    pass
                            site_host = urllib.parse.urlparse(final_url).netloc
                            out[row] = WebsiteResult(
                                row=row,
                                website=url,
                                emails=_rank_emails(e, site_host),
                                phones=merge_phones(p),
                                social_links=_dedupe_preserve(s),
                                used_browser_fallback=True,
                            )
                        except Exception as exc:
                            out[row] = WebsiteResult(
                                row=row,
                                website=url,
                                used_browser_fallback=True,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                    finally:
                        queue.task_done()
            finally:
                await page.close()

        await asyncio.gather(*[_worker() for _ in range(worker_count)])
        await context.close()
        await browser.close()

    return out


async def extract_websites_batch(
    items: list[tuple[int, str]],
    concurrency: int = 12,
    use_browser_fallback: bool = True,
    on_result: Callable[[WebsiteResult], None] | None = None,
) -> dict[int, WebsiteResult]:
    """Scrape all website URLs concurrently via HTTP, with optional Playwright fallback."""
    results: dict[int, WebsiteResult] = {}
    if not items:
        return results

    sem = asyncio.Semaphore(max(1, concurrency))
    fallback_queue: list[tuple[int, str]] = []

    async def _run_http(row: int, url: str) -> None:
        async with sem:
            try:
                res, needs_browser = await asyncio.to_thread(_scrape_website_http_sync, row, url)
            except Exception as exc:
                res = WebsiteResult(row=row, website=url, error=f"{type(exc).__name__}: {exc}")
                needs_browser = False
            if needs_browser and use_browser_fallback:
                fallback_queue.append((row, url))
            else:
                results[row] = res
                if on_result is not None:
                    on_result(res)

    await asyncio.gather(*[_run_http(r, u) for r, u in items])

    if fallback_queue:
        logger.info("Running Playwright fallback for %d JS/protected websites", len(fallback_queue))
        fb_results = await _scrape_websites_playwright(fallback_queue, concurrency=min(4, len(fallback_queue)))
        for row, fb_res in fb_results.items():
            results[row] = fb_res
            if on_result is not None:
                on_result(fb_res)

    return results
