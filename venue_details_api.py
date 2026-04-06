# ═══════════════════════════════════════════════════════════════════════════════
# Venue Details REST API
# Self-contained Flask API wrapping get_venue_details() with Selenium scraping
# ═══════════════════════════════════════════════════════════════════════════════
#
# Requirements: flask, flask-cors, requests, beautifulsoup4, selenium, webdriver-manager
#
# requirements.txt:
# flask==3.0.0
# flask-cors==4.0.0
# requests==2.31.0
# beautifulsoup4==4.12.3
# selenium==4.25.0
# webdriver-manager==4.0.2
#
# To run locally:
#   pip install -r requirements.txt
#   python venue_details_api.py
#
# Dockerfile:
# FROM python:3.11-slim
# RUN apt-get update && apt-get install -y chromium chromium-driver && rm -rf /var/lib/apt/lists/*
# ENV CHROME_BIN=/usr/bin/chromium
# WORKDIR /app
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt
# COPY . .
# EXPOSE 5000
# CMD ["python", "venue_details_api.py"]
#
# API Usage:
#   POST /api/venue-details
#   Body: {"venue_name": "One Badminton Academy"}
#   or:   {"slug": "one-badminton-academy"}          (AFA direct)
#   or:   {"venue_id": "ISALSTBB"}                    (SWP direct)
#
#   GET /health
# ═══════════════════════════════════════════════════════════════════════════════

import json
import os
import re
import time
from typing import Dict, List, Optional, Any
from urllib.parse import quote, urljoin

import requests as http_requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import StaleElementReferenceException

from flask import Flask, request, jsonify
from flask_cors import CORS


# ═══════════════════════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════════════════════

app = Flask(__name__)
CORS(app)


# ═══════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════

# ── AFA ──
AFA_VENUE_LIST_API  = "https://community.afa-sports.com/directory/v1/sports_complex"
AFA_VENUE_PAGE_BASE = "https://playsportstogether.com/complex"
AFA_PAGE_SIZE       = 8

AFA_HEADERS = {
    "accept":          "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin":          "https://playsportstogether.com",
    "referer":         "https://playsportstogether.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}

# ── SWP ──
SWP_VENUE_LIST_API = "https://solemas.com/swp_rest/sportsweplay_com_my/booking/venue_list/venue_list"
SWP_HEADERS_JSON = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept":       "application/json",
    "Referer":      "https://www.sportsweplay.com.my/",
    "Origin":       "https://www.sportsweplay.com.my",
    "Content-Type": "application/json",
}

SWP_SPORTS_PATTERN = re.compile(
    r"badminton|pickleball|tennis|futsal|swimming|paddle|squash|volleyball|basketball|court",
    re.I,
)

# Paths to JSON files (expected in same directory as this script)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SWP_VENUES_JSON = os.path.join(SCRIPT_DIR, "swp_venues_all.json")
AFA_VENUES_JSON = os.path.join(SCRIPT_DIR, "afa_venues_all.json")

# Global in-memory venue data
swp_venues: Dict[str, Dict] = {}
afa_venues: Dict[str, Dict] = {}


# ═══════════════════════════════════════════════════════════
# HELPERS (inlined from helpers.py)
# ═══════════════════════════════════════════════════════════

def load_venue_data():
    """Load SWP and AFA venue data into memory."""
    global swp_venues, afa_venues
    if os.path.exists(SWP_VENUES_JSON):
        with open(SWP_VENUES_JSON, 'r', encoding='utf-8') as f:
            swp_data = json.load(f)
            # swp_venues = {venue['venueName']: venue for venue in swp_data}
            swp_venues = {venue['name']: venue for venue in swp_data}
        print(f"[Startup] Loaded {len(swp_venues)} SWP venues")
    else:
        print(f"[Startup] WARNING: {SWP_VENUES_JSON} not found")

    if os.path.exists(AFA_VENUES_JSON):
        with open(AFA_VENUES_JSON, 'r', encoding='utf-8') as f:
            afa_data = json.load(f)
            afa_venues = {venue['name']: venue for venue in afa_data}
        print(f"[Startup] Loaded {len(afa_venues)} AFA venues")
    else:
        print(f"[Startup] WARNING: {AFA_VENUES_JSON} not found")


def lookup_venue(venue_name: str) -> Optional[Dict[str, Any]]:
    """Lookup venue by name in SWP or AFA data.
    Returns {'source': 'swp' or 'afa', 'id': ..., 'slug': ..., 'data': ...} or None.
    """
    # Check SWP first
    if venue_name in swp_venues:
        venue = swp_venues[venue_name]
        return {
            'source': 'swp',
            'id': venue['id'],
            'slug': venue['id'],  # SWP uses id as slug
            'data': venue
        }
    # Check AFA
    if venue_name in afa_venues:
        venue = afa_venues[venue_name]
        return {
            'source': 'afa',
            'id': venue['id'],
            'slug': venue['slug'],
            'data': venue
        }

    # Fuzzy match: check if venue_name is a substring of any known venue name
    name_lower = venue_name.strip().lower()
    for vname, venue in swp_venues.items():
        if name_lower in vname.lower() or vname.lower() in name_lower:
            return {
                'source': 'swp',
                'id': venue['id'],
                'slug': venue['id'],
                'data': venue
            }
    for vname, venue in afa_venues.items():
        if name_lower in vname.lower() or vname.lower() in name_lower:
            return {
                'source': 'afa',
                'id': venue['id'],
                'slug': venue['slug'],
                'data': venue
            }

    return None


# ═══════════════════════════════════════════════════════════
# SELENIUM DRIVER FACTORY
# ═══════════════════════════════════════════════════════════

def _make_driver(headless: bool = True) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    chrome_bin = os.environ.get("CHROME_BIN")
    if chrome_bin:
        options.binary_location = chrome_bin

    # Use system chromedriver instead of downloading
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
    return webdriver.Chrome(
        service=Service(chromedriver_path),
        options=options,
    )


# ═══════════════════════════════════════════════════════════
# AFA — API + Scrape
# ═══════════════════════════════════════════════════════════

def _afa_get_api_data(slug: str) -> dict:
    url = f"{AFA_VENUE_LIST_API}/{slug}"
    print(f"  [AFA·API] GET {url}")
    resp = http_requests.get(url, headers=AFA_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _afa_scrape_sections(slug: str, headless: bool) -> dict:
    """
    Opens the AFA venue page in a browser and clicks through each accordion
    to extract text sections (pricing, opening hours, amenities, policy, etc.).
    Returns { section_key: raw_text }.
    """
    url      = f"{AFA_VENUE_PAGE_BASE}/{slug}"
    driver   = _make_driver(headless)
    sections = {}

    print(f"  [AFA·Browser] Scraping {url}")
    try:
        driver.get(url)
        time.sleep(3)

        for _ in range(4):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(0.5)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)

        accordion_items = driver.find_elements(By.CSS_SELECTOR, "div.accordion-item")
        print(f"  [AFA·Browser] Found {len(accordion_items)} accordion(s)")

        for i, item in enumerate(accordion_items):
            try:
                btn   = item.find_element(By.CSS_SELECTOR, "h2 button.accordion-button")
                label = driver.execute_script("return arguments[0].innerText;", btn).strip()
                label = re.sub(r'[\u2039\u203a\u2018\u2019\u25b2\u25bc\ue5ce\ue5cf\n]+', '', label).strip()
                if not label:
                    label = f"section_{i + 1}"
                key = re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')
                print(f"    [{i+1}/{len(accordion_items)}] '{label}'")

                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                time.sleep(0.4)
                try:
                    btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", btn)
                time.sleep(1.5)

                content = ""
                for selector in ["div.accordion-body", "div.accordion-collapse"]:
                    try:
                        el      = item.find_element(By.CSS_SELECTOR, selector)
                        content = driver.execute_script("return arguments[0].innerText;", el).strip()
                        if content:
                            break
                    except Exception:
                        pass

                if not content:
                    try:
                        content = driver.execute_script(
                            "var kids = arguments[0].querySelectorAll('*');"
                            "for (var k of kids) {"
                            "  var t = k.innerText ? k.innerText.trim() : '';"
                            "  if (t.length > 30) return t;"
                            "}"
                            "return '';",
                            item,
                        ).strip()
                    except Exception:
                        pass

                sections[key] = content
                print(f"       → {len(content)} chars")

            except StaleElementReferenceException:
                print(f"    [!] Stale element on accordion {i+1}, skipping")
                continue
            except Exception as e:
                print(f"    [!] Error on accordion {i+1}: {e}")
                continue

    except Exception as e:
        print(f"  [AFA·Browser] Fatal: {e}")
    finally:
        driver.quit()

    return sections


def _afa_get_venue_details(slug: str, headless: bool) -> Optional[dict]:
    """
    Called when source='afa' is known from cache.
    Hits AFA REST API directly with slug + scrapes accordion sections.
    No search required.
    """
    print(f"\n[AFA] Fetching details — slug='{slug}'")
    try:
        api_data = _afa_get_api_data(slug)
    except Exception as e:
        print(f"  [AFA·API] Failed: {e}")
        return None

    inner = api_data.get("data", {})
    if not inner or not inner.get("id"):
        print(f"  [AFA·API] Empty response for slug '{slug}'.")
        return None

    sections = _afa_scrape_sections(slug, headless)
    return _map_from_afa(api_data, sections)


def _afa_fallback_search(venue_name: str, headless: bool) -> Optional[dict]:
    """
    Called only when venue is NOT in cache.
    Searches AFA listing API by name → finds slug → fetches full details.
    """
    print(f"\n[AFA·Fallback] Searching for '{venue_name}' ...")
    name_lower = venue_name.strip().lower()
    start      = 0

    while True:
        url = (
            f"{AFA_VENUE_LIST_API}"
            f"?city=&state=&start={start}&length={AFA_PAGE_SIZE}"
            f"&search={quote(venue_name)}&country=Malaysia&category_id="
        )
        try:
            resp    = http_requests.get(url, headers=AFA_HEADERS, timeout=10)
            data    = resp.json()
            records = data.get("data", [])
            total   = data.get("recordsTotal", 0)
            if isinstance(records, dict):
                records = records.get("sports_complexes", [])
        except Exception as e:
            print(f"  [AFA·Fallback] Request failed: {e}")
            return None

        for v in records:
            vname = (v.get("name") or v.get("title") or "").strip()
            slug  = v.get("slug", "").strip()
            if name_lower in vname.lower() or vname.lower() in name_lower:
                print(f"  [AFA·Fallback] Found slug='{slug}'")
                return _afa_get_venue_details(slug, headless)

        start += AFA_PAGE_SIZE
        if start >= total or not records:
            break

    print(f"  [AFA·Fallback] Not found.")
    return None


# ═══════════════════════════════════════════════════════════
# SWP — Scrape
# ═══════════════════════════════════════════════════════════

def _swp_get_panel_text(driver, btn) -> str:
    try:
        text = driver.execute_script(
            "var el = arguments[0].nextElementSibling;"
            "return el ? el.innerText.trim() : '';",
            btn,
        )
        if text:
            return text
    except Exception:
        pass

    try:
        panel = btn.find_element(By.XPATH, "./following-sibling::div[1]")
        text  = driver.execute_script("return arguments[0].innerText;", panel).strip()
        if text:
            return text
    except Exception:
        pass

    return ""


def _swp_image_priority(url: str) -> int:
    if "w=3840" in url: return 2
    if "w=1920" in url: return 1
    return 0


def _swp_extract_venue_code(url: str) -> Optional[str]:
    m = re.search(r'court_institution(?:%2F|/)([A-Z0-9]{6,12})', url, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r'/([A-Z0-9]{6,12})-', url)
    return m.group(1).upper() if m else None


def _swp_parse_venue_page(soup: BeautifulSoup, base_url: str) -> dict:
    """
    Extracts structured fields from the SWP venue page HTML.
    NOTE: booking_url is intentionally NOT extracted here —
          it is passed in from the venue list cache which holds
          the authoritative bookingLink from the SWP API.
    """
    data = {
        "venue_name":         None,
        "address":            None,
        "phone":              None,
        "whatsapp":           None,
        "social_links":       {},
        "main_image_url":     None,
        "gallery_image_urls": [],
        "sports_types":       [],
        "rating":             None,
        "review_count":       None,
    }

    # Name — first h1 or h2
    for tag in soup.find_all(['h1', 'h2']):
        txt = tag.get_text(strip=True)
        if len(txt) > 5:
            data["venue_name"] = txt
            break

    # Address — element containing a postcode
    for elem in soup.find_all(string=True):
        t = elem.strip()
        if re.search(r'\b\d{5}\b', t) and len(t) > 20:
            parent = elem.find_parent(['p', 'div', 'span', 'address'])
            if parent:
                addr = " ".join(parent.stripped_strings).strip()
                if len(addr) > 20:
                    data["address"] = addr
                    break

    # Phone — flex contact container or tel: links
    contact_containers = soup.find_all("div", class_=lambda c: c and
        all(k in c for k in ["flex", "flex-wrap", "items-center", "gap-4", "text-gray-600", "text-sm"]))
    for container in contact_containers:
        matches = re.findall(r'(?:\+60|01)[0-9\s\-]{8,12}', container.get_text(separator=" ", strip=True))
        for match in matches:
            cleaned = re.sub(r'[\s\-]', '', match)
            if len(cleaned) >= 10:
                data["phone"] = cleaned
                break
        if data["phone"]:
            break

    if not data["phone"]:
        for a in soup.find_all("a", href=re.compile(r'^tel:')):
            data["phone"] = re.sub(r'[\s\-]', '', a["href"].replace("tel:", ""))
            break

    # Social / WhatsApp
    for a in soup.find_all("a", href=True):
        href       = a["href"]
        href_lower = href.lower()
        if not data["whatsapp"] and ("wa.me" in href_lower or "whatsapp" in href_lower):
            data["whatsapp"] = href
        if "facebook" in href_lower and "facebook" not in data["social_links"]:
            data["social_links"]["facebook"] = href
        if "instagram" in href_lower and "instagram" not in data["social_links"]:
            data["social_links"]["instagram"] = href

    # Rating
    rating_containers = soup.find_all("div", class_=lambda v: v and
        all(k in v for k in ["bg-white", "rounded-lg", "shadow-soft", "p-4"]))
    for container in rating_containers:
        for t in container.find_all(string=True):
            if re.match(r'^\d(\.\d)?$', t.strip()):
                try:
                    val = float(t.strip())
                    if 0 <= val <= 5.0:
                        data["rating"] = val
                        break
                except ValueError:
                    pass
        if data["rating"] is not None:
            break

    # Review count
    for elem in soup.find_all(class_=lambda v: v and all(k in v for k in ["text-xs", "text-gray-600", "mt-1"])):
        m = re.search(r'(\d+)\s*(review|reviews|ulasan|penilaian|rating|ratings|\()', elem.get_text(strip=True), re.I)
        if m:
            try:
                data["review_count"] = int(m.group(1))
                break
            except ValueError:
                pass

    # Images — only court_institution CDN images
    seen       = set()
    all_images = []
    for img in soup.find_all("img"):
        src = (img.get("data-src") or img.get("data-lazy") or
               img.get("data-lazy-src") or img.get("src") or "").split(',')[0].strip()
        if not src or any(x in src.lower() for x in ["placeholder", "logo", "icon", ".svg"]):
            continue
        full_url = urljoin(base_url, src)
        if full_url not in seen and "court_institution" in full_url.lower():
            seen.add(full_url)
            all_images.append(full_url)

    if all_images:
        all_images.sort(key=_swp_image_priority, reverse=True)
        data["main_image_url"] = all_images[0]
        main_code = _swp_extract_venue_code(data["main_image_url"])
        if main_code:
            data["gallery_image_urls"] = list(dict.fromkeys(
                u for u in all_images[1:] if _swp_extract_venue_code(u) == main_code
            ))

    # Sports types
    sports = set()
    for txt in soup.find_all(string=SWP_SPORTS_PATTERN):
        clean = txt.strip()
        if 4 < len(clean) < 35:
            sports.add(clean)
    data["sports_types"] = list(sports)

    return data


def _swp_scrape_sections(driver) -> dict:
    sections = {}
    buttons  = driver.find_elements(
        By.CSS_SELECTOR,
        "button.flex.items-center.justify-between.w-full.text-left",
    )
    print(f"  [SWP·Browser] Found {len(buttons)} accordion(s)")

    for i, btn in enumerate(buttons):
        try:
            label = btn.text.strip() or f"section_{i + 1}"
            key   = re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')

            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.7)
            btn.click()
            time.sleep(2.5)

            text = _swp_get_panel_text(driver, btn)

            if len(text) > 50:
                sections[key] = text
                print(f"    [{i+1}] '{label}' → {len(text)} chars")
            else:
                print(f"    [{i+1}] '{label}' → skipped (too short)")

        except Exception as e:
            print(f"    [!] Skipped button {i+1}: {e}")
            continue

    return sections


def _swp_get_venue_details(
    venue_id:    str,
    headless:    bool,
    booking_url: str = "",
) -> Optional[dict]:
    """
    Called when source='swp' is known from cache.
    Navigates directly to sportsweplay.com.my/venue/{venue_id}/ — no search needed.

    booking_url is NOT scraped from the page — it is taken from the venue list
    cache where it was mapped directly from SWP's own API field 'bookingLink'.
    """
    url = f"https://www.sportsweplay.com.my/venue/{venue_id}/"
    print(f"\n[SWP] Fetching details — venue_id='{venue_id}'")
    print(f"  [SWP·Browser] Opening {url}")

    driver = _make_driver(headless)
    try:
        driver.get(url)
        time.sleep(4)

        sections = _swp_scrape_sections(driver)

        for _ in range(5):
            driver.execute_script("window.scrollBy(0, 1200);")
            time.sleep(1.3)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)

        soup   = BeautifulSoup(driver.page_source, "html.parser")
        parsed = _swp_parse_venue_page(soup, url)

        parsed.update({
            "venue_id":    venue_id,
            "venue_url":   url,
            "booking_url": booking_url or f"https://www.swp.solemas.com/booking/venue_details/{venue_id}/booking_confirmations",
        })

        return _map_from_swp(parsed, sections)

    except Exception as e:
        print(f"  [SWP] Fatal: {e}")
        return None
    finally:
        driver.quit()


def _swp_fallback_search(venue_name: str, headless: bool) -> Optional[dict]:
    """
    Called only when venue is NOT in cache.
    Searches SWP listing API by name → finds venue_id + bookingLink → fetches details.
    """
    print(f"\n[SWP·Fallback] Searching for '{venue_name}' ...")
    name_lower = venue_name.lower().strip()
    offset     = 1
    best_id    = None
    best_link  = ""
    best_score = 0

    while True:
        payload = {"sportType": "all", "locationID": "all", "offset": offset, "sortBy": "name-asc"}
        try:
            r    = http_requests.post(SWP_VENUE_LIST_API, json=payload, headers=SWP_HEADERS_JSON, timeout=12)
            data = r.json()
        except Exception as e:
            print(f"  [SWP·Fallback] Request failed: {e}")
            return None

        for v in data.get("venueList", []):
            vname = (v.get("venueName") or "").lower().strip()
            if not vname:
                continue
            if name_lower in vname:
                score = 100
            elif vname in name_lower:
                score = 90
            else:
                score = len(set(name_lower.split()) & set(vname.split())) * 25

            if score > best_score:
                best_score = score
                best_id    = v.get("id")
                best_link  = v.get("bookingLink", "")

        if offset >= data.get("pageCount", 1):
            break
        offset += 1

    if best_id and best_score >= 40:
        print(f"  [SWP·Fallback] Found venue_id='{best_id}'")
        return _swp_get_venue_details(best_id, headless, booking_url=best_link)

    print(f"  [SWP·Fallback] Not found.")
    return None


# ═══════════════════════════════════════════════════════════
# MAPPERS — Unified schema
# ═══════════════════════════════════════════════════════════

def _map_from_afa(api_data: dict, sections: dict) -> dict:
    data       = api_data.get("data", {})
    info       = data.get("info", {})
    facilities = data.get("facilities", [])
    slug       = data.get("slug", "")

    return {
        "source":       "afa",
        "id":           str(data.get("id", "")),
        "name":         data.get("name"),
        "slug":         slug,
        "venue_url":    f"{AFA_VENUE_PAGE_BASE}/{slug}" if slug else None,
        "deeplink_url": data.get("deeplink_url"),
        "booking_url":  None,
        "rating":       round(float(info["rating"]), 2) if info.get("rating") else None,
        "review_count": None,
        "sports_types": list({
            cat.get("category", {}).get("name")
            for f in facilities
            for cat in f.get("categories", [])
            if cat.get("category", {}).get("name")
        }),
        "contact": {
            "phone":    info.get("phone_number"),
            "whatsapp": None,
            "email":    None,
            "social":   {"facebook": None, "instagram": None},
        },
        "location": {
            "address":  info.get("address"),
            "postcode": str(info.get("postcode", "")) or None,
            "city":     info.get("city"),
            "state":    info.get("state"),
            "country":  info.get("country"),
            "coordinates": {
                "lat": float(data["location_lat"])  if data.get("location_lat")  else None,
                "lng": float(data["location_long"]) if data.get("location_long") else None,
            },
        },
        "media": {
            "icon":        data.get("icon"),
            "main_image":  data.get("images", [None])[0],
            "gallery":     data.get("images", [])[1:],
            "floor_plans": info.get("floor_plan", []),
        },
        "sections": {
            "overview":             None,
            "pricing":              sections.get("pricing_myr_16_30_hour") or sections.get("pricing"),
            "opening_hours":        sections.get("opening_hours"),
            "amenities_facilities": sections.get("amenities_facilities"),
            "centre_layout":        sections.get("centre_layout"),
            "centre_policy":        sections.get("centre_policy"),
            "rules":                None,
        },
    }


def _map_from_swp(parsed: dict, sections: dict) -> dict:
    venue_id = parsed.get("venue_id", "")
    return {
        "source":       "swp",
        "id":           venue_id,
        "name":         parsed.get("venue_name"),
        "slug":         venue_id,
        "venue_url":    parsed.get("venue_url"),
        "deeplink_url": None,
        "booking_url":  parsed.get("booking_url"),
        "rating":       round(parsed["rating"], 2) if parsed.get("rating") else None,
        "review_count": parsed.get("review_count"),
        "sports_types": parsed.get("sports_types", []),
        "contact": {
            "phone":    parsed.get("phone"),
            "whatsapp": parsed.get("whatsapp"),
            "email":    None,
            "social": {
                "facebook":  parsed.get("social_links", {}).get("facebook"),
                "instagram": parsed.get("social_links", {}).get("instagram"),
            },
        },
        "location": {
            "address":  parsed.get("address"),
            "postcode": None,
            "city":     None,
            "state":    None,
            "country":  "Malaysia",
            "coordinates": {"lat": None, "lng": None},
        },
        "media": {
            "icon":        None,
            "main_image":  parsed.get("main_image_url"),
            "gallery":     parsed.get("gallery_image_urls", []),
            "floor_plans": [],
        },
        "sections": {
            "overview":             sections.get("overview"),
            "pricing":              sections.get("pricing_details") or sections.get("pricing"),
            "opening_hours":        sections.get("opening_hours") or sections.get("hours"),
            "amenities_facilities": sections.get("amenities_facilities") or sections.get("amenities"),
            "centre_layout":        None,
            "centre_policy":        None,
            "rules":                sections.get("rules"),
        },
    }


# ═══════════════════════════════════════════════════════════
# MAIN FUNCTION — get_venue_details()
# ═══════════════════════════════════════════════════════════

def get_venue_details(
    venue_name: str,
    slug: Optional[str] = None,
    venue_id: Optional[str] = None,
) -> Optional[Dict]:
    """Get detailed venue info. If slug/venue_id provided, use directly; else lookup by name."""
    if slug:
        # Assume AFA if slug provided
        return _afa_get_venue_details(slug, headless=True)
    elif venue_id:
        # Assume SWP if venue_id provided
        booking_url = ""
        return _swp_get_venue_details(venue_id, headless=True, booking_url=booking_url)
    else:
        # Lookup by name in cached data
        match = lookup_venue(venue_name)
        if match:
            if match['source'] == 'afa':
                return _afa_get_venue_details(match['slug'], headless=True)
            elif match['source'] == 'swp':
                booking_url = match.get('booking_url', '') or match.get('data', {}).get('bookingLink', '')
                return _swp_get_venue_details(match['id'], headless=True, booking_url=booking_url)

        # Fallback: search both platforms
        print(f"\n[Fallback] Venue '{venue_name}' not in cache. Searching both platforms...")
        result = _afa_fallback_search(venue_name, headless=True)
        if result:
            return result
        result = _swp_fallback_search(venue_name, headless=True)
        if result:
            return result

    return None


# ═══════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "healthy",
        "swp_venues_loaded": len(swp_venues),
        "afa_venues_loaded": len(afa_venues),
    })


@app.route('/api/venue-details', methods=['POST'])
def api_venue_details():
    """
    POST /api/venue-details
    Body: {
        "venue_name": "One Badminton Academy",   // required if slug/venue_id not given
        "slug": "one-badminton-academy",          // optional — AFA direct lookup
        "venue_id": "ISALSTBB"                    // optional — SWP direct lookup
    }
    """
    try:
        body = request.get_json(force=True)
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON body"}), 400

    if not body:
        return jsonify({"success": False, "error": "Empty request body"}), 400

    venue_name = body.get("venue_name", "").strip()
    slug       = body.get("slug", "").strip() or None
    venue_id   = body.get("venue_id", "").strip() or None

    if not venue_name and not slug and not venue_id:
        return jsonify({
            "success": False,
            "error": "At least one of 'venue_name', 'slug', or 'venue_id' is required"
        }), 400

    print(f"\n{'='*60}")
    print(f"[API] Request: venue_name='{venue_name}', slug='{slug}', venue_id='{venue_id}'")
    print(f"{'='*60}")

    try:
        result = get_venue_details(
            venue_name=venue_name or "unknown",
            slug=slug,
            venue_id=venue_id,
        )
    except Exception as e:
        print(f"[API] Error: {e}")
        return jsonify({
            "success": False,
            "error": f"Internal error: {str(e)}",
            "query": {
                "venue_name": venue_name,
                "slug": slug,
                "venue_id": venue_id,
            }
        }), 500

    if result is None:
        return jsonify({
            "success": False,
            "error": f"Venue not found: '{venue_name or slug or venue_id}'",
            "query": {
                "venue_name": venue_name,
                "slug": slug,
                "venue_id": venue_id,
            }
        }), 404

    return jsonify({
        "success": True,
        "error": None,
        "data": result,
    })


# ═══════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("[Startup] Loading venue data...")
    load_venue_data()
    print(f"[Startup] Ready — {len(swp_venues)} SWP + {len(afa_venues)} AFA venues")
    print(f"[Startup] Starting Flask on port 5000...")
    app.run(host='0.0.0.0', port=5000, debug=False)
