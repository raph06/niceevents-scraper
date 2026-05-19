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
from dateutil.relativedelta import relativedelta
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
    {"source": "OT_NICE",          "city": "Nice",           "url": "https://www.explorenicecotedazur.com/evenements/agenda-de-la-semaine/"},
    {"source": "CANNES",           "city": "Cannes",         "url": "https://www.cannes.com/fr/agenda/agenda-recherche-filtree.html"},
    {"source": "ANTIBES",          "city": "Antibes",        "url": "https://www.antibes-juanlespins.com/information/agenda/evenements-ponctuels"},
    {"source": "MENTON",           "city": "Menton",         "url": "https://www.menton-riviera-merveilles.fr/sorganiser/agenda/tout-lagenda/"},
    {"source": "CAGNES",           "city": "Cagnes-sur-Mer", "url": "https://ville.cagnes.fr/les-evenements/"},
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
    "janvier": 1, "jan": 1, "jan.": 1,
    "février": 2, "fevrier": 2, "fév": 2, "fev": 2, "fév.": 2, "fev.": 2,
    "mars": 3, "mar": 3, "mar.": 3,
    "avril": 4, "avr": 4, "avr.": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7, "juil": 7, "juil.": 7,
    "août": 8, "aout": 8, "aoû": 8, "aou": 8,
    "septembre": 9, "sept": 9, "sep": 9, "sept.": 9, "sep.": 9,
    "octobre": 10, "oct": 10, "oct.": 10,
    "novembre": 11, "nov": 11, "nov.": 11,
    "décembre": 12, "decembre": 12, "déc": 12, "dec": 12, "déc.": 12, "dec.": 12,
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
    # With explicit year: "23 mai 2026" or "1 déc. 2026" (period after abbrev month)
    m = re.search(r"(\d{1,2})\s+(\w+\.?)\s+(20\d{2})", text)
    if m:
        day, month_str, year = m.group(1), m.group(2), m.group(3)
        month = _FRENCH_MONTHS.get(month_str)
        if month:
            return f"{year}-{month:02d}-{int(day):02d}{time_suffix}"
    # Without year: "12 mai" or "2 déc." — infer current or next year
    m = re.search(r"(\d{1,2})\s+(\w+\.?)", text)
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

_SSL_BROKEN_HOSTS = {"le109.nice.fr"}

def _normalize_image_url(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url:
        return None
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    elif url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("https://"):
        return None
    # Proxy images from hosts whose TLS cert Android doesn't trust
    try:
        host = url.split("/")[2]
        if host in _SSL_BROKEN_HOSTS:
            url = "https://wsrv.nl/?url=" + url[len("https://"):]
    except IndexError:
        pass
    return url

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

    # LE109: enrich with detail-page time (itemprop startDate) and full-resolution image
    # Uses the existing Playwright page (same session/cookies/UA as the listing scrape).
    if site["source"] == "LE109" and events:
        time_ok = img_ok = 0
        for ev in events:
            try:
                await page.goto(ev["source_url"], wait_until="domcontentloaded", timeout=15_000)
                detail = BeautifulSoup(await page.content(), "lxml")
                time_el = detail.select_one("time[itemprop='startDate'][datetime]")
                if time_el:
                    ts = parse_ms(time_el.get("datetime"))
                    if ts:
                        ev["starts_at"] = ts
                        ev["ends_at"] = ts + 7_200_000
                        ev["has_time"] = True
                        time_ok += 1
                img_el = detail.select_one("figure img[src]")
                src = img_el.get("src") if img_el else None
                img = _normalize_image_url(
                    "https://le109.nice.fr" + src if src and src.startswith("/") else src
                )
                if not img:
                    meta_img = detail.select_one("meta[itemprop='image'][content]")
                    img = _normalize_image_url(meta_img.get("content")) if meta_img else None
                if img:
                    ev["image_url"] = img
                    img_ok += 1
            except Exception as e:
                print(f"  LE109 detail error ({ev['source_url']}): {e}")
        print(f"  LE109 detail enrichment: {time_ok} with time, {img_ok} with image")

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

    # explorenicecotedazur.com — IRIS/wp-etourisme-v3 SSR WordPress
    if source == "OT_NICE":
        base = "https://www.explorenicecotedazur.com"
        block = soup.select_one("div.wpet-block-list")
        title_els = block.select("h2.iris-card__content__title") if block else []
        print(f"  OT_NICE: {len(title_els)} event cards")
        for h2 in title_els:
            link_el = h2.select_one("a[href*='/evenement/']")
            if not link_el:
                continue
            href = link_el.get("href", "")
            url = href if href.startswith("http") else base + href
            title = h2.get_text(strip=True)
            if not title or len(title) < 2:
                continue
            content = h2.parent.parent  # iris-card__content
            content_rows = content.select("div.wp-block-wpet-card-template-content-row")
            date_text = content_rows[0].get_text(separator=" ", strip=True) if content_rows else ""
            # Date range "23 sept 2025 30 juin 2026" — iterate all matches, keep last (end date = future)
            date_str = None
            for m in re.finditer(r'\d{1,2}\s+\w+\.?\s+20\d{2}', date_text):
                parsed = parse_french_date(m.group(0))
                if parsed:
                    date_str = parsed
            if not date_str:
                date_str = parse_french_date(date_text)
            if not date_str:
                continue
            place = content_rows[2].get_text(strip=True) if len(content_rows) > 2 else site["city"]
            wrapper = content.parent  # iris-card__wrapper
            img_el = wrapper.select_one("div.iris-card__media img, img")
            img_url = None
            if img_el:
                src = img_el.get("data-src") or img_el.get("src", "")
                if src and not src.startswith("data:"):
                    img_url = _normalize_image_url(src if src.startswith("http") else base + src)
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                ev["place"] = place or site["city"]
                if img_url:
                    ev["image_url"] = img_url
                events.append(ev)
        if events:
            print(f"  → OT_NICE: {len(events)} events")
        return events

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
            # Date + time: scan all lines in one pass
            # Time ("20h30") and date ("Le vendredi 22 mai 2026") may be on separate lines
            date_str = None
            _time_only_re = re.compile(r'^(\d{1,2})[h:](\d{0,2})\s*$')
            time_suffix = None
            for line in lines:
                if date_str is None:
                    parsed = parse_french_date(line)
                    if parsed:
                        date_str = parsed
                if time_suffix is None:
                    tm = _time_only_re.match(line.strip())
                    if tm:
                        h, m = int(tm.group(1)), int(tm.group(2)) if tm.group(2) else 0
                        if 0 <= h <= 23 and 0 <= m <= 59:
                            time_suffix = f"T{h:02d}:{m:02d}:00"
            if not date_str:
                continue
            if time_suffix and "T" not in date_str:
                date_str += time_suffix
            # Venue/description: first non-date, non-price, non-title, non-time, non-stopword line
            _LE109_STOP = {"le", "la", "les", "du", "de", "au", "aux", "un", "une", "des", "en", "et", "à"}
            description = ""
            for line in lines:
                if (line == title or _date_hint.search(line)
                        or re.match(r"^\d+\s*€?$", line)
                        or _time_only_re.match(line.strip())
                        or line.lower() in _LE109_STOP
                        or len(line) < 3):
                    continue
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

    # nikaia.fr — <article class="bloc-event"> with <h1 itemprop="name"> title,
    # <time itemprop="startDate"> date, and <meta itemprop="image"> for per-event image.
    if source == "NIKAIA":
        base = "https://www.nikaia.fr"
        for article in soup.select("article.bloc-event, article[class*='bloc-event'], article[class*='event']"):
            # Title lives in h1[itemprop="name"] on nikaia.fr (not h2/h3)
            title_el = article.select_one("h1[itemprop='name'], h1, h2, h3, h4, .event-title, [class*='title']")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title or len(title) < 2:
                continue
            date_str = None
            date_el = article.select_one("time[datetime], [itemprop='startDate'], [class*='date']")
            if date_el:
                date_str = date_el.get("datetime") or date_el.get("content") or parse_french_date(date_el.get_text(strip=True))
            if not date_str:
                for el in article.select("p, span, div"):
                    date_str = parse_french_date(el.get_text(strip=True))
                    if date_str:
                        break
            if not date_str:
                continue
            link_el = article.select_one("a[href]")
            url = site["url"]
            if link_el:
                href = link_el.get("href", "")
                url = href if href.startswith("http") else base + href
            # Per-event image: prefer itemprop="image" meta tag inside the article (absolute URL)
            image_url = None
            itemprop_img = article.select_one('meta[itemprop="image"]')
            if itemprop_img:
                img_content = itemprop_img.get("content", "")
                if img_content and "nikaia-share-default" not in img_content:
                    image_url = _normalize_image_url(img_content)
            # Fallback: per-event thumbnail <img> inside .imageholder (relative URL)
            if image_url is None:
                img_el = article.select_one(".imageholder img, img[src]")
                if img_el:
                    src = img_el.get("src", "")
                    if src and "nikaia-share-default" not in src:
                        if src.startswith("http"):
                            image_url = _normalize_image_url(src)
                        elif src.startswith("/"):
                            image_url = _normalize_image_url(base + src)
            # If both paths returned the default placeholder, image_url stays None
            ev = _make_event(title, date_str, {}, site, seen)
            if ev:
                ev["source_url"] = url
                if image_url:
                    ev["image_url"] = image_url
                events.append(ev)
        if events:
            print(f"  → NIKAIA: {len(events)} events")
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


_TNN_MONTHS_URL = [
    "janvier", "fevrier", "mars", "avril", "mai", "juin",
    "juillet", "aout", "septembre", "octobre", "novembre", "decembre",
]

_TNN_CAT_MAP = {
    "théâtre": "THEATRE", "theatre": "THEATRE",
    "danse": "AUTRE", "musique": "CONCERT", "concert": "CONCERT",
    "jeune public": "FAMILLE", "famille": "FAMILLE",
    "lecture": "THEATRE", "exposition": "EXPO",
}

def _tnn_category(breadcrumb: str, title: str) -> str:
    lo = breadcrumb.lower()
    for key, val in _TNN_CAT_MAP.items():
        if key in lo:
            return val
    return infer_category(title)

async def scrape_tnn() -> list:
    """
    TNN (Théâtre National de Nice) — server-side rendered, no JS needed.
    Phase 1: collect unique event URLs from monthly calendar pages.
    Phase 2: fetch each detail page, emit one event per performance date.
    """
    BASE = "https://www.tnn.fr"
    UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    _WEEKDAY_RE = re.compile(
        r"\b(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b", re.IGNORECASE
    )
    _DATE_LINE_RE = re.compile(r"\d{1,2}\s+\w+\s+20\d{2}", re.IGNORECASE)
    _TIME_RE = re.compile(r"\b(\d{1,2})h(\d{2})?\b")

    now = datetime.now(timezone.utc)
    calendar_urls = []
    for delta in range(13):
        target = now + relativedelta(months=delta)
        m_name = _TNN_MONTHS_URL[target.month - 1]
        calendar_urls.append((f"{BASE}/fr/calendrier/{m_name}/{target.year}", target.year))

    connector = aiohttp.TCPConnector(limit=5)
    headers = {"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"}
    unique_event_urls: dict = {}
    all_events: list = []
    seen_keys: set = set()

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        # Phase 1: collect unique event URLs from monthly listings
        for cal_url, _ in calendar_urls:
            try:
                async with session.get(cal_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        continue
                    html = await r.text(errors="ignore")
            except Exception as e:
                print(f"  TNN listing error {cal_url}: {e}")
                continue
            if "Aucun spectacle" in html or "aucun spectacle" in html:
                continue
            soup = BeautifulSoup(html, "lxml")
            for link in soup.select("a[href*='/spectacles/'], a[href*='/evenements/']"):
                href = link.get("href", "")
                if not href:
                    continue
                if re.search(r"/(spectacles|evenements)/?$", href):
                    continue
                if re.search(r"/spectacles/saison-\d{4}-\d{4}/?$", href):
                    continue
                url = href if href.startswith("http") else BASE + href
                if url in unique_event_urls:
                    continue
                title_el = link.select_one("h3, h2, h4")
                title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
                title = title.strip()
                if not title or len(title) < 3:
                    continue
                img_el = link.select_one("img[src]")
                img_raw = img_el.get("src", "") if img_el else ""
                img_url = _normalize_image_url(
                    img_raw if img_raw.startswith("http") else (BASE + img_raw if img_raw.startswith("/") else None)
                )
                unique_event_urls[url] = (title, img_url)

        print(f"  TNN Phase 1: {len(unique_event_urls)} unique event URLs")
        if not unique_event_urls:
            return []

        # Phase 2: fetch detail pages, emit one event per performance date
        for event_url, (listing_title, listing_img) in unique_event_urls.items():
            try:
                async with session.get(event_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        continue
                    html = await r.text(errors="ignore")
            except Exception as e:
                print(f"  TNN detail error {event_url}: {e}")
                continue
            soup = BeautifulSoup(html, "lxml")

            h1 = soup.select_one("h1")
            title = h1.get_text(strip=True) if h1 else listing_title

            cat_raw = ""
            for sel in [".breadcrumb", "[class*='breadcrumb']", "[class*='categori']", "nav"]:
                el = soup.select_one(sel)
                if el:
                    cat_raw = el.get_text(separator=" ", strip=True)
                    if cat_raw:
                        break
            category = _tnn_category(cat_raw, title)

            desc = ""
            for sel in ["[class*='description']", "[class*='synopsis']", "[class*='texte']", "article p"]:
                el = soup.select_one(sel)
                if el:
                    desc = el.get_text(strip=True)[:400]
                    if desc:
                        break

            img_url = listing_img
            slider_el = soup.select_one("img[src*='-slider'], img[src*='slider']")
            if slider_el:
                src = slider_el.get("src", "")
                img_url = _normalize_image_url(
                    src if src.startswith("http") else (BASE + src if src.startswith("/") else None)
                ) or img_url
            if not img_url:
                og = soup.find("meta", property="og:image")
                if og:
                    img_url = _normalize_image_url(og.get("content"))

            place = "Théâtre National de Nice"
            for sel in ["[class*='lieu']", "[class*='salle']", "[class*='venue']"]:
                el = soup.select_one(sel)
                if el:
                    t = el.get_text(strip=True)
                    if t and len(t) < 80:
                        place = t
                        break

            price_raw = ""
            for sel in ["[class*='tarif']", "[class*='prix']", "[class*='price']"]:
                el = soup.select_one(sel)
                if el:
                    price_raw = el.get_text(separator=" ", strip=True)
                    if price_raw:
                        break
            if not price_raw:
                for tag in soup.find_all(["p", "li", "span"], limit=100):
                    t = tag.get_text(strip=True)
                    if re.search(r"€|\blibre\b|\bgratuit\b|\btarif\b", t, re.IGNORECASE) and len(t) < 200:
                        price_raw = t
                        break
            price, price_val = parse_price(price_raw)

            # Extract all (date, optional time) pairs from the detail page
            all_text_blocks = []
            for el in soup.find_all(["p", "li", "span", "div", "strong", "time"]):
                t = el.get_text(strip=True)
                if t and len(t) < 150:
                    all_text_blocks.append(t)

            performance_dates: list = []
            i = 0
            while i < len(all_text_blocks):
                block = all_text_blocks[i]
                date_match = _DATE_LINE_RE.search(block)
                if date_match:
                    date_only = _WEEKDAY_RE.sub("", date_match.group(0)).strip()
                    date_iso = parse_french_date(date_only)
                    if date_iso:
                        time_iso = None
                        for j in range(i, min(i + 3, len(all_text_blocks))):
                            tm = _TIME_RE.search(all_text_blocks[j])
                            if tm:
                                h = int(tm.group(1))
                                m = int(tm.group(2)) if tm.group(2) else 0
                                if 0 <= h <= 23:
                                    time_iso = f"T{h:02d}:{m:02d}:00"
                                    break
                        full_dt = date_iso + (time_iso or "")
                        performance_dates.append(full_dt)
                i += 1

            performance_dates = list(dict.fromkeys(performance_dates))
            if not performance_dates:
                print(f"  TNN: no dates found for {event_url}")
                continue

            for date_str in performance_dates:
                dedup_key = f"{event_url}#{date_str}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                starts_at = parse_ms(date_str)
                if not starts_at or starts_at < _NOW_MS - 3_600_000:
                    continue
                has_time = "T" in date_str
                all_events.append({
                    "title": title,
                    "description": desc,
                    "place": place,
                    "city": "Nice",
                    "starts_at": starts_at,
                    "ends_at": starts_at + 7_200_000,
                    "has_time": has_time,
                    "price": price,
                    "price_val": price_val,
                    "source": "TNN",
                    "source_url": event_url,
                    "image_url": img_url,
                    "category": category,
                })

    print(f"  TNN: {len(all_events)} events from {len(unique_event_urls)} productions")
    return all_events


async def main():
    print(f"Scrape started {datetime.now(timezone.utc).isoformat()}Z")
    all_events: list = []

    # TNN: server-side rendered — runs with plain aiohttp, no browser needed
    try:
        tnn_events = await scrape_tnn()
        all_events.extend(tnn_events)
    except Exception as e:
        print(f"  ERROR TNN: {e}")

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
        key = f"{ev['title'].lower()[:40]}_{ev['starts_at'] // 3_600_000}"
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(ev)

    _TITLE_BLOCKLIST = re.compile(r'séniors?|seniors?', re.IGNORECASE)
    deduped = [e for e in deduped if not _TITLE_BLOCKLIST.search(e.get("title", ""))]

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
