#!/usr/bin/env python3
"""
NiceEvents backend scraper
Runs on GitHub Actions every 3h, outputs events.json served via GitHub Pages.
"""

import asyncio
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import dateutil.parser
from bs4 import BeautifulSoup
from dateutil.tz import gettz
from playwright.async_api import async_playwright, Page

# ─── Unsplash config ───────────────────────────────────────────────────────────
# Get a free key at https://unsplash.com/developers (50 req/h on free tier)
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()

_CAT_QUERIES = {
    "CONCERT": "concert live music performance crowd",
    "EXPO":    "art exhibition gallery contemporary museum",
    "THEATRE": "theatre stage actors performance spotlight",
    "CINEMA":  "cinema film projector screen",
    "MARCHE":  "outdoor market france provence vendors",
    "FOOD":    "gourmet food restaurant meal table",
    "FAMILLE": "family children activities outdoor fun",
    "NUIT":    "nightclub party dj crowd lights",
    "SPORT":   "sport outdoor running competition",
    "AUTRE":   "côte d'azur nice france mediterranean",
}

# ─── Site configs ──────────────────────────────────────────────────────────────

SITES = [
    {"source": "VILLE_NICE",       "city": "Nice",           "url": "https://www.nice.fr/agenda/"},
    {"source": "OT_NICE",          "city": "Nice",           "url": "https://www.explorenicecotedazur.com/agenda/"},
    {"source": "CANNES",           "city": "Cannes",         "url": "https://www.cannes.com/fr/agenda/agenda-recherche-filtree.html"},
    {"source": "ANTIBES",          "city": "Antibes",        "url": "https://www.antibes-juanlespins.com/information/agenda/evenements-ponctuels"},
    {"source": "MENTON",           "city": "Menton",         "url": "https://www.menton-riviera-merveilles.fr/sorganiser/agenda/tout-lagenda/"},
    {"source": "CAGNES",           "city": "Cagnes-sur-Mer", "url": "https://ville.cagnes.fr/les-evenements/"},
    {"source": "TNN",              "city": "Nice",           "url": "https://www.tnn.fr/fr/calendrier"},
    {"source": "OPERA_NICE",       "city": "Nice",           "url": "https://www.opera-nice.org/fr/calendrier"},
    {"source": "NIKAIA",           "city": "Nice",           "url": "https://www.nikaia.fr/programmation"},
    {"source": "LE109",            "city": "Nice",           "url": "https://le109.nice.fr/programmation"},
    {"source": "PALAIS_FESTIVALS", "city": "Cannes",         "url": "https://www.palaisdesfestivals.com/agenda/culturel/"},
    {"source": "MAMAC",            "city": "Nice",           "url": "https://www.mamac-nice.org/exposition/"},
    {"source": "CHAGALL",          "city": "Nice",           "url": "https://musees-nationaux-alpesmaritimes.fr/chagall/agenda"},
    {"source": "MATISSE",          "city": "Nice",           "url": "https://www.musee-matisse-nice.org/fr/evenement/"},
    {"source": "STOCKFISH",        "city": "Nice",           "url": "https://www.infoconcert.com/salle/stockfish-a-nice-68191/concerts"},
    {"source": "MONACO",           "city": "Monaco",         "url": "https://www.visitmonaco.com/evenements/agenda-des-evenements?page=1"},
    {"source": "MONACO",           "city": "Monaco",         "url": "https://www.visitmonaco.com/evenements/agenda-des-evenements?page=2"},
    {"source": "MONACO",           "city": "Monaco",         "url": "https://www.visitmonaco.com/evenements/agenda-des-evenements?page=3"},
    {"source": "COTEDAZUR",        "city": "Nice",           "url": "https://cotedazurfrance.fr/decouvrir/votre-sejour/nice-cote-dazur/lagenda-des-evenements-a-nice/?listpage=1"},
    {"source": "COTEDAZUR",        "city": "Nice",           "url": "https://cotedazurfrance.fr/decouvrir/votre-sejour/nice-cote-dazur/lagenda-des-evenements-a-nice/?listpage=2"},
    {"source": "INFOLOCALE",       "city": "Nice",           "url": "https://www.infolocale.fr/evenements?age%5B0%5D=Tout%20Public&age%5B1%5D=Adulte&age%5B2%5D=Adolescent&age%5B3%5D=Enfant&age%5B4%5D=Bebe&commune=06088"},
]

# ─── Category inference ────────────────────────────────────────────────────────

_CATS = {
    "CONCERT":  ["concert", "musique", "jazz", "rock", "electro", "dj", "vinyle", "chant",
                 "symphonie", "philharmon", "orchestre", "opéra", "récital"],
    "THEATRE":  ["théâtre", "theatre", "pièce", "comédie", "comedie", "spectacle", "one man"],
    "EXPO":     ["exposition", "expo", "vernissage", "galerie", "musée", "musee",
                 "photo", "peinture", "sculpture"],
    "CINEMA":   ["cinéma", "cinema", "film", "projection", "documentaire", "ciné"],
    "MARCHE":   ["marché", "marche", "brocante", "vide-grenier", "foire", "salon"],
    "FOOD":     ["food", "gastronomie", "cuisine", "dégustation", "vin", "restaurant", "chef"],
    "FAMILLE":  ["enfant", "famille", "jeunesse", "kids", "conte"],
    "NUIT":     ["club", "boite", "discothèque", "nightclub", "soirée", "afterwork"],
    "SPORT":    ["sport", "triathlon", "marathon", "course", "vélo", "yoga", "natation"],
}

def infer_category(title: str) -> str:
    text = title.lower()
    for cat, kws in _CATS.items():
        if any(kw in text for kw in kws):
            return cat
    return "AUTRE"

# ─── Parsing helpers ───────────────────────────────────────────────────────────

_PARIS_TZ = gettz("Europe/Paris")

def parse_ms(s) -> Optional[int]:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = dateutil.parser.parse(s, dayfirst=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_PARIS_TZ)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None

_FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3,
    "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}

def parse_french_date(text: str) -> Optional[str]:
    """'Ven. 23 mai 2026 14h30', 'Du 5 au 8 juin 2026', 'mardi 12 mai' → ISO datetime/date string."""
    text = re.sub(r"\b(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\b(lun|mar|mer|jeu|ven|sam|dim)\.?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = text.lower().strip()
    # Extract time component if present: "14h30", "14h", "14:30"
    time_suffix = ""
    tm = re.search(r'(?:^|[\s\-àa])(\d{1,2})[h:](\d{0,2})(?!\d)', text)
    if tm:
        h_val, m_val = int(tm.group(1)), int(tm.group(2)) if tm.group(2) else 0
        if 0 <= h_val <= 23 and 0 <= m_val <= 59:
            time_suffix = f"T{h_val:02d}:{m_val:02d}:00"
    # With explicit year: "23 mai 2026"
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(20\d{2})", text)
    if m:
        day, month_str, year = m.group(1), m.group(2), m.group(3)
        month = _FRENCH_MONTHS.get(month_str)
        if month:
            return f"{year}-{month:02d}-{int(day):02d}{time_suffix}"
    # Without year: "12 mai" — infer current or next year
    m = re.search(r"(\d{1,2})\s+(\w+)", text)
    if m:
        day, month_str = m.group(1), m.group(2)
        month = _FRENCH_MONTHS.get(month_str)
        if month:
            now = datetime.now(timezone.utc)
            year = now.year
            try:
                candidate = datetime(year, month, int(day), tzinfo=timezone.utc)
                if candidate < now - timedelta(days=1):
                    year += 1
                return f"{year}-{month:02d}-{int(day):02d}{time_suffix}"
            except ValueError:
                return None
    return None

def parse_price(raw: Optional[str]) -> tuple:
    if not raw:
        return "—", 0
    lo = raw.lower().strip()
    if any(w in lo for w in ["gratuit", "libre", "free"]):
        return "Gratuit", 0
    nums = [float(m.replace(",", ".")) for m in re.findall(r"\d+(?:[.,]\d+)?", raw)
            if 0 < float(m.replace(",", ".")) < 10_000]
    if not nums:
        return "—", 0
    if len(nums) == 1:
        return f"€{int(nums[0])}", int(nums[0])
    return f"€{int(min(nums))}–{int(max(nums))}", int(min(nums))

def _str(obj: dict, *keys) -> Optional[str]:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def _normalize_image_url(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url:
        return None
    if url.startswith("https://"):
        return url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    if url.startswith("//"):
        return "https:" + url
    return None

def _img(obj: dict) -> Optional[str]:
    for k in ["image", "imageUrl", "thumbnail", "photo", "cover", "picture",
              "thumbnail_url", "image_url", "featured_image", "banner", "poster",
              "visual", "visuel", "affiche", "illustration", "media"]:
        v = obj.get(k)
        if isinstance(v, str):
            url = _normalize_image_url(v)
            if url:
                return url
        if isinstance(v, dict):
            u = v.get("url") or v.get("src") or v.get("contentUrl") or v.get("href")
            url = _normalize_image_url(u)
            if url:
                return url
            # Monaco-style srcset dict: {w414_search: "https://...", w592_search: "https://..."}
            srcset = v.get("srcset")
            if isinstance(srcset, dict):
                for sv in srcset.values():
                    url = _normalize_image_url(sv)
                    if url:
                        return url
            elif isinstance(srcset, str):
                first = srcset.split(",")[0].strip().split(" ")[0]
                url = _normalize_image_url(first)
                if url:
                    return url
    return None

def _url(obj: dict, site_url: str) -> str:
    """Extract URL from event object, handling both string and dict values."""
    for k in ["url", "link", "lien", "permalink", "href"]:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            u = v.strip()
            if u.startswith("http"):
                return u
            if u.startswith("/"):
                base = re.match(r"https?://[^/]+", site_url)
                return (base.group(0) if base else "") + u
        if isinstance(v, dict):
            u = v.get("href") or v.get("url") or v.get("src")
            if isinstance(u, str) and u.strip():
                u = u.strip()
                if u.startswith("http"):
                    return u
                if u.startswith("/"):
                    base = re.match(r"https?://[^/]+", site_url)
                    return (base.group(0) if base else "") + u
    return site_url

# ─── JSON event extraction ─────────────────────────────────────────────────────

_NOW_MS = int(time.time() * 1000)

def _make_event(title, date_str, obj, site, seen) -> Optional[dict]:
    if title in seen:
        return None
    starts_at = parse_ms(date_str)
    if not starts_at or starts_at < _NOW_MS - 3_600_000:
        return None
    has_time = bool(re.search(r'[T ]\d{2}:\d{2}', date_str or ""))
    end_str = _str(obj, "endDate", "end_date", "dateFin", "endsAt", "end_at")
    ends_at = parse_ms(end_str) or (starts_at + 7_200_000)
    loc = obj.get("location") or obj.get("lieu") or obj.get("place") or obj.get("venue")
    if isinstance(loc, dict):
        place = loc.get("name") or loc.get("nom") or site["city"]
    elif isinstance(loc, str) and loc.strip():
        place = loc
    else:
        place = site["city"]
    url = _url(obj, site["url"])
    price_raw = _str(obj, "price", "prix", "tarif", "cost")
    price, price_val = parse_price(price_raw)
    seen.add(title)
    return {
        "title": title,
        "description": _str(obj, "description", "summary", "excerpt") or "",
        "place": place,
        "city": site["city"],
        "starts_at": starts_at,
        "ends_at": ends_at,
        "has_time": has_time,
        "price": price,
        "price_val": price_val,
        "source": site["source"],
        "source_url": url,
        "image_url": _img(obj),
        "category": infer_category(title),
    }

def walk(data, site: dict, seen: set, depth: int = 0) -> list:
    if depth > 12 or len(seen) > 300:
        return []
    events = []
    if isinstance(data, dict):
        type_val = str(data.get("@type", ""))
        if "Event" in type_val and "ItemList" not in type_val:
            name = _str(data, "name")
            date = _str(data, "startDate")
            if name and date:
                loc = data.get("location") or {}
                if "VirtualLocation" in str(loc.get("@type", "") if isinstance(loc, dict) else ""):
                    return []
                ev = _make_event(name, date, data, site, seen)
                if ev:
                    return [ev]
            return []
        if "ItemList" in type_val:
            for item in data.get("itemListElement", []):
                inner = item.get("item", item) if isinstance(item, dict) else item
                events.extend(walk(inner, site, seen, depth + 1))
            return events
        title = _str(data, "title", "name", "titre", "nom", "libelle", "label")
        date_str = _str(data, "startDate", "start_date", "dateDebut", "date_debut",
                        "startsAt", "start_at", "date_start")
        if title and date_str and len(title) > 3 and re.search(r"20\d{2}", date_str):
            ev = _make_event(title, date_str, data, site, seen)
            if ev:
                return [ev]
        for v in data.values():
            events.extend(walk(v, site, seen, depth + 1))
    elif isinstance(data, list):
        for item in data:
            events.extend(walk(item, site, seen, depth + 1))
    return events

# ─── Per-site Playwright scrape ────────────────────────────────────────────────

async def scrape_site(page: Page, site: dict) -> list:
    print(f"\n[{site['source']}] {site['url']}")
    captured: list = []

    async def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if resp.status == 200 and "json" in ct:
                body = await resp.body()
                if len(body) > 200:
                    try:
                        captured.append(json.loads(body))
                    except Exception:
                        pass
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await page.goto(site["url"], wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        print(f"  goto error: {e}")

    # Wait for JS to hydrate and fire API calls
    await asyncio.sleep(4)

    # Dismiss cookie/RGPD banners
    for sel in ["#tarteaucitronAllDenied2", "#accept-all-cookies", ".cc-btn.cc-dismiss",
                "[class*='cookie-accept']", ".popin-cookies .accept", ".rgpd-accept-all",
                "button[data-testid='accept-all']", "#axeptio_btn_acceptAll"]:
        try:
            await page.click(sel, timeout=1_000)
            await asyncio.sleep(1)
            break
        except Exception:
            pass

    # OPERA_NICE: calendar defaults to past season — click "today/next" to reach current month
    if site["source"] == "OPERA_NICE":
        for nav_sel in [".fc-today-button", "button[aria-label*='aujourd']",
                        "button[aria-label*='Today']", ".calendar-today",
                        ".fc-next-button", "button[aria-label*='next']", "button[aria-label*='suivant']"]:
            try:
                await page.click(nav_sel, timeout=1_500)
                await asyncio.sleep(2)
                break
            except Exception:
                pass

    # Scroll to trigger lazy-loaded content, wait for any new requests
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(3)
    except Exception:
        pass

    # Remove listener before processing
    page.remove_listener("response", on_response)

    content = await page.content()
    soup = BeautifulSoup(content, "lxml")
    seen: set = set()
    events: list = []

    # Diagnostics
    ld_scripts = soup.find_all("script", type="application/ld+json")
    print(f"  HTML: {len(content)} chars | JSON-LD scripts: {len(ld_scripts)} | API responses captured: {len(captured)}")
    if captured:
        for i, r in enumerate(captured[:5]):
            keys = list(r.keys()) if isinstance(r, dict) else f"list[{len(r)}]"
            print(f"  API[{i}]: keys={keys} | {str(r)[:500]}")

    # Strategy 1: JSON-LD
    for script in ld_scripts:
        try:
            data = json.loads(script.string or "")
            events.extend(walk(data, site, seen))
        except Exception:
            pass
    if events:
        print(f"  → JSON-LD: {len(events)} events")
        return events

    # Strategy 2a: Site-specific parsers for known API formats
    source = site["source"]
    if not events:
        if source == "OPERA_NICE":
            for resp in captured:
                if not isinstance(resp, dict) or resp.get("success") != 1:
                    continue
                result = resp.get("result", [])
                if not isinstance(result, list):
                    continue
                for item in result:
                    title = _str(item, "title", "name")
                    start_ms = item.get("start")
                    if not title or not isinstance(start_ms, (int, float)):
                        continue
                    start_ms = int(start_ms)
                    if start_ms < _NOW_MS - 3_600_000 or title in seen:
                        continue
                    seen.add(title)
                    end_ms = item.get("end")
                    end_ms = int(end_ms) if isinstance(end_ms, (int, float)) else (start_ms + 7_200_000)
                    url = item.get("url", "")
                    if url and not url.startswith("http"):
                        url = "https://www.opera-nice.org" + url
                    events.append({
                        "title": title,
                        "description": _str(item, "description", "summary") or "",
                        "place": "Opéra de Nice",
                        "city": site["city"],
                        "starts_at": start_ms,
                        "ends_at": end_ms,
                        "has_time": True,
                        "price": "—",
                        "price_val": 0,
                        "source": site["source"],
                        "source_url": url or site["url"],
                        "image_url": _img(item),
                        "category": infer_category(title),
                    })
            if events:
                print(f"  → OPERA_NICE API: {len(events)} events")
                return events

        elif source == "CHAGALL":
            for resp in captured:
                if not isinstance(resp, dict):
                    continue
                html_str = resp.get("html")
                if not html_str:
                    continue
                ch_soup = BeautifulSoup(html_str, "lxml")
                base_url = "https://musees-nationaux-alpesmaritimes.fr"
                for article in ch_soup.select("article[about], article.node-event, article[class*='event']"):
                    title_el = article.select_one("h2 a, h3 a, h2, h3, .field-name-title, .node-title")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    date_str = None
                    date_el = article.select_one(
                        "time[datetime], [class*='field-name-field-date'], [class*='date-display'], "
                        "[class*='field--name-field-date'], .date-display-single, [class*='tribe-event-date'], "
                        "[class*='date']"
                    )
                    if date_el:
                        date_str = date_el.get("datetime") or parse_french_date(date_el.get_text(strip=True))
                    # Fallback: try any <p> or <span> text
                    if not date_str:
                        for el in article.select("p, span"):
                            date_str = parse_french_date(el.get_text(strip=True))
                            if date_str:
                                break
                    if not date_str:
                        continue
                    url = article.get("about", "")
                    if url and not url.startswith("http"):
                        url = base_url + url
                    img_el = article.select_one("img[src]")
                    img_url = img_el.get("src") if img_el else None
                    if img_url and not img_url.startswith("http"):
                        img_url = base_url + img_url
                    ev = _make_event(title, date_str, {}, site, seen)
                    if ev:
                        ev["source_url"] = url or site["url"]
                        if img_url:
                            ev["image_url"] = _normalize_image_url(img_url)
                        events.append(ev)
            if events:
                print(f"  → CHAGALL HTML: {len(events)} events")
                return events

        elif source == "CAGNES":
            for resp in captured:
                if not isinstance(resp, dict):
                    continue
                data = resp.get("data")
                if not isinstance(data, dict):
                    continue
                html_str = data.get("html")
                if not html_str:
                    continue
                ca_soup = BeautifulSoup(html_str, "lxml")
                for item in ca_soup.select(".jet-listing-grid__item, .jet-listing-item, article, .elementor-post"):
                    title_el = item.select_one(
                        "h2, h3, h4, .jet-listing-dynamic-field__content, "
                        ".elementor-heading-title, .entry-title"
                    )
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    date_str = None
                    for date_el in item.select("time[datetime], [class*='date'], .elementor-icon-list-text"):
                        date_str = date_el.get("datetime") or parse_french_date(date_el.get_text(strip=True))
                        if date_str:
                            break
                    if not date_str:
                        continue
                    link_el = item.select_one("a[href]")
                    url = link_el.get("href") if link_el else site["url"]
                    if url and not url.startswith("http"):
                        url = "https://ville.cagnes.fr" + url
                    ev = _make_event(title, date_str, {}, site, seen)
                    if ev:
                        ev["source_url"] = url or site["url"]
                        events.append(ev)
            if events:
                print(f"  → CAGNES JetEngine HTML: {len(events)} events")
                return events

        elif source == "INFOLOCALE":
            # Algolia response: API[2] = {'results': [{'hits': [...]}]}
            # Each hit: titre, photo.url, texte (contains French date), lieu, date_debut
            for resp in captured:
                if not isinstance(resp, dict):
                    continue
                results = resp.get("results")
                if not isinstance(results, list) or not results:
                    continue
                hits = results[0].get("hits", []) if isinstance(results[0], dict) else []
                if not hits:
                    continue
                base = "https://www.infolocale.fr"
                for hit in hits:
                    title = hit.get("titre") or hit.get("title") or hit.get("nom")
                    if not title:
                        continue
                    # Try explicit date fields first
                    date_str = (hit.get("date_debut") or hit.get("dateDebut")
                                or hit.get("date") or hit.get("startDate"))
                    # Fall back to parsing from texte/description
                    if not date_str:
                        texte = hit.get("texte") or hit.get("description") or ""
                        date_str = parse_french_date(texte)
                    if not date_str:
                        continue
                    slug = hit.get("slug") or hit.get("id") or ""
                    url = f"{base}/annonce/{slug}" if slug else site["url"]
                    photo = hit.get("photo") or {}
                    img_url = photo.get("url") if isinstance(photo, dict) else None
                    lieu = hit.get("lieu") or {}
                    place = (lieu.get("nom") or lieu.get("libelle") or "Nice") if isinstance(lieu, dict) else "Nice"
                    ev = _make_event(title, date_str, {}, site, seen)
                    if ev:
                        ev["source_url"] = url
                        ev["place"] = place
                        if img_url:
                            ev["image_url"] = _normalize_image_url(img_url)
                        events.append(ev)
                if events:
                    print(f"  → INFOLOCALE Algolia: {len(events)} events")
                    return events

    # Strategy 2: Network-intercepted API responses
    for resp in captured:
        if isinstance(resp, dict) and set(resp.keys()) & {"GLOBAL", "SECTIONS", "TOOLTIPS", "NOTIFICATIONS"}:
            continue
        events.extend(walk(resp, site, seen))
    if events:
        print(f"  → API intercept: {len(events)} events")
        return events

    # Strategy 3: BeautifulSoup CSS selectors (static/SSR pages)
    events = scrape_html(soup, site, seen)
    if events:
        print(f"  → HTML selectors: {len(events)} events")

    # Apply og:image as fallback for events without a per-event image
    og_tag = soup.find("meta", property="og:image")
    page_image = _normalize_image_url(og_tag.get("content") if og_tag else None)
    if page_image:
        for ev in events:
            if not ev.get("image_url"):
                ev["image_url"] = page_image

    if events:
        return events

    print("  → 0 events")
    return []


def scrape_html(soup: BeautifulSoup, site: dict, seen: set) -> list:
    """CSS selector fallback for sites with known HTML structure."""
    events = []
    source = site["source"]

    # nice.fr — municipal agenda with French date text in cards
    if source == "VILLE_NICE":
        base = "https://www.nice.fr"
        all_cards = soup.select("article.event--block, article[class*='event-date']")
        if all_cards:
            print(f"  VILLE_NICE: {len(all_cards)} cards")
        for card in all_cards:
            title_el = card.select_one("h2, h3, .tribe-event-name a, a[class*='title']")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue
            date_str = None
            # Tribe Events: date is in <meta itemprop="startDate" content="2026-05-19">
            # and time is in a SEPARATE <time datetime="14:30:00"> — must combine both
            meta_el = card.select_one("meta[itemprop='startDate']")
            if meta_el:
                date_val = meta_el.get("content", "")
                if re.search(r'^\d{4}-\d{2}-\d{2}T', date_val):
                    # Already a full ISO datetime (includes time) — use as-is
                    date_str = date_val
                elif re.search(r'^\d{4}-\d{2}-\d{2}$', date_val):
                    # Date-only — look for a separate time element
                    date_str = date_val
                    for tel in card.select("time[datetime]"):
                        tval = tel.get("datetime", "")
                        if re.match(r'^\d{1,2}:\d{2}', tval):  # time-only value
                            date_str = date_val + "T" + tval
                            break
            if not date_str:
                # Fallback: abbr with title attribute, or visible text
                date_el = card.select_one(
                    "abbr.tribe-event-date-start, time[datetime], "
                    "[class*='tribe-event-date'], abbr[class*='date-start']"
                )
                if date_el:
                    dt_attr = date_el.get("title") or date_el.get("datetime")
                    if dt_attr and re.search(r'\d{4}-\d{2}-\d{2}', dt_attr):
                        date_str = dt_attr
                    else:
                        date_str = parse_french_date(date_el.get_text(strip=True))
            if not date_str:
                continue
            link_el = card.select_one("a[href*='/agenda/']")
            if link_el:
                href = link_el.get("href", "")
                url = href if href.startswith("http") else base + href
            else:
                url = site["url"]
            img_el = card.select_one("img[src]")
            img_url = None
            if img_el:
                src = img_el.get("src", "")
                if src.startswith("http"):
                    img_url = _normalize_image_url(src)
                elif src.startswith("/"):
                    img_url = _normalize_image_url(base + src)
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                if img_url:
                    ev["image_url"] = img_url
                events.append(ev)
        return events

    # tnn.fr — date is a text sibling of the <a>, title may be link text directly
    if source == "TNN":
        # Diagnostics: show first spectacle/evenement links found
        sample = soup.select("a[href*='/spectacles/'], a[href*='/evenements/']")
        print(f"  TNN links found: {len(sample)} — first 3: {[a.get('href','')[:80] for a in sample[:3]]}")
        base = "https://www.tnn.fr"
        # Exclude season overview page — only match event-specific slugs (contain at least 2 slashes after /fr/)
        for link in soup.select("a[href*='/spectacles/'], a[href*='/evenements/']"):
            href = link.get("href", "")
            if not href:
                continue
            # Skip nav-level links like /fr/spectacles/saison-2025-2026 (no event slug after)
            if re.search(r"/(spectacles|evenements)/?$", href):
                continue
            if re.search(r"/spectacles/saison-\d{4}-\d{4}/?$", href):
                continue
            # Title: prefer heading inside link, fall back to link text
            title_el = link.select_one("h3, h2, h4, [class*='event-title'], [class*='title']")
            title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
            if not title or len(title) < 3:
                continue
            date_str = None
            # Date is a day-group header (e.g. <strong>mardi 12 mai</strong>) sitting
            # several DOM levels above the link — find_all_previous() traverses backward
            # through the whole document regardless of nesting depth.
            for prev in link.find_all_previous(["strong", "h2", "h3", "time"], limit=15):
                if prev.name == "time" and prev.get("datetime"):
                    date_str = prev.get("datetime")
                    break
                parsed = parse_french_date(prev.get_text(strip=True))
                if parsed:
                    date_str = parsed
                    break
            if not date_str:
                continue
            url = href if href.startswith("http") else base + href
            img_el = link.select_one("img[src]")
            img_url = img_el.get("src") if img_el else None
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                if img_url:
                    ev["image_url"] = _normalize_image_url(img_url if img_url.startswith("http") else base + img_url)
                events.append(ev)
        return events

    # le109.nice.fr — cards: <a href="/programmation/[slug]"><img><h2>Title<span>date</span><span>venue</span></a>
    # get_text(strip=True) concatenates all without separators → use separator="\n" to split cleanly
    if source == "LE109":
        base = "https://le109.nice.fr"
        all_links = soup.select("a[href*='/programmation/']")
        event_links = [l for l in all_links if not re.match(r"^/programmation/?$", l.get("href", ""))]
        print(f"  LE109 debug: {len(all_links)} total links, {len(event_links)} event links")
        _date_hint = re.compile(
            r"\d{4}|janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
            r"septembre|octobre|novembre|décembre|decembre|lundi|mardi|mercredi|"
            r"jeudi|vendredi|samedi|dimanche", re.IGNORECASE
        )
        for link in event_links:
            href = link.get("href", "")
            url = href if href.startswith("http") else base + href
            # Split all text by newline to get clean fields (title / date / venue / price)
            lines = [l.strip() for l in link.get_text(separator="\n", strip=True).split("\n") if l.strip()]
            if not lines:
                continue
            # Title = first line that isn't a date indicator or a short number (price/year)
            title = None
            for line in lines:
                if len(line) >= 3 and not _date_hint.search(line) and not re.match(r"^\d+\s*€?$", line):
                    title = line
                    break
            if not title:
                title = lines[0]  # fallback: take first line
            if not title or len(title) < 2:
                continue
            # Date + time: scan all lines
            date_str = None
            for line in lines:
                parsed = parse_french_date(line)
                if parsed:
                    date_str = parsed
                    break
            if not date_str:
                continue
            # Venue/description: last meaningful non-date, non-price, non-title line
            description = ""
            for line in lines:
                if line == title or _date_hint.search(line) or re.match(r"^\d+\s*€?$", line):
                    continue
                if len(line) >= 2:
                    description = line
                    break
            # Image: prefer data-src (lazy loading), fallback to src
            img_url = None
            img_el = link.select_one("img[data-src], img[src]")
            if img_el:
                src = img_el.get("data-src") or img_el.get("src", "")
                if src and not src.startswith("data:"):
                    img_url = _normalize_image_url(src if src.startswith("http") else base + src)
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                if img_url:
                    ev["image_url"] = img_url
                if description:
                    ev["description"] = description
                events.append(ev)
        if events:
            print(f"  → LE109: {len(events)} events")
        return events

    # infoconcert.com — concert listings
    if source == "STOCKFISH":
        for card in soup.select(".concert-item, .event-item, article.concert, .list-concert li"):
            title_el = card.select_one("h2, h3, .title, .artist, .concert-title")
            date_el = card.select_one("time, .date, .concert-date, [datetime]")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            date_str = date_el.get("datetime") or date_el.get_text(strip=True) if date_el else None
            link_el = card.select_one("a[href]")
            url = link_el["href"] if link_el else site["url"]
            if not url.startswith("http"):
                url = "https://www.infoconcert.com" + url
            ev = _make_event(title, date_str, {}, site, seen) if date_str else None
            if ev:
                ev["source_url"] = url
                events.append(ev)

    # cotedazurfrance.fr — structure: <a href="/offres/...-nice-fr-[id]/"><img><span date><h3 title></a>
    if source == "COTEDAZUR":
        for link in soup.select("a[href*='/offres/']"):
            href = link.get("href", "")
            title_el = link.select_one("h3, h2, h4")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            date_str = None
            for el in link.select("span, p, time"):
                text = el.get_text(strip=True)
                parsed = parse_french_date(text)
                if parsed:
                    date_str = parsed
                    break
            if not date_str:
                continue
            url = href if href.startswith("http") else "https://cotedazurfrance.fr" + href
            img_el = link.select_one("img[src]")
            img_url = img_el.get("src") if img_el else None
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                if img_url:
                    ev["image_url"] = _normalize_image_url(img_url)
                events.append(ev)
        if events:
            print(f"  → COTEDAZUR: {len(events)} events")
        return events

    # menton-riviera-merveilles.fr — same CMS as COTEDAZUR (/offres/ links, Cloudly CDN images)
    if source == "MENTON":
        base = "https://www.menton-riviera-merveilles.fr"
        for link in soup.select("a[href*='/offres/']"):
            href = link.get("href", "")
            title_el = link.select_one("h3, h2, h4")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            date_str = None
            for el in link.select("span, p, time"):
                text = el.get_text(strip=True)
                parsed = parse_french_date(text)
                if parsed:
                    date_str = parsed
                    break
            if not date_str:
                continue
            url = href if href.startswith("http") else base + href
            img_el = link.select_one("img[src]")
            img_url = None
            if img_el:
                src = img_el.get("src", "")
                if src.startswith("http"):
                    img_url = _normalize_image_url(src)
                elif src.startswith("/"):
                    img_url = _normalize_image_url(base + src)
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                if img_url:
                    ev["image_url"] = img_url
                events.append(ev)
        if events:
            print(f"  → MENTON: {len(events)} events")
        return events

    # infolocale.fr — local events aggregator, commune=06088 (Nice)
    if source == "INFOLOCALE":
        base = "https://www.infolocale.fr"
        for card in soup.select(
            ".event-item, .evenement, article.node-evenement, .views-row, "
            "article[class*='event'], li[class*='event'], .card-event, "
            "[class*='fiche-event'], [class*='event-teaser']"
        ):
            title_el = card.select_one("h2, h3, h4, .title, [class*='title'], [class*='nom']")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            date_str = None
            for date_el in card.select("time[datetime], [class*='date'], [class*='Date'], .field-date"):
                date_str = date_el.get("datetime") or parse_french_date(date_el.get_text(strip=True))
                if date_str:
                    break
            if not date_str:
                continue
            link_el = card.select_one("a[href]")
            url = link_el.get("href", "") if link_el else site["url"]
            if url and not url.startswith("http"):
                url = base + url
            img_el = card.select_one("img[src], img[data-src]")
            img_url = (img_el.get("src") or img_el.get("data-src")) if img_el else None
            if img_url and not img_url.startswith("http"):
                img_url = base + img_url
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url or site["url"]
                if img_url:
                    ev["image_url"] = _normalize_image_url(img_url)
                events.append(ev)
        if events:
            print(f"  → INFOLOCALE: {len(events)} events")
        return events

    # antibes-juanlespins.com — 60 events, /information/agenda/ links with headings
    if source == "ANTIBES":
        base = "https://www.antibes-juanlespins.com"
        for link in soup.select("a[href*='/information/agenda/'], a[href*='/agenda/']"):
            href = link.get("href", "")
            if not href or href.endswith("/agenda") or href.endswith("/agenda/"):
                continue
            title_el = link.select_one("h2, h3, h4")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            date_str = None
            for el in link.select("time, [class*='date'], p, span"):
                date_str = parse_french_date(el.get_text())
                if date_str:
                    break
            if not date_str:
                continue
            url = href if href.startswith("http") else base + href
            img_el = link.select_one("img[src]")
            img_url = None
            if img_el:
                src = img_el.get("src", "")
                if src.startswith("http"):
                    img_url = _normalize_image_url(src)
                elif src.startswith("/"):
                    img_url = _normalize_image_url(base + src)
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                if img_url:
                    ev["image_url"] = img_url
                events.append(ev)
        if events:
            print(f"  → ANTIBES: {len(events)} events")
        return events

    # cannes.com — 280 events in static HTML, links like /agenda/annee-...
    if source == "CANNES":
        base = "https://www.cannes.com"
        for link in soup.select("a[href*='/agenda/annee-']"):
            href = link.get("href", "")
            if not href:
                continue
            # Title: last non-empty line of anchor text (skip category prefix)
            lines = [l.strip() for l in link.get_text().splitlines() if l.strip()]
            title = lines[-1] if lines else None
            if not title:
                continue
            # Date: try strong tags first, then full text
            date_str = None
            for el in link.select("strong, [class*='date'], time"):
                date_str = parse_french_date(el.get_text())
                if date_str:
                    break
            if not date_str:
                date_str = parse_french_date(link.get_text())
            if not date_str:
                continue
            url = href if href.startswith("http") else base + href
            img_el = link.select_one("img[src]")
            img_url = None
            if img_el:
                src = img_el.get("src", "")
                if src.startswith("http"):
                    img_url = _normalize_image_url(src)
                elif src.startswith("/"):
                    img_url = _normalize_image_url(base + src)
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                if img_url:
                    ev["image_url"] = img_url
                events.append(ev)
        if events:
            print(f"  → CANNES: {len(events)} events")
        return events

    # musee-matisse-nice.org — event links with h3 title and p date text
    if source == "MATISSE":
        base = "https://www.musee-matisse-nice.org"
        for card in soup.select("a[href*='/fr/evenement/']"):
            href = card.get("href", "")
            if not href:
                continue
            title_el = card.select_one("h3, h2, h4")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue
            date_str = None
            for el in card.select("p, [class*='date'], time"):
                parsed = parse_french_date(el.get_text(strip=True))
                if parsed:
                    date_str = parsed
                    break
            if not date_str:
                continue
            url = href if href.startswith("http") else base + href
            img_el = card.select_one("img[src]")
            img_url = None
            if img_el:
                src = img_el.get("src", "")
                if src.startswith("http"):
                    img_url = _normalize_image_url(src)
                elif src.startswith("/"):
                    img_url = _normalize_image_url(base + src)
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                if img_url:
                    ev["image_url"] = img_url
                events.append(ev)
        if events:
            print(f"  → MATISSE: {len(events)} events")
        return events

    # Generic: look for <article> or <li> with a <time datetime="..."> and a heading
    if not events:
        for card in soup.select("article, li.event, li.evenement, .event-card, .agenda-item"):
            title_el = card.select_one("h1, h2, h3, h4, .title, .event-title")
            time_el = card.select_one("time[datetime]")
            if not title_el or not time_el:
                continue
            title = title_el.get_text(strip=True)
            date_str = time_el.get("datetime", "")
            if not date_str or not re.search(r"20\d{2}", date_str):
                continue
            link_el = card.select_one("a[href]")
            url = link_el["href"] if link_el else site["url"]
            if not url.startswith("http"):
                base = re.match(r"https?://[^/]+", site["url"])
                url = (base.group(0) if base else "") + url
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                events.append(ev)

    return events

# ─── Main ──────────────────────────────────────────────────────────────────────

async def _og_image(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """Fetch the og:image from an event detail page."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
            if r.status != 200:
                return None
            text = await r.text(errors="ignore")
            # og:image can appear in either attribute order
            m = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                text, re.IGNORECASE,
            ) or re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                text, re.IGNORECASE,
            )
            if m:
                return _normalize_image_url(m.group(1))
    except Exception:
        pass
    return None


async def _unsplash(category: str, session: aiohttp.ClientSession) -> Optional[str]:
    if not UNSPLASH_ACCESS_KEY:
        return None
    query = _CAT_QUERIES.get(category, _CAT_QUERIES["AUTRE"])
    try:
        url = f"https://api.unsplash.com/search/photos?query={query}&per_page=3&orientation=landscape"
        async with session.get(
            url,
            headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status == 200:
                data = await r.json()
                results = data.get("results", [])
                if results:
                    return _normalize_image_url(results[0].get("urls", {}).get("regular"))
    except Exception:
        pass
    return None


async def enrich_images(events: list) -> None:
    """
    Two-pass image enrichment:
      1. Fetch og:image from each event's detail page (async, capped at 30).
      2. Unsplash fallback keyed by category (at most ~10 API calls).
    """
    for ev in events:
        ev["image_url"] = _normalize_image_url(ev.get("image_url"))

    no_img = [e for e in events if not e.get("image_url")]
    print(f"\nImage enrichment: {len(no_img)}/{len(events)} events without image")

    connector = aiohttp.TCPConnector(limit=10)
    ua = "Mozilla/5.0 (compatible; NiceEventsScraper/1.0)"
    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": ua}) as session:

        # Pass 1 — detail page og:image (only URLs with a real slug, not site roots)
        fetchable = [
            e for e in no_img
            if e.get("source_url", "").startswith("http")
            and re.search(r"/[^/?#]{3,}/[^/?#]{3,}", e["source_url"])
        ][:30]
        if fetchable:
            images = await asyncio.gather(*[_og_image(e["source_url"], session) for e in fetchable])
            hits = sum(1 for img in images if img)
            for ev, img in zip(fetchable, images):
                if img:
                    ev["image_url"] = img
            print(f"  → {hits} images from detail pages ({len(fetchable)} fetched)")

        # Pass 2 — Unsplash by category for anything still missing
        unsplash_cache: dict = {}
        unsplash_hits = 0
        for ev in events:
            if not ev.get("image_url"):
                cat = ev.get("category", "AUTRE")
                if cat not in unsplash_cache:
                    unsplash_cache[cat] = await _unsplash(cat, session)
                if unsplash_cache.get(cat):
                    ev["image_url"] = unsplash_cache[cat]
                    unsplash_hits += 1
        if UNSPLASH_ACCESS_KEY:
            print(f"  → {unsplash_hits} images from Unsplash")
        else:
            still_missing = sum(1 for e in events if not e.get("image_url"))
            print(f"  → Unsplash disabled (no key) — {still_missing} events still without image")


async def main():
    print(f"Scrape started {datetime.now(timezone.utc).isoformat()}Z")
    all_events: list = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        page = await ctx.new_page()

        for site in SITES:
            try:
                events = await scrape_site(page, site)
                all_events.extend(events)
            except Exception as e:
                print(f"  ERROR {site['source']}: {e}")

        await browser.close()

    now_ms = int(time.time() * 1000)
    future = [e for e in all_events if e["starts_at"] > now_ms - 3_600_000]

    # Remove events from sources where >60% share the same timestamp (bad date extraction)
    by_source: dict = {}
    for ev in future:
        by_source.setdefault(ev["source"], []).append(ev)
    filtered: list = []
    for src, evs in by_source.items():
        if len(evs) < 3:
            filtered.extend(evs)
            continue
        ts_counts = Counter(e["starts_at"] for e in evs)
        top_ts, top_count = ts_counts.most_common(1)[0]
        if top_count >= max(3, len(evs) * 0.6):
            valid = [e for e in evs if ts_counts[e["starts_at"]] < 3]
            print(f"  ⚠ {src}: {top_count}/{len(evs)} events share ts {top_ts} → discarded (bad date), kept {len(valid)}")
            filtered.extend(valid)
        else:
            filtered.extend(evs)
    future = filtered

    seen_keys: set = set()
    deduped = []
    for ev in future:
        key = f"{ev['title'].lower()[:40]}_{ev['starts_at'] // 86_400_000}"
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(ev)

    deduped.sort(key=lambda e: e["starts_at"])

    await enrich_images(deduped)

    output = {
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(deduped),
        "events": deduped,
    }
    with open("events.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}")
    print(f"Total: {len(deduped)} events")
    breakdown: dict = {}
    for ev in deduped:
        breakdown[ev["source"]] = breakdown.get(ev["source"], 0) + 1
    for src, n in sorted(breakdown.items()):
        print(f"  {src}: {n}")

if __name__ == "__main__":
    asyncio.run(main())
