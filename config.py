"""Configuration, constants, and Google Sheet download helpers."""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1xQ3C2Ms1InD6h6YGO9l8ntMiy8q9pJwkrg2frX52vXw/edit?gid=0#gid=0"
)

# 1-based Excel column indices matching the target sheet layout
COL_LEAD_ID = 1        # A: Lead #
COL_NAME = 2           # B: Business Name
COL_CATEGORY = 3       # C: Category
COL_MAPS_URL = 4       # D: Google Maps Link
COL_ADDRESS = 5        # E: Full Address
COL_WEBSITE = 6        # F: Website
COL_EMAIL = 7          # G: Email
COL_PHONE = 8          # H: Phone Number
COL_SOCIAL = 12        # L: Social Media Link

TARGET_COLUMNS = (
    COL_NAME,
    COL_CATEGORY,
    COL_ADDRESS,
    COL_WEBSITE,
    COL_EMAIL,
    COL_PHONE,
    COL_SOCIAL,
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
}

GOOGLE_CONSENT_COOKIES = [
    {
        "name": "CONSENT",
        "value": "YES+cb.20210328-17-p0.en+FX+667",
        "domain": ".google.com",
        "path": "/",
    },
    {
        "name": "SOCS",
        "value": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiA_LyaBg",
        "domain": ".google.com",
        "path": "/",
    },
]

EMAIL_REGEX = re.compile(
    r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,12})(?![A-Za-z0-9._%+\-])"
)

INVALID_EMAIL_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".avif",
    ".mp4", ".mp3", ".zip", ".json",
)

VALID_LONG_TLDS = {
    "aero", "agency", "bar", "biz", "booking", "cafe", "cat", "catering",
    "center", "click", "cloud", "club", "community", "company", "consulting",
    "coop", "design", "digital", "email", "estate", "events", "galicia",
    "group", "guide", "holiday", "hotel", "house", "info", "jobs", "madrid",
    "marketing", "media", "mobi", "museum", "name", "online", "partners",
    "place", "pub", "restaurant", "services", "shop", "solutions", "space",
    "store", "studio", "today", "tours", "travel", "valencia", "world",
    "barcelona", "academy", "eus", "wine", "kitchen", "food",
}

IGNORED_EMAIL_DOMAINS = (
    "example.com", "example.org", "domain.com", "email.com", "test.com",
    "ejemplo.com", "ejemplo.es", "above.com", "sedo.com", "bodis.com",
    "afternic.com", "hugedomains.com", "dan.com", "domainprivacy.com",
    "greensock.com", "centralapp.com", "stride-site.com",
    "webador.com", "webador.es", "webador.fr", "conversia.es",
    "wixpress.com", "sentry.io", "sentry-next.wixpress.com", "ingest.sentry.io",
    "wordpress.org", "wordpress.com", "w3.org", "schema.org",
    "googleapis.com", "gstatic.com", "googleusercontent.com",
    "squarespace.com", "cloudflare.com", "jquery.com", "bootstrapcdn.com",
    "fontawesome.com", "cookieyes.com", "complianz.io", "iubenda.com",
    "usercentrics.eu", "usercentrics.com", "cookiebot.com", "onetrust.com",
    "godaddy.com", "secureserver.net", "mailchimp.com", "hubspot.com",
    "googletagmanager.com", "google-analytics.com",
)

IGNORED_EMAIL_EXACT = {
    "support@parallevarya.com",
}

IGNORED_EMAIL_PREFIXES = (
    "noreply@", "no-reply@", "mailer-daemon@", "donotreply@", "do-not-reply@",
    "u003e", "protection@", "abuse@", "postmaster@",
)

SOCIAL_DOMAINS = (
    "instagram.com",
    "facebook.com",
    "fb.com",
    "tiktok.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
)

SOCIAL_URL_REGEX = re.compile(
    r"https?://(?:www\.|m\.|es-es\.|es\.)?"
    r"(?:instagram\.com|facebook\.com|fb\.com|tiktok\.com|linkedin\.com|twitter\.com|x\.com|youtube\.com)"
    r"[^\s\"'<>\\)]*",
    re.IGNORECASE,
)

NOISE_SOCIAL_PATTERNS = (
    "/sharer", "/share.php", "/share?", "/sharearticle", "/dialog/", "/plugins/",
    "/intent/", "/tr?", "/tr/", "/about/privacy", "/policies",
    "/privacy", "/terms", "/help/", "/login", "/signup", "/accounts/",
    "/p/", "/reel/", "/reels/", "/stories/", "/tv/", "/explore/", "/static/",
    "/embed/", "/watch?", "/iframe_api", "/s/player", "/2008/fbml",
    "twitter.com/i/", "x.com/i/", "youtu.be/",
    "developers.facebook.com", "business.facebook.com", "l.facebook.com", "lm.facebook.com",
    "facebook.com/wix", "instagram.com/wix", "twitter.com/wix",
    "facebook.com/wordpress", "facebook.com/squarespace",
)

SUBPAGE_KEYWORDS = (
    "contact", "contacto", "contacta", "contactar",
    "aviso-legal", "aviso", "legal",
    "privacidad", "privacy", "politica-de-privacidad", "politica-privacidad",
    "reserva", "reservas", "about", "nosotros", "quienes-somos",
)


def extract_sheet_id_and_gid(sheet_url_or_id: str) -> tuple[str, str | None]:
    """Parse a Google Sheets URL or raw spreadsheet ID into (sheet_id, gid)."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9\-_]+)", sheet_url_or_id)
    sheet_id = m.group(1) if m else sheet_url_or_id.strip()
    parsed = urllib.parse.urlparse(sheet_url_or_id)
    gid = None
    for part in (parsed.query, parsed.fragment):
        qs = urllib.parse.parse_qs(part)
        if "gid" in qs and qs["gid"]:
            gid = qs["gid"][0]
            break
    return sheet_id, gid


def download_google_sheet(sheet_url_or_id: str, dest_path: Path) -> Path:
    """Download a publicly accessible Google Sheet as an .xlsx file."""
    sheet_id, _ = extract_sheet_id_and_gid(sheet_url_or_id)
    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    req = urllib.request.Request(export_url, headers={"User-Agent": BROWSER_HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if not data.startswith(b"PK"):
        raise RuntimeError(
            "Google Sheet did not return a valid .xlsx archive. "
            "Ensure the sheet is shared publicly ('Anyone with the link can view') "
            "or pass a local .xlsx file via --input."
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(data)
    return dest_path
