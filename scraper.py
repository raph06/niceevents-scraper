#!/usr/bin/env python3
"""
NiceEvents backend scraper
Runs on GitHub Actions every 3h, outputs events.json served via GitHub Pages.
"""

import asyncio
import json
import re
import time
from collections import Counter
from datetime import datetime
from typing import Optional

import dateutil.parser
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page

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

def parse_ms(s) -> Optional[int]:
    if not s or not isinstance(s, str):
        return None
    try:
        return int(dateutil.parser.parse(s, dayfirst=True).timestamp() * 1000)
    except Exception:
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

def _img(obj: dict) -> Optional[str]:
    for k in ["image", "imageUrl", "thumbnail", "photo", "cover", "picture"]:
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict):
            u = v.get("url") or v.get("src") or v.get("contentUrl")
            if isinstance(u, str) and u.startswith("http"):
                return u
    return None

# ─── JSON event extraction ─────────────────────────────────────────────────────

_NOW_MS = int(time.time() * 1000)

def _make_event(title, date_str, obj, site, seen) -> Optional[dict]:
    if title in seen:
        return None
    starts_at = parse_ms(date_str)
    if not starts_at or starts_at < _NOW_MS - 3_600_000:
        return None
    end_str = _str(obj, "endDate", "end_date", "dateFin", "endsAt", "end_at")
    ends_at = parse_ms(end_str) or (starts_at + 7_200_000)
    loc = obj.get("location") or obj.get("lieu") or obj.get("place") or obj.get("venue")
    if isinstance(loc, dict):
        place = loc.get("name") or loc.get("nom") or site["city"]
    elif isinstance(loc, str) and loc.strip():
        place = loc
    else:
        place = site["city"]
    url = _str(obj, "url", "link", "lien", "permalink", "href") or site["url"]
    if not url.startswith("http"):
        url = site["url"]
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

    # Strategy 2: Network-intercepted API responses
    for resp in captured:
        events.extend(walk(resp, site, seen))
    if events:
        print(f"  → API intercept: {len(events)} events")
        return events

    # Strategy 3: BeautifulSoup CSS selectors (static/SSR pages)
    events = scrape_html(soup, site, seen)
    if events:
        print(f"  → HTML selectors: {len(events)} events")
        return events

    print("  → 0 events")
    return []


def scrape_html(soup: BeautifulSoup, site: dict, seen: set) -> list:
    """CSS selector fallback for sites with known HTML structure."""
    events = []
    source = site["source"]

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

async def main():
    print(f"Scrape started {datetime.utcnow().isoformat()}Z")
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

    output = {
        "scraped_at": datetime.utcnow().isoformat() + "Z",
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
