
import re, csv, json, asyncio, argparse, random
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

# Chatbot detection keywords
CHATBOTS = {
    "intercom":  ["intercom","widget.intercom.io"],
    "drift":     ["drift.com","js.driftt.com"],
    "tidio":     ["tidio","code.tidio.co"],
    "livechat":  ["livechatinc.com","__lc."],
    "zendesk":   ["zopim","zendesk"],
    "hubspot":   ["hs-scripts.com","hubspot"],
    "tawk":      ["tawk.to"],
    "crisp":     ["crisp.chat"],
    "freshchat": ["freshchat"],
    "olark":     ["olark"],
    "podium":    ["podium.com"],
}

# Email detection keywords
JUNK_EMAILS = {
    "example.com","sentry.io","schema.org","google.com","facebook.com",
    "twitter.com","instagram.com","wixpress.com","squarespace.com",
    "cloudflare.com","sendgrid.net","mailchimp.com","amazonaws.com",
    "tripadvisor.com","booking.com","expedia.com","yelp.com",
    "hotels.com","kayak.com","priceline.com","orbitz.com",
}

# Hotel website page keywords
CRAWL_KEYWORDS = {
    "contact":   ["contact","contact-us","reach-us","find-us","get-in-touch","connect"],
    "about":     ["about","about-us","our-story","overview","who-we-are","our-hotel","history"],
    "amenities": ["amenities","facilities","features","services","hotel-features"],
    "rooms":     ["rooms","accommodations","suites","guestrooms","room-types","stay","lodging"],
    "dining":    ["dining","restaurant","food","eat","bar","lounge","cuisine","drink","menu"],
    "spa":       ["spa","wellness","fitness","gym","pool","recreation","health","beauty"],
    "meetings":  ["meetings","events","conference","weddings","corporate","banquet","groups"],
    "team":      ["team","staff","management","our-team","leadership","people","bios"],
}

# Contact info keywords
STAFF_TITLES = [
    "general manager","gm","owner","proprietor","director of",
    "hotel manager","property manager","operations manager",
    "sales manager","revenue manager","guest relations",
    "front desk manager","innkeeper","managing director",
]


# Google maps scrape
async def scrape_google_maps(city: str, max_hotels: int, page) -> list[dict]:
    print(f"\n[1/3] Google Maps — clicking through hotels in: {city}")

    await page.goto(
        f"https://www.google.com/maps/search/hotels+in+{city.replace(' ', '+')}",
        wait_until="domcontentloaded", timeout=30000
    )
    await page.wait_for_timeout(3000)

    try:
        await page.wait_for_selector('div[role="feed"]', timeout=12000)
    except Exception:
        print("  [!] Google Maps sidebar not found — try running again")
        return []

    # Scroll sidebar to load cards
    print("  Scrolling sidebar to load hotel cards...")
    seen_hrefs, stall, prev = set(), 0, 0
    while stall < 4:
        cards = await page.query_selector_all('div[role="feed"] a[href*="/maps/place/"]')
        count = 0
        for c in cards:
            h = await c.get_attribute("href") or ""
            if h and h not in seen_hrefs:
                seen_hrefs.add(h); count += 1
        if count == prev:
            stall += 1
        else:
            stall = 0; prev = count
        if count >= max_hotels * 2:
            break
        await page.evaluate('var f=document.querySelector(\'div[role="feed"]\');if(f)f.scrollBy(0,900);')
        await page.wait_for_timeout(1500)

    print(f"  Found ~{len(seen_hrefs)} cards — clicking each one...")
    hotels, processed = [], set()

    for i in range(min(max_hotels, len(seen_hrefs))):
        fresh = await page.query_selector_all('div[role="feed"] a[href*="/maps/place/"]')
        if i >= len(fresh):
            break
        card = fresh[i]
        href = await card.get_attribute("href") or ""
        if href in processed:
            continue
        processed.add(href)

        try:
            await card.scroll_into_view_if_needed()
            await page.wait_for_timeout(300)
            await card.click()
            await page.wait_for_timeout(2500)
        except Exception:
            try:
                await page.evaluate("arguments[0].click()", card)
                await page.wait_for_timeout(2500)
            except Exception:
                continue

        hotel = await _extract_panel(page, city, href)
        if hotel:
            stars  = f"{'★'*hotel['star_rating']}" if hotel.get('star_rating') else "?"
            phone  = "📞✓" if hotel.get('phone') else "📞—"
            web    = "🌐✓" if hotel.get('website_url') else "🌐—"
            print(f"  [{i+1}] {hotel['property_name'][:40]:40} {stars:6} {phone} {web}")
            hotels.append(hotel)
        else:
            print(f"  [{i+1}] — could not extract")

        await asyncio.sleep(random.uniform(0.8, 1.4))

    print(f"\n  [✓] Collected {len(hotels)} hotels from Google Maps")
    return hotels


async def _extract_panel(page, city: str, maps_url: str) -> dict | None:
    h = _blank_hotel(city, maps_url)

    # Name
    for sel in ['h1.DUwDvf','h1[class*="fontHeadlineLarge"]','[data-attrid="title"] span','h1']:
        el = await page.query_selector(sel)
        if el:
            name = (await el.inner_text()).strip()
            if name and len(name) > 2:
                h["property_name"] = name; break
    if not h["property_name"]:
        return None

    # Star rating (e.g. "4-star hotel") 
    for sel in ['span[aria-label*="star hotel" i]','button[aria-label*="star hotel" i]',
                'div.skqShb','span.mgr77e']:
        el = await page.query_selector(sel)
        if el:
            aria = await el.get_attribute("aria-label") or await el.inner_text()
            m = re.search(r"(\d)\s*-?\s*star", aria, re.I)
            if m:
                h["star_rating"] = int(m.group(1)); break

    # Rating + review count scrape
    try:
        # Try the container element first — most reliable
        rating_container = await page.query_selector('div.F7nice')
        if rating_container:
            aria = await rating_container.get_attribute("aria-label") or ""
            full_text = (await rating_container.inner_text()).strip()

            # Extract rating (X.X format between 1.0-5.0)
            rm = re.search(r"(\d\.\d)", full_text)
            if rm:
                try:
                    v = float(rm.group(1))
                    if 1.0 <= v <= 5.0:
                        h["google_rating"] = v
                except Exception:
                    pass

            # Extract review count
            rev_m = re.search(r"[\(\s]([\d,]{2,})[\)\s]", full_text)
            if not rev_m:
                rev_m = re.search(r"([\d,]+)\s+review", aria, re.I)
            if rev_m:
                try:
                    h["google_reviews"] = int(rev_m.group(1).replace(",", ""))
                except Exception:
                    pass

        # Backuo iption: individual span selectors
        if not h.get("google_rating"):
            for sel in ['div.F7nice span[aria-hidden="true"]', 'span.ceNzKf',
                        'span.MW4etd']:
                el = await page.query_selector(sel)
                if el:
                    t = (await el.inner_text()).strip()
                    try:
                        v = float(t)
                        if 1.0 <= v <= 5.0:
                            h["google_rating"] = v; break
                    except Exception:
                        pass

        if not h.get("google_reviews"):
            # The review count element — usually a span inside F7nice after the rating
            for sel in ['div.F7nice span.UY7F9', 'button[jsaction*="review"] span',
                        'span[aria-label*="review" i]']:
                el = await page.query_selector(sel)
                if el:
                    aria = await el.get_attribute("aria-label") or ""
                    t    = (await el.inner_text()).strip()
                    # Remove parentheses
                    raw = re.sub(r"[(),]", "", aria or t).strip()
                    m = re.search(r"(\d+)", raw)
                    if m:
                        v = int(m.group(1))
                        if v > 3:  # filter out "3 stars" etc
                            h["google_reviews"] = v; break
    except Exception:
        pass

    # Address
    for sel in ['button[data-item-id="address"]','button[aria-label*="Address" i]']:
        el = await page.query_selector(sel)
        if el:
            aria = await el.get_attribute("aria-label") or ""
            addr = re.sub(r"^Address[:\s]*", "", aria, flags=re.I).strip()
            if addr:
                h["address"] = addr; break
            t = (await el.inner_text()).strip()
            if t:
                h["address"] = t; break

    # Phone
    for sel in ['button[data-item-id*="phone"]','button[aria-label*="Phone" i]','a[href^="tel:"]']:
        el = await page.query_selector(sel)
        if el:
            aria = await el.get_attribute("aria-label") or ""
            raw  = re.sub(r"^Phone[:\s]*", "", aria, flags=re.I).strip()
            if not raw:
                raw = (await el.get_attribute("href") or "").replace("tel:", "").strip()
            if not raw:
                raw = (await el.inner_text()).strip()
            if raw:
                h["phone"] = _clean_phone(raw)
                h["all_phones"] = [h["phone"]]; break

    # Website
    for sel in ['a[data-item-id="authority"]','a[aria-label*="website" i]']:
        el = await page.query_selector(sel)
        if el:
            href = await el.get_attribute("href") or ""
            if href.startswith("http") and "google.com" not in href:
                h["website_url"] = href; break

    # Property type
    for sel in ['button[jsaction*="category"]','span.DkEaL','button.DkEaL']:
        el = await page.query_selector(sel)
        if el:
            cat = (await el.inner_text()).lower().strip()
            h["property_type"] = _classify_type(h["property_name"], cat); break

    return h


def _blank_hotel(city, maps_url):
    return {
        "property_name": "", "city": city, "address": "",
        "google_rating": None, "google_reviews": None,
        "google_maps_url": maps_url, "star_rating": None,
        "property_type": "independent hotel",
        "website_url": "", "phone": "", "all_phones": [],
        "email": "", "all_emails": [],
        "rooms": None, "room_types": "", "staff_contacts": "",
        "chatbot_status": "unknown", "chatbot_platform": "",
        "has_mobile_checkin": 0, "has_digital_key": 0, "has_smart_locks": 0,
        "has_guest_messaging": 0, "near_airport": 0, "near_tourist": 0, "near_business": 0,
        "has_pool": 0, "has_gym": 0, "has_spa": 0, "has_restaurant": 0, "has_bar": 0,
        "has_breakfast": 0, "has_parking": 0, "has_meeting_rooms": 0,
        "has_wedding_services": 0, "has_airport_shuttle": 0, "has_room_service": 0,
        "has_concierge": 0, "has_laundry": 0, "has_ev_charging": 0,
        "has_accessible_rooms": 0, "is_pet_friendly": 0,
        "min_stay_nights": None, "usp": "", "pages_crawled": "",
    }


#Hotel website scrape

async def crawl_websites(hotels: list[dict], page) -> list[dict]:
    with_sites = sum(1 for h in hotels if h.get("website_url"))
    print(f"\n[2/3] Website crawl — scraping {with_sites} hotel websites...")

    for i, hotel in enumerate(hotels):
        url = (hotel.get("website_url") or "").strip()
        if not url:
            hotel["chatbot_status"] = "none"
            print(f"  [{i+1}/{len(hotels)}] {hotel['property_name'][:40]} — no website")
            continue

        print(f"  [{i+1}/{len(hotels)}] {hotel['property_name'][:40]}")
        try:
            data = await _crawl_site(page, url)
            hotel.update(data)
            em = f" 📧{len(data['all_emails'])}" if data.get("all_emails") else ""
            ph = " 📞" if data.get("all_phones") else ""
            rm = f" 🛏{data['rooms']}" if data.get("rooms") else ""
            print(f"      chatbot={data['chatbot_status']}{em}{ph}{rm}  [{data.get('pages_crawled','home')}]")
        except Exception as e:
            hotel["chatbot_status"] = "unknown"
            print(f"      ✗ {str(e)[:60]}")

        await asyncio.sleep(random.uniform(1, 2))

    return hotels


async def _crawl_site(page, base_url: str) -> dict:
    result = _blank_site()
    base_domain = urlparse(base_url).netloc
    visited, pages_done = set(), []
    all_text = all_html = ""

    async def visit(url: str) -> tuple[str, str]:
        if url in visited or len(visited) >= 10:
            return "", ""
        visited.add(url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=13000)
            await page.wait_for_timeout(1000)
            for sel in [
                "#onetrust-accept-btn-handler",
                "button[id*='accept' i]","button[class*='accept' i]",
                "button:has-text('Accept All')","button:has-text('I Accept')",
                "button:has-text('Got it')","button:has-text('OK')",
                ".cc-btn.cc-allow",".cookie-consent__accept","#cookie-accept",
            ]:
                try:
                    b = await page.query_selector(sel)
                    if b and await b.is_visible():
                        await b.click(); await asyncio.sleep(0.4); break
                except Exception:
                    pass
            return await page.inner_text("body"), await page.content()
        except Exception:
            return "", ""

    t0, h0 = await visit(base_url)
    if not t0:
        return result
    pages_done.append("home")
    all_text += t0 + "\n"
    all_html += h0

    hl = h0.lower()
    for nm, sigs in CHATBOTS.items():
        if any(s in hl for s in sigs):
            result["chatbot_platform"] = nm
            result["chatbot_status"]   = "basic"; break
    if not result["chatbot_platform"]:
        result["chatbot_status"] = "none"

    sub: dict[str, str] = {}
    try:
        for link in await page.query_selector_all("a[href]"):
            href = (await link.get_attribute("href") or "").strip()
            lt   = (await link.inner_text()).lower().strip()
            if not href or href.startswith(("#","mailto:","tel:","javascript:")):
                continue
            full = (href if href.startswith("http") else
                    f"{urlparse(base_url).scheme}://{base_domain}{href}"
                    if href.startswith("/") else urljoin(base_url, href))
            if urlparse(full).netloc != base_domain:
                continue
            hl2 = href.lower()
            for cat, kws in CRAWL_KEYWORDS.items():
                if cat not in sub and any(k in hl2 or k in lt for k in kws):
                    sub[cat] = full; break
    except Exception:
        pass

    for cat, url in list(sub.items())[:8]:
        t, h = await visit(url)
        if t:
            all_text += f"\n\n==={cat.upper()}===\n{t}"
            all_html += h
            pages_done.append(cat)
        await asyncio.sleep(0.3)

    result["pages_crawled"] = ",".join(pages_done)
    tl = all_text.lower()

    #Phones
    phones = []
    for m in re.finditer(r'href=["\']tel:([+\d\s.\-\(\)]{7,20})["\']', all_html, re.I):
        p = _clean_phone(m.group(1))
        if p: phones.insert(0, p)
    for m in re.finditer(r'"telephone"\s*:\s*"([^"]{7,25})"', all_html):
        p = _clean_phone(m.group(1))
        if p: phones.insert(0, p)
    for pat in [r"\+1[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}",
                r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}",
                r"\d{3}[\s.\-]\d{3}[\s.\-]\d{4}"]:
        for m in re.finditer(pat, all_text):
            phones.append(_clean_phone(m.group(0)))
    seen_d, deduped = set(), []
    for p in phones:
        d = re.sub(r"\D", "", p)
        if d not in seen_d and 10 <= len(d) <= 11:
            seen_d.add(d); deduped.append(p)
    result["all_phones"] = deduped[:4]
    if deduped: result["phone"] = deduped[0]

    # Emails
    emails_raw = []
    for m in re.finditer(r'href=["\']mailto:([^"\'?\s]+)["\']', all_html, re.I):
        emails_raw.insert(0, m.group(1).strip())
    for m in re.finditer(r'"email"\s*:\s*"([^"@\s]+@[^"\s]+)"', all_html):
        emails_raw.insert(0, m.group(1).strip())
    for m in re.finditer(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', all_text):
        emails_raw.append(m.group(0))
    # Obfuscated: "info [at] hotel [dot] com"
    for m in re.finditer(
        r'([a-zA-Z0-9._%+\-]+)\s*[\[\(]?\s*at\s*[\]\)]?\s*([a-zA-Z0-9.\-]+)\s*[\[\(]?\s*dot\s*[\]\)]?\s*([a-zA-Z]{2,})',
        all_text, re.I
    ):
        emails_raw.append(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")
    clean_emails, seen_em = [], set()
    for e in emails_raw:
        domain = e.lower().split("@")[-1] if "@" in e else ""
        if domain in JUNK_EMAILS: continue
        if any(j in e.lower() for j in ["noreply","no-reply","donotreply","unsubscribe","placeholder"]): continue
        if e not in seen_em:
            seen_em.add(e); clean_emails.append(e)
    result["all_emails"] = clean_emails[:6]
    if clean_emails: result["email"] = clean_emails[0]

    #Scraping room count using strict detection keywords
    def _rooms_strict(text):
        pats = [
            r'"numberOfRooms"\s*:\s*["\']?(\d{1,4})["\']?',
            r'numberOfRooms[\s:"\']+(\d{1,4})',
            r'roomCount[\s:"\']+(\d{1,4})',
            r'totalRooms[\s:"\']+(\d{1,4})',
            r'(\d{1,4})[- ]room\s+(?:hotel|boutique|property|inn|resort|suite)',
            r'(\d{1,4})\s+(?:well.appointed\s+|elegantly\s+|luxuriously\s+|spacious\s+)?guest\s+rooms?',
            r'(\d{1,4})\s+guestrooms?\b',
            r'(\d{1,4})\s+accommodations?\b',
            r'(\d{1,4})\s+rooms?\s+and\s+suites?',
            r'(\d{1,4})\s+suites?\s+and\s+rooms?',
            r'total\s+of\s+(\d{1,4})\s+(?:rooms?|suites?|keys?)',
            r'(?:features?|offers?|houses?|boasts?)\s+(\d{1,4})\s+(?:guest\s+)?rooms?',
            r'\brooms?\s*[:\-]\s*(\d{1,4})\b',
        ]
        t2 = text.lower()
        for pat in pats:
            m = re.search(pat, t2, re.I | re.M)
            if m:
                try:
                    v = int(m.group(1))
                    if 3 <= v <= 5000: return v
                except Exception:
                    pass
        return None

    title_m = re.search(r'<title>(.*?)</title>', all_html, re.I | re.S)
    meta_m  = re.search(r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)["\']', all_html, re.I)
    if not meta_m:
        meta_m = re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*name=["\']description["\']', all_html, re.I)

    result["rooms"] = (
        _rooms_strict(title_m.group(1) if title_m else "") or
        _rooms_strict(meta_m.group(1) if meta_m else "") or
        _rooms_strict(all_html) or
        _rooms_strict(tl)
    )
    # Google search room counts if not found on website
    rt_kws = [
        "king room","queen room","double room","twin room","single room",
        "deluxe room","superior room","standard room","classic room",
        "junior suite","executive suite","presidential suite","penthouse suite",
        "penthouse","suite","studio suite","family room","connecting room",
        "accessible room","loft","villa","bungalow","cottage",
    ]
    rt_found = []
    for rt in rt_kws:
        if rt in tl and rt.title() not in rt_found:
            rt_found.append(rt.title())
    result["room_types"] = ", ".join(rt_found[:10])

    # Amenities
    def chk(*kws): return 1 if any(k in tl for k in kws) else 0

    result["has_pool"]             = chk("swimming pool","outdoor pool","indoor pool","rooftop pool","heated pool","pool deck","lap pool","plunge pool","our pool")
    result["has_gym"]              = chk("fitness center","fitness centre","fitness room","workout room","exercise room","weight room","gym","24-hour fitness","state-of-the-art fitness")
    result["has_spa"]              = chk("spa","massage","body treatment","wellness center","wellness centre","day spa","full-service spa","spa services","nail salon")
    result["has_restaurant"]       = chk("restaurant","dining room","on-site dining","farm-to-table","our chef","fine dining","casual dining","bistro","brasserie","steakhouse","our kitchen","rooftop restaurant")
    result["has_bar"]              = chk("bar","lounge","cocktail","rooftop bar","craft beer","wine bar","whiskey bar","happy hour","speakeasy","tavern","pub")
    result["has_breakfast"]        = chk("breakfast included","complimentary breakfast","free breakfast","breakfast buffet","full breakfast","continental breakfast","hot breakfast","breakfast daily","breakfast is served")
    result["has_parking"]          = chk("parking","valet","self-park","parking garage","free parking","complimentary parking","covered parking","on-site parking","secure parking")
    result["has_meeting_rooms"]    = chk("meeting room","conference room","boardroom","event space","meeting space","conference center","function room","breakout room","meeting facility")
    result["has_wedding_services"] = chk("wedding","bridal","bride","ceremony","reception","rehearsal dinner","wedding package","wedding venue","elopement")
    result["has_airport_shuttle"]  = chk("airport shuttle","shuttle service","airport transfer","airport transportation","complimentary shuttle","free shuttle to airport")
    result["has_room_service"]     = chk("room service","in-room dining","24-hour room service","order to your room")
    result["has_concierge"]        = chk("concierge","guest services","personal assistant","concierge service","our concierge","24-hour concierge")
    result["has_laundry"]          = chk("laundry","dry cleaning","valet laundry","laundry service","same-day laundry")
    result["has_ev_charging"]      = chk("ev charging","electric vehicle","tesla charger","electric car charging","ev station","charging station")
    result["has_accessible_rooms"] = chk("accessible","wheelchair","ada","mobility impaired","handicap accessible","roll-in shower","accessible room")
    result["is_pet_friendly"]      = chk("pet friendly","pet-friendly","pets allowed","pets welcome","dogs welcome","dogs allowed","pet policy","we love pets","furry friend")
    result["has_mobile_checkin"]   = chk("mobile check-in","mobile checkin","online check-in","contactless check-in","skip the front desk","digital check-in","express check-in")
    result["has_digital_key"]      = chk("digital key","mobile key","smartphone key","keyless entry","room key on phone","virtual key")
    result["has_smart_locks"]      = chk("smart lock","keyless","digital lock","electronic lock","smart door")
    result["has_guest_messaging"]  = chk("text us","whatsapp","sms","guest messaging","two-way messaging","message us","chat with us")

    # Location
    result["near_airport"]  = chk("airport shuttle","minutes from airport","airport transfer","o'hare","midway","lax","jfk","ord","atl","dfw","bna","mco","near the airport")
    result["near_tourist"]  = chk("attractions","sightseeing","tourist","theme park","beach","historic district","entertainment district","music row","broadway","museum","riverwalk","magnificent mile","navy pier","millennium park")
    result["near_business"] = chk("convention center","conference center","business district","financial district","trade show","the loop","merchandise mart","midtown","wall street")

    # Min stay requirements
    m = re.search(r"minimum\s+(?:stay\s+(?:of\s+)?)?(\d+)\s*[-]\s*night", tl)
    if m: result["min_stay_nights"] = int(m.group(1))

    # USP
    for sel in [
        "[class*='hero'] h1","[class*='hero'] h2","[class*='hero'] p",
        "[class*='banner'] h1","[class*='banner'] p",
        "[class*='intro'] p","[class*='welcome'] p",
        "[class*='tagline']","[class*='headline']",
        "main > section > p","main > div > p","h1 + p","h2 + p",
        ".about-text p","#about p","[class*='about'] p",
    ]:
        try:
            for el in await page.query_selector_all(sel):
                t = (await el.inner_text()).strip()
                if (60 < len(t) < 500 and "cookie" not in t.lower()
                        and "javascript" not in t.lower() and "©" not in t):
                    result["usp"] = t; break
            if result["usp"]: break
        except Exception:
            pass

    # Staff contacts
    staff, lines = [], [l.strip() for l in all_text.split("\n") if l.strip()]
    for j, line in enumerate(lines):
        ll = line.lower()
        if any(t in ll for t in STAFF_TITLES):
            title_str = next((t.title() for t in STAFF_TITLES if t in ll), line[:50])
            for offset in [-1, 1, -2, 2]:
                try:
                    cand = lines[j + offset].strip()
                    if re.match(r"^[A-Z][a-z]+(?: [A-Z][a-z]+){1,2}$", cand) and len(cand) > 4:
                        first = cand.split()[0].lower()
                        last  = cand.split()[-1].lower()
                        em    = next((e for e in clean_emails if first in e.lower() or last in e.lower()), "")
                        if cand not in [s["name"] for s in staff]:
                            staff.append({"name": cand, "title": title_str, "email": em}); break
                except Exception:
                    pass
    result["staff_contacts"] = "; ".join(
        f"{s['name']} ({s['title']})" + (f" <{s['email']}>" if s["email"] else "")
        for s in staff[:5]
    )

    return result


def _blank_site():
    return {
        "email": "", "phone": "", "all_emails": [], "all_phones": [],
        "rooms": None, "room_types": "", "staff_contacts": "",
        "chatbot_status": "unknown", "chatbot_platform": "",
        "has_mobile_checkin": 0, "has_digital_key": 0, "has_smart_locks": 0,
        "has_guest_messaging": 0, "near_airport": 0, "near_tourist": 0, "near_business": 0,
        "has_pool": 0, "has_gym": 0, "has_spa": 0, "has_restaurant": 0, "has_bar": 0,
        "has_breakfast": 0, "has_parking": 0, "has_meeting_rooms": 0,
        "has_wedding_services": 0, "has_airport_shuttle": 0, "has_room_service": 0,
        "has_concierge": 0, "has_laundry": 0, "has_ev_charging": 0,
        "has_accessible_rooms": 0, "is_pet_friendly": 0,
        "min_stay_nights": None, "usp": "", "pages_crawled": "",
    }


def _classify_type(name: str, cat: str = "") -> str:
    nl = name.lower()
    chains = [
        "marriott","hilton","hyatt","sheraton","westin","holiday inn","hampton",
        "courtyard","fairfield","radisson","wyndham","best western","ihg",
        "crowne plaza","doubletree","embassy suites","four seasons","ritz",
        "kimpton","omni","loews","autograph","curio","tapestry","intercontinental",
        "indigo","staybridge","candlewood","aloft","le meridien","w hotel",
    ]
    if any(c in nl for c in chains):              return "chain hotel"
    if "boutique" in nl or "boutique" in cat:     return "boutique hotel"
    if any(k in nl for k in ["inn","lodge"]):      return "inn"
    if "b&b" in nl or "bed and breakfast" in nl:  return "bed & breakfast"
    if "apart" in nl:                             return "aparthotel"
    return "independent hotel"


def _clean_phone(raw: str) -> str:
    if not raw: return ""
    d = re.sub(r"\D", "", raw.strip())
    if len(d) == 10: return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    if len(d) == 11 and d[0] == "1": return f"+1 ({d[1:4]}) {d[4:7]}-{d[7:]}"
    return raw.strip()



# Google search room count

async def lookup_rooms_google(hotels: list[dict], page) -> list[dict]:
    """
    For EVERY hotel, search Google for the room count.
    Google search is the most accurate source — it reads from the hotel's
    official knowledge panel and AI Overview. Always overrides website scraping.
    """
    print(f"\n[3/4] Google room lookup — searching all {len(hotels)} hotels...")

    for i, hotel in enumerate(hotels):
        name = hotel["property_name"]
        city = hotel["city"]
        query = f"{name} {city} number of rooms"
        prev = hotel.get("rooms")
        print(f"  [{i+1}/{len(hotels)}] {name[:45]}")

        rooms = await _google_room_search(page, query, name)
        if rooms:
            hotel["rooms"] = rooms
            hotel["rooms_source"] = "google"
            if prev and prev != rooms:
                print(f"      ✓ {rooms} rooms (corrected from {prev})")
            else:
                print(f"      ✓ {rooms} rooms")
        else:
            # Shows none if google doesn't have room count
            hotel["rooms"] = None
            hotel["rooms_source"] = ""
            if prev:
                print(f"      — not found on Google (clearing website value of {prev})")
            else:
                print(f"      — not found")

        await asyncio.sleep(1.5)

    return hotels


async def _google_room_search(page, query: str, hotel_name: str) -> int | None:
    """
    Search Google and extract room count from:
    1. AI Overview / Gemini answer at top of page
    2. Featured snippet / knowledge panel
    3. First search result snippets
    """
    try:
        url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)
    except Exception:
        return None

    try:
        text = await page.inner_text("body")
        html = await page.content()
    except Exception:
        return None

    # Scraping gemini answer box at the top of the page
    ai_selectors = [
        # AI Overview container
        'div[data-attrid*="rooms"]',
        'div[class*="ai-overview"]',
        'div[data-ved] div[data-hveid]',
        # Knowledge panel room count
        'div[data-attrid="hw:/collection/hotels:number_of_rooms"]',
        'span[data-dtld="hw:/collection/hotels:number_of_rooms"]',
        # Featured snippet
        'div.IZ6rdc',
        'div[class*="kp-blk"]',
        'div[data-tts="answers"]',
    ]
    for sel in ai_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                t = (await el.inner_text()).strip()
                v = _extract_room_number(t, hotel_name)
                if v: return v
        except Exception:
            pass

    # If no gemini answer pops up, parse the full page text with strict patterns ────────────
    # Look in the first ~3000 chars (top of results page)
    top_text = text[:3000]
    v = _extract_room_number(top_text, hotel_name)
    if v: return v

    #Fallback: Look for specific Google knowledge panel patterns
    kp_patterns = [
        r'(\d{1,4})\s+rooms?\s*(?:\n|$|\|)',   # "121 rooms" at end of line
        r'Rooms?\s*\n?(\d{1,4})',
        r'(\d{1,4})\s+guest\s+rooms?',             # "121 guest rooms"
        r'Number\s+of\s+rooms?[:\s]+(\d{1,4})',   # "Number of rooms: 121"
    ]
    for pat in kp_patterns:
        m = re.search(pat, text[:4000], re.I | re.M)
        if m:
            try:
                v = int(m.group(1))
                if 3 <= v <= 5000: return v
            except Exception:
                pass

    return None


def _extract_room_number(text: str, hotel_name: str) -> int | None:
    """
    Extract a room count from a snippet of Google search result text.
    Uses strict patterns that require "room/suite/guestroom" context.
    """
    patterns = [
        r'(\d{1,4})\s+guest\s+rooms?',
        r'(\d{1,4})\s+guestrooms?\b',
        r'(\d{1,4})\s+rooms?\s+and\s+suites?',
        r'(\d{1,4})\s+suites?\s+and\s+rooms?',
        r'(\d{1,4})[- ]room\s+(?:hotel|property|boutique)',
        r'total\s+of\s+(\d{1,4})\s+(?:rooms?|suites?)',
        r'(?:has|have|with|featuring)\s+(\d{1,4})\s+(?:guest\s+)?rooms?',
        r'\brooms?:\s*(\d{1,4})\b',
        r'(\d{1,4})\s+accommodations?\b',
        # "121 rooms" at a line boundary (strong signal in knowledge panels)
        r'(\d{1,4})\s+rooms?\s*(?:\n|$)',
    ]
    tl = text.lower()
    for pat in patterns:
        m = re.search(pat, tl, re.I | re.M)
        if m:
            try:
                v = int(m.group(1))
                if 3 <= v <= 5000: return v
            except Exception:
                pass
    return None


# Scoring

def score_hotel(h: dict) -> dict:
    s = {}
    rooms = _toint(h.get("rooms"))
    s["score_rooms"] = (
        15 if rooms and 41  <= rooms <= 120 else
        10 if rooms and 121 <= rooms <= 200 else
        8  if rooms and 21  <= rooms <= 40  else
        4  if rooms and 10  <= rooms <= 20  else
        5  if rooms and rooms > 200         else 0)

    s["score_type"] = {
        "boutique hotel": 10, "independent hotel": 10, "inn": 8,
        "bed & breakfast": 8, "aparthotel": 7, "chain hotel": 2,
    }.get(h.get("property_type", ""), 5)

    # Use star rating 
    stars = _toint(h.get("star_rating"))
    s["score_stars"] = 5 if stars == 3 else 4 if stars == 4 else 3 if stars == 2 else 2

    rv = _toint(h.get("google_reviews")) or 0
    s["score_reviews"] = 8 if rv >= 500 else 5 if rv >= 100 else 2

    s["score_chatbot"] = (
        5 if h.get("chatbot_status") == "none" else
        3 if h.get("chatbot_status") == "basic" else 2)

    tech = (int(h.get("has_mobile_checkin", 0)) * 3 +
            int(h.get("has_digital_key", 0)) * 3 +
            int(h.get("has_smart_locks", 0)) * 2)
    s["score_tech"] = tech

    loc = (int(h.get("near_tourist", 0)) * 4 +
           int(h.get("near_business", 0)) * 3 +
           int(h.get("near_airport", 0)) * 2)
    s["score_location"] = min(loc, 9)

    s["score_contact"] = 3 if (h.get("email") or h.get("all_emails")) else 0

    total = sum(s.values())
    s["lead_score"] = total
    s["priority"]   = "Hot" if total >= 45 else "Warm" if total >= 25 else "Cold"
    return s


def build_outreach(h: dict) -> str:
    """Generate specific, hotel-based outreach reasoning."""
    parts = []
    rooms = _toint(h.get("rooms"))
    stars = _toint(h.get("star_rating"))

    if h.get("chatbot_status") == "none" and h.get("website_url"):
        parts.append("no AI chatbot — guests can't get instant answers 24/7")
    if not int(h.get("has_mobile_checkin", 0)):
        parts.append("no mobile check-in — guests still queue at front desk")
    if rooms and rooms >= 50:
        parts.append(f"{rooms} rooms = high daily guest inquiry volume")
    if int(h.get("has_restaurant", 0)) and not int(h.get("has_concierge", 0)):
        parts.append("has dining but no concierge — AI handles recommendations")
    if int(h.get("has_meeting_rooms", 0)):
        parts.append("meeting/event guests generate complex booking requests")
    if int(h.get("near_tourist", 0)):
        parts.append("tourist location = constant questions about local area")
    if int(h.get("near_business", 0)) and not int(h.get("has_guest_messaging", 0)):
        parts.append("business guests need fast answers — no messaging platform found")
    if not (h.get("email") or h.get("all_emails")):
        parts.append("no email found — reach via phone or Google Maps listing")

    if not parts:
        if stars and stars >= 4:
            parts.append(f"{stars}-star property — guests expect premium, instant responses")
        else:
            parts.append("independent property — AI concierge differentiates from chains")

    return "; ".join(parts[:3])


def build_hotel_summary(h: dict) -> str:
    """Build a specific hotel description from scraped data."""
    # Use USP if it's specific enough
    usp = h.get("usp", "").strip()
    if usp and len(usp) > 40:
        first = re.split(r'[.!?]', usp)[0].strip()
        if len(first) > 30:
            return first[:200]

    # Build from data
    rooms  = _toint(h.get("rooms"))
    stars  = _toint(h.get("star_rating"))
    ptype  = h.get("property_type", "hotel")
    city   = h.get("city", "")
    name   = h.get("property_name", "")

    parts = []
    if rooms:  parts.append(f"{rooms}-room")
    if stars:  parts.append(f"{stars}-star")
    parts.append(ptype)
    if city:   parts.append(f"in {city}")

    ams = []
    if int(h.get("has_pool", 0)):             ams.append("pool")
    if int(h.get("has_spa", 0)):              ams.append("spa")
    if int(h.get("has_restaurant", 0)):       ams.append("restaurant")
    if int(h.get("has_meeting_rooms", 0)):    ams.append("meeting rooms")
    if int(h.get("has_wedding_services", 0)): ams.append("wedding venue")
    if int(h.get("is_pet_friendly", 0)):      ams.append("pet-friendly")
    if ams:
        parts.append("with " + ", ".join(ams[:3]))

    summary = " ".join(parts).strip()
    return summary.capitalize() if summary else f"{ptype.capitalize()} in {city}"


def _toint(v):
    try: return int(float(str(v).replace(",", "")))
    except: return None


# Exporting files

CSV_COLS = [
    "rank","lead_score","priority","property_name","property_type",
    "city","address","phone","all_phones","email","all_emails",
    "website_url","google_maps_url","google_reviews","star_rating",
    "rooms","room_types","staff_contacts","chatbot_status","chatbot_platform",
    "has_mobile_checkin","has_digital_key","has_smart_locks","has_guest_messaging",
    "has_pool","has_gym","has_spa","has_restaurant","has_bar","has_breakfast",
    "has_parking","has_airport_shuttle","has_room_service","has_concierge",
    "has_meeting_rooms","has_wedding_services","has_laundry",
    "has_ev_charging","has_accessible_rooms","is_pet_friendly",
    "near_airport","near_tourist","near_business",
    "min_stay_nights","outreach_angle","hotel_summary","pages_crawled",
]


def _san(v):
    """Sanitize string: strip HTML, collapse whitespace."""
    if not isinstance(v, str): return v
    v = re.sub(r'<[^>]+>', '', v)
    v = re.sub(r'[\r\n\t]+', ' ', v)
    v = re.sub(r'\s{2,}', ' ', v).strip()
    return v.replace('\x00', '')


def export_csv(hotels, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for h in hotels:
            row = {}
            for k in CSV_COLS:
                v = h.get(k, "")
                if isinstance(v, list): v = "; ".join(str(x) for x in v)
                elif v is None: v = ""
                row[k] = v
            w.writerow(row)
    print(f"  [✓] CSV: {path}")


def export_dashboard(hotels, path):
    clean = []
    for h in hotels:
        c = {}
        for k, v in h.items():
            c[k] = [_san(x) if isinstance(x, str) else x for x in v] if isinstance(v, list) else _san(v)
        c["all_emails_str"] = "; ".join(_san(e) for e in (h.get("all_emails") or []))
        c["all_phones_str"] = "; ".join(_san(p) for p in (h.get("all_phones") or []))
        # Guarantee required JS fields always exist
        c["priority"]   = c.get("priority")   or "Cold"
        c["lead_score"] = c.get("lead_score") or 0
        c["rank"]       = c.get("rank")       or 0
        clean.append(c)

    # ensure_ascii=True converts all non-ASCII to \uXXXX — safe in any HTML
    hotel_json = json.dumps(clean, default=str, ensure_ascii=True)

    gen  = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    tot  = len(hotels)
    hot  = sum(1 for h in hotels if h.get("priority") == "Hot")
    warm = sum(1 for h in hotels if h.get("priority") == "Warm")
    cold = sum(1 for h in hotels if h.get("priority") == "Cold")
    wem  = sum(1 for h in hotels if h.get("email") or h.get("all_emails"))
    wrm  = sum(1 for h in hotels if h.get("rooms"))

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_html(hotel_json, gen, tot, hot, warm, cold, wem, wrm))
    print(f"  [✓] Dashboard: {path}")


def _html(hotel_json, gen, tot, hot, warm, cold, wem, wrm):
    
    #Dashboard layout and design
        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unloket Leads</title>
<script id="hdata" type="application/json">{hotel_json}</script>
<style>
:root{{--bg:#0f1117;--s:#1a1d27;--s2:#22263a;--bd:#2e3354;--tx:#e8eaf0;--mu:#8892b0;
  --ac:#7c6ff7;--hot:#ff6b6b;--hotbg:rgba(255,107,107,.13);
  --warm:#ffd166;--warmbg:rgba(255,209,102,.11);
  --cold:#74c0fc;--coldbg:rgba(116,192,252,.11);--gr:#06d6a0}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--tx);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;line-height:1.5}}
.hdr{{background:var(--s);border-bottom:1px solid var(--bd);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}}
.logo{{font-size:19px;font-weight:700}}.logo span{{color:var(--ac)}}
.meta{{color:var(--mu);font-size:12px}}
.stats{{display:flex;gap:10px;padding:14px 24px;border-bottom:1px solid var(--bd);flex-wrap:wrap}}
.stat{{background:var(--s);border:1px solid var(--bd);border-radius:8px;padding:11px 15px;flex:1;min-width:80px}}
.sl{{color:var(--mu);font-size:11px;text-transform:uppercase;letter-spacing:.4px;margin-bottom:2px}}
.sv{{font-size:20px;font-weight:700}}
.ctrl{{padding:11px 24px;background:var(--s);border-bottom:1px solid var(--bd);display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
input,select{{background:var(--bg);border:1px solid var(--bd);border-radius:7px;padding:7px 11px;color:var(--tx);font-size:13px;outline:none}}
input{{flex:1;min-width:150px}}input:focus,select:focus{{border-color:var(--ac)}}
.cnt{{margin-left:auto;color:var(--mu);font-size:12px}}
.xbtn{{background:var(--ac);color:#fff;border:none;border-radius:7px;padding:7px 13px;font-size:13px;font-weight:600;cursor:pointer}}.xbtn:hover{{opacity:.85}}
.wrap{{overflow-x:auto;padding:0 24px 32px}}
table{{width:100%;border-collapse:collapse;margin-top:10px;min-width:900px}}
th{{background:var(--s2);color:var(--mu);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;padding:9px 11px;text-align:left;border-bottom:1px solid var(--bd);cursor:pointer;white-space:nowrap;user-select:none}}
th:hover{{color:var(--tx)}}.sorted{{color:var(--ac)}}
td{{padding:10px 11px;border-bottom:1px solid var(--bd);vertical-align:top}}
tr:hover td{{background:var(--s2);cursor:pointer}}
.hot{{background:var(--hotbg);color:var(--hot);border:1px solid rgba(255,107,107,.3);border-radius:20px;padding:2px 8px;font-size:11px;font-weight:600}}
.warm{{background:var(--warmbg);color:var(--warm);border:1px solid rgba(255,209,102,.3);border-radius:20px;padding:2px 8px;font-size:11px;font-weight:600}}
.cold{{background:var(--coldbg);color:var(--cold);border:1px solid rgba(116,192,252,.3);border-radius:20px;padding:2px 8px;font-size:11px;font-weight:600}}
.sw{{display:flex;align-items:center;gap:6px}}
.sn{{font-weight:700;font-size:15px;min-width:28px}}
.bb{{flex:1;height:5px;background:var(--bd);border-radius:3px;min-width:40px}}
.bf{{height:100%;border-radius:3px}}
.nm{{font-weight:600}}.sub{{color:var(--mu);font-size:12px}}
a{{color:var(--ac);text-decoration:none}}a:hover{{text-decoration:underline}}
.yes{{color:var(--gr)}}.no{{color:var(--mu)}}
.tag-g{{display:inline-block;background:rgba(6,214,160,.12);color:var(--gr);border-radius:4px;padding:2px 7px;font-size:11px;margin:2px}}
.empty{{padding:48px;text-align:center;color:var(--mu)}}
.overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:9999;overflow-y:auto;padding:30px 16px}}
.overlay.open{{display:block}}
.modal{{background:var(--s);border:1px solid var(--bd);border-radius:14px;max-width:700px;margin:0 auto;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.5)}}
.mhd{{padding:20px 24px 16px;border-bottom:1px solid var(--bd);display:flex;align-items:flex-start;justify-content:space-between;gap:12px;position:sticky;top:0;background:var(--s);z-index:1}}
.mtt{{font-size:17px;font-weight:700;line-height:1.3}}
.msb{{color:var(--mu);font-size:12px;margin-top:3px}}
.xcl{{background:none;border:none;color:var(--mu);font-size:24px;cursor:pointer;line-height:1;flex-shrink:0}}.xcl:hover{{color:var(--tx)}}
.mbd{{padding:20px 24px 28px}}
.rb{{display:inline-flex;align-items:center;gap:10px;background:rgba(124,111,247,.12);border:1px solid rgba(124,111,247,.25);border-radius:10px;padding:12px 18px;margin-bottom:14px}}
.rn{{font-size:36px;font-weight:700;color:var(--ac);line-height:1}}
.rl{{font-size:11px;color:var(--mu);line-height:1.5}}
.sg{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:10px 0 4px}}
.si{{background:var(--bg);border-radius:8px;padding:9px 11px}}
.sil{{color:var(--mu);font-size:11px;margin-bottom:2px}}
.siv{{font-size:17px;font-weight:700;color:var(--ac)}}
.sec{{color:var(--mu);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;margin:16px 0 8px;padding-top:13px;border-top:1px solid var(--bd)}}
.dr{{display:flex;gap:10px;margin-bottom:7px;font-size:13px;align-items:flex-start}}
.dl{{color:var(--mu);min-width:140px;flex-shrink:0;padding-top:1px}}
.usp-b{{background:var(--bg);border-left:3px solid var(--ac);padding:10px 14px;border-radius:0 8px 8px 0;font-size:13px;color:var(--mu);font-style:italic;margin:8px 0;line-height:1.6}}
.ang-b{{background:rgba(255,107,107,.07);border-left:3px solid var(--hot);padding:10px 14px;border-radius:0 8px 8px 0;font-size:13px;margin:8px 0;line-height:1.6}}
.ag{{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}}
</style></head><body>
<div class="hdr">
  <div class="logo">Unlo<span>ket</span> <span style="color:var(--mu);font-weight:400;font-size:13px">Lead Dashboard</span></div>
  <div class="meta">Generated {gen} &nbsp;&middot;&nbsp; {tot} hotels &nbsp;&middot;&nbsp;
    <span style="color:var(--hot)">{hot} Hot</span> &nbsp;&middot;&nbsp;
    <span style="color:var(--warm)">{warm} Warm</span> &nbsp;&middot;&nbsp;
    <span style="color:var(--cold)">{cold} Cold</span></div>
</div>
<div class="stats" id="stats"></div>
<div class="ctrl">
  <input type="text" id="search" placeholder="Search by name, city, email, amenity...">
  <select id="tier"><option value="">All tiers</option><option value="Hot">🔥 Hot</option><option value="Warm">🟡 Warm</option><option value="Cold">🔵 Cold</option></select>
  <select id="type"><option value="">All types</option></select>
  <select id="chatbot"><option value="">Any chatbot</option><option value="none">No chatbot ← best leads</option><option value="basic">Has chatbot</option></select>
  <select id="pet"><option value="">Any pet policy</option><option value="1">Pet friendly</option></select>
  <button class="xbtn" onclick="doExport()">&#11015; Export CSV</button>
  <span class="cnt" id="cnt"></span>
</div>
<div class="wrap">
<table>
<thead><tr>
  <th onclick="sortBy('rank')" data-c="rank">#</th>
  <th onclick="sortBy('lead_score')" data-c="lead_score">Score &#8597;</th>
  <th onclick="sortBy('priority')" data-c="priority">Tier</th>
  <th onclick="sortBy('property_name')" data-c="property_name">Hotel</th>
  <th onclick="sortBy('rooms')" data-c="rooms">Rooms</th>
  <th onclick="sortBy('star_rating')" data-c="star_rating">Stars</th>
  <th onclick="sortBy('google_reviews')" data-c="google_reviews">Reviews</th>
  <th data-c="chatbot_status">Chatbot</th>
  <th data-c="email">Email</th>
  <th data-c="phone">Phone</th>
  <th data-c="outreach_angle">Why reach out</th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
<div class="empty" id="empty" style="display:none">No hotels match your filters.</div>
</div>
<div class="overlay" id="overlay">
  <div class="modal">
    <div class="mhd">
      <div><div class="mtt" id="m-name"></div><div class="msb" id="m-sub"></div></div>
      <button class="xcl" onclick="closeModal()">&#x2715;</button>
    </div>
    <div class="mbd" id="m-body"></div>
  </div>
</div>
<script>
var ALL = JSON.parse(document.getElementById('hdata').textContent);
var filtered = ALL.slice();
var sortCol = 'rank', sortAsc = true;

function scoreColor(s) {{ return s>=45?'#ff6b6b':s>=25?'#ffd166':'#74c0fc'; }}
function tierCls(p)    {{ return p==='Hot'?'hot':p==='Warm'?'warm':'cold'; }}
function tierLabel(p)  {{ return p==='Hot'?'&#128293; Hot':p==='Warm'?'&#129505; Warm':'&#128309; Cold'; }}

function stat(l,v,c) {{
  return '<div class="stat"><div class="sl">'+l+'</div><div class="sv" style="color:'+c+'">'+v+'</div></div>';
}}
function drow(l,v) {{
  if(v===null||v===undefined||v==='') return '';
  return '<div class="dr"><span class="dl">'+l+'</span><span>'+v+'</span></div>';
}}

function init() {{
  var types=[], hot=0, warm=0, cold=0, wem=0, wrm=0, avg=0;
  ALL.forEach(function(h) {{
    if(h.property_type && types.indexOf(h.property_type)<0) types.push(h.property_type);
    if(h.priority==='Hot') hot++;
    else if(h.priority==='Warm') warm++;
    else cold++;
    avg += h.lead_score||0;
    if(h.email||h.all_emails_str) wem++;
    if(h.rooms) wrm++;
  }});
  avg = ALL.length ? Math.round(avg/ALL.length) : 0;
  types.sort();
  var sel=document.getElementById('type');
  types.forEach(function(t) {{ var o=document.createElement('option');o.value=t;o.textContent=t;sel.appendChild(o); }});
  document.getElementById('stats').innerHTML =
    stat('Total',ALL.length,'var(--tx)') +
    stat('&#128293; Hot',hot,'var(--hot)') +
    stat('&#129505; Warm',warm,'var(--warm)') +
    stat('&#128309; Cold',cold,'var(--cold)') +
    stat('Avg Score',avg,'var(--ac)') +
    stat('&#128231; Email',wem,'var(--gr)') +
    stat('&#128715; Rooms',wrm,'var(--ac)');
  applySort();
  render();
}}

function applyFilters() {{
  var q=document.getElementById('search').value.toLowerCase();
  var ti=document.getElementById('tier').value;
  var ty=document.getElementById('type').value;
  var cb=document.getElementById('chatbot').value;
  var pt=document.getElementById('pet').value;
  filtered=ALL.filter(function(h) {{
    if(ti && h.priority!==ti) return false;
    if(ty && h.property_type!==ty) return false;
    if(cb && h.chatbot_status!==cb) return false;
    if(pt==='1' && !h.is_pet_friendly) return false;
    if(q) {{
      var hay=[h.property_name,h.city,h.address,h.email,h.all_emails_str,
               h.phone,h.all_phones_str,h.outreach_angle,h.hotel_summary,
               h.room_types,h.staff_contacts].join(' ').toLowerCase();
      if(hay.indexOf(q)<0) return false;
    }}
    return true;
  }});
  applySort(); render();
}}

function sortBy(col) {{
  if(sortCol===col) sortAsc=!sortAsc;
  else {{ sortCol=col; sortAsc=(col==='rank'); }}
  document.querySelectorAll('th').forEach(function(t){{ t.classList.remove('sorted'); }});
  var th=document.querySelector('th[data-c="'+col+'"]');
  if(th) th.classList.add('sorted');
  applySort(); render();
}}

function applySort() {{
  filtered.sort(function(a,b) {{
    var av=a[sortCol], bv=b[sortCol];
    if(av==null) av=sortAsc?1e9:-1e9;
    if(bv==null) bv=sortAsc?1e9:-1e9;
    if(typeof av==='string') return sortAsc?av.localeCompare(bv):bv.localeCompare(av);
    return sortAsc?av-bv:bv-av;
  }});
}}

function render() {{
  document.getElementById('cnt').textContent=filtered.length+' of '+ALL.length+' hotels';
  if(!filtered.length) {{
    document.getElementById('tbody').innerHTML='';
    document.getElementById('empty').style.display='block';
    return;
  }}
  document.getElementById('empty').style.display='none';
  var html='';
  filtered.forEach(function(h,i) {{
    var s=h.lead_score||0, pct=Math.min(s/70*100,100), c=scoreColor(s);
    var idx=ALL.indexOf(h);
    var rooms=h.rooms?'<span style="color:var(--ac);font-weight:700">'+h.rooms+'</span>':'<span class="no">&mdash;</span>';
    var stars=h.star_rating?'&#9733;'.repeat(Math.min(h.star_rating,5)):'&mdash;';
    var em=h.email?'<a href="mailto:'+h.email+'" onclick="event.stopPropagation()" style="font-size:12px">'+h.email+'</a>':'<span class="no">&mdash;</span>';
    var reviews=h.google_reviews?Number(h.google_reviews).toLocaleString():'&mdash;';
    html+='<tr onclick="openModal('+idx+')">'
      +'<td style="color:var(--mu)">'+(h.rank||i+1)+'</td>'
      +'<td><div class="sw"><div class="sn" style="color:'+c+'">'+s+'</div>'
      +'<div class="bb"><div class="bf" style="width:'+pct+'%;background:'+c+'"></div></div></div></td>'
      +'<td><span class="'+tierCls(h.priority)+'">'+tierLabel(h.priority)+'</span></td>'
      +'<td><div class="nm">'+(h.property_name||'&mdash;')+'</div>'
      +'<div class="sub">'+(h.property_type||'')+'&nbsp;&middot;&nbsp;'+(h.city||'')+'</div></td>'
      +'<td>'+rooms+'</td>'
      +'<td>'+stars+'</td>'
      +'<td>'+reviews+'</td>'
      +'<td><span class="'+(h.chatbot_status==='none'?'yes':'no')+'">'+(h.chatbot_status||'&mdash;')+'</span></td>'
      +'<td>'+em+'</td>'
      +'<td style="font-size:12px;white-space:nowrap">'+(h.phone||'&mdash;')+'</td>'
      +'<td style="font-size:12px;color:var(--mu);max-width:200px">'+(h.outreach_angle||'&mdash;')+'</td>'
      +'</tr>';
  }});
  document.getElementById('tbody').innerHTML=html;
}}

function openModal(idx) {{
  var h=ALL[idx]; if(!h) return;
  document.getElementById('m-name').textContent=h.property_name||'';
  document.getElementById('m-sub').textContent=(h.address||h.city||'')+' \u00b7 '+h.priority+' \u00b7 Score: '+(h.lead_score||0);

  var amMap=[
    ['Pool','has_pool'],['Gym','has_gym'],['Spa','has_spa'],
    ['Restaurant','has_restaurant'],['Bar','has_bar'],['Breakfast','has_breakfast'],
    ['Parking','has_parking'],['Airport Shuttle','has_airport_shuttle'],
    ['Room Service','has_room_service'],['Concierge','has_concierge'],
    ['Meeting Rooms','has_meeting_rooms'],['Weddings','has_wedding_services'],
    ['Laundry','has_laundry'],['EV Charging','has_ev_charging'],
    ['Accessible','has_accessible_rooms'],['Pet Friendly','is_pet_friendly'],
    ['Mobile Check-in','has_mobile_checkin'],['Digital Key','has_digital_key'],
    ['Smart Locks','has_smart_locks'],['Guest Messaging','has_guest_messaging'],
  ];
  var ams=amMap.filter(function(x){{return h[x[1]]==1||h[x[1]]===true;}}).map(function(x){{return x[0];}});

  var scores=[
    ['Rooms',h.score_rooms||0],['Type',h.score_type||0],['Stars',h.score_stars||0],
    ['Reviews',h.score_reviews||0],['Chatbot',h.score_chatbot||0],
    ['Tech',h.score_tech||0],['Location',h.score_location||0],['Contact',h.score_contact||0]
  ];

  var emails=(h.all_emails_str||h.email||'').split(';').map(function(x){{return x.trim();}}).filter(Boolean);
  var phones=(h.all_phones_str||h.phone||'').split(';').map(function(x){{return x.trim();}}).filter(Boolean);

  var body='';
  if(h.outreach_angle) body+='<div class="ang-b"><strong>Why reach out:</strong> '+h.outreach_angle+'</div>';
  if(h.hotel_summary)  body+='<div class="usp-b">'+h.hotel_summary+'</div>';

  if(h.rooms) {{
    body+='<div class="rb"><div class="rn">'+h.rooms+'</div><div class="rl">guest<br>rooms</div></div>';
  }} else {{
    body+='<div style="color:var(--mu);font-size:13px;margin-bottom:12px">Room count: not found on website</div>';
  }}

  body+='<div class="sec" style="border-top:none;padding-top:0;margin-top:4px">Score Breakdown</div>';
  body+='<div class="sg">';
  scores.forEach(function(sc){{body+='<div class="si"><div class="sil">'+sc[0]+'</div><div class="siv">'+sc[1]+'</div></div>';}});
  body+='</div>';

  body+='<div class="sec">Contact</div>';
  if(phones.length) body+=drow('Phone(s)',phones.map(function(p){{return '<a href="tel:'+p+'">'+p+'</a>';}}).join(' &middot; '));
  if(emails.length) body+=drow('Email(s)',emails.map(function(e){{return '<a href="mailto:'+e+'">'+e+'</a>';}}).join(' &middot; '));
  if(h.website_url) body+=drow('Website','<a href="'+h.website_url+'" target="_blank">'+h.website_url+'</a>');
  if(h.google_maps_url) body+=drow('Google Maps','<a href="'+h.google_maps_url+'" target="_blank">View on Maps &#8594;</a>');
  if(h.staff_contacts) body+=drow('Staff Found','<span style="font-size:12px">'+h.staff_contacts+'</span>');

  body+='<div class="sec">Property Details</div>';
  body+=drow('Type',h.property_type);
  if(h.star_rating) body+=drow('Stars','&#9733;'.repeat(Math.min(h.star_rating,5)));
  if(h.rooms) body+=drow('Rooms',h.rooms+' guest rooms');
  if(h.room_types) body+=drow('Room Types',h.room_types);
  if(h.google_reviews) body+=drow('Google Reviews',Number(h.google_reviews).toLocaleString());
  if(h.min_stay_nights) body+=drow('Min Stay',h.min_stay_nights+' nights');

  var locArr=[];
  if(h.near_tourist) locArr.push('Tourist area');
  if(h.near_business) locArr.push('Business district');
  if(h.near_airport) locArr.push('Near airport');
  if(locArr.length) body+=drow('Location',locArr.join(', '));

  if(ams.length) {{
    body+='<div class="sec">Amenities Detected ('+ams.length+')</div><div class="ag">';
    ams.forEach(function(a){{body+='<span class="tag-g">'+a+'</span>';}});
    body+='</div>';
  }}

  body+='<div class="sec">Technology</div>';
  body+=drow('Chatbot',h.chatbot_status+(h.chatbot_platform?' ('+h.chatbot_platform+')':''));
  body+=drow('Mobile Check-in',h.has_mobile_checkin?'&#10003; Yes':'&#10007; No');
  body+=drow('Digital Key',h.has_digital_key?'&#10003; Yes':'&#10007; No');
  body+=drow('Smart Locks',h.has_smart_locks?'&#10003; Yes':'&#10007; No');
  body+=drow('Guest Messaging',h.has_guest_messaging?'&#10003; Yes':'&#10007; No');

  body+='<div class="sec">Pages Crawled</div>';
  body+='<div style="color:var(--mu);font-size:12px">'+(h.pages_crawled||'homepage only')+'</div>';

  document.getElementById('m-body').innerHTML=body;
  document.getElementById('overlay').classList.add('open');
  document.getElementById('overlay').scrollTop=0;
}}

function closeModal() {{ document.getElementById('overlay').classList.remove('open'); }}
document.getElementById('overlay').addEventListener('click',function(e){{ if(e.target===this) closeModal(); }});
document.addEventListener('keydown',function(e){{ if(e.key==='Escape') closeModal(); }});

function doExport() {{
  var cols=['rank','lead_score','priority','property_name','property_type','city','address',
    'phone','all_phones_str','email','all_emails_str','website_url','google_reviews',
    'star_rating','rooms','room_types','chatbot_status','has_mobile_checkin',
    'has_digital_key','has_pool','has_gym','has_spa','has_restaurant','has_bar',
    'has_breakfast','has_parking','has_airport_shuttle','has_room_service',
    'has_concierge','has_meeting_rooms','has_wedding_services','has_laundry',
    'has_accessible_rooms','is_pet_friendly','near_airport','near_tourist',
    'near_business','min_stay_nights','outreach_angle','hotel_summary'];
  var rows=[cols.join(',')];
  filtered.forEach(function(h) {{
    rows.push(cols.map(function(c) {{
      var v=h[c]; if(v==null) v=''; v=String(v);
      if(v.indexOf(',')>=0||v.indexOf('"')>=0) v='"'+v.replace(/"/g,'""')+'"';
      return v;
    }}).join(','));
  }});
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([rows.join('\\n')],{{type:'text/csv'}}));
  a.download='unloket_leads.csv'; a.click();
}}

['search','tier','type','chatbot','pet'].forEach(function(id) {{
  document.getElementById(id).addEventListener(id==='search'?'input':'change',applyFilters);
}});
init();
</script></body></html>"""


# Main

async def run(city: str, max_hotels: int):
    out = Path("output")
    out.mkdir(exist_ok=True)

    print(f"\n{'='*55}")
    print(f"  UNLOKET HOTEL SCRAPER")
    print(f"  City: {city}  |  Target: {max_hotels} hotels")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  A browser window will open — leave it alone")
    print(f"{'='*55}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox","--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 900},
        )
        await ctx.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot,mp4,mp3}",
            lambda r: r.abort()
        )
        gmaps_page = await ctx.new_page()
        site_page  = await ctx.new_page()

        hotels = await scrape_google_maps(city, max_hotels, gmaps_page)
        if not hotels:
            await browser.close()
            return

        await crawl_websites(hotels, site_page)

        await lookup_rooms_google(hotels, site_page)

        await browser.close()

    print(f"\n[4/4] Scoring {len(hotels)} hotels...")
    for h in hotels:
        h.update(score_hotel(h))
        h["outreach_angle"] = build_outreach(h)
        h["hotel_summary"]  = build_hotel_summary(h)
    hotels.sort(key=lambda h: h.get("lead_score", 0), reverse=True)
    for i, h in enumerate(hotels):
        h["rank"] = i + 1

    print("\n[Export] Saving...")
    export_csv(hotels, str(out / "leads_ranked.csv"))
    export_dashboard(hotels, str(out / "dashboard.html"))

    hot  = sum(1 for h in hotels if h.get("priority") == "Hot")
    warm = sum(1 for h in hotels if h.get("priority") == "Warm")
    cold = sum(1 for h in hotels if h.get("priority") == "Cold")
    wem  = sum(1 for h in hotels if h.get("email") or h.get("all_emails"))
    wrm  = sum(1 for h in hotels if h.get("rooms"))

    print(f"\n{'='*55}")
    print(f"  DONE — {len(hotels)} hotels in {city}")
    print(f"  Hot:{hot}  Warm:{warm}  Cold:{cold}")
    print(f"  With email:      {wem}/{len(hotels)}")
    print(f"  With room count: {wrm}/{len(hotels)}")
    print(f"\n  Top 5:")
    for h in hotels[:5]:
        em = " E" if h.get("email") else ""
        rm = f" R:{h['rooms']}" if h.get("rooms") else ""
        print(f"    #{h['rank']}  {h['lead_score']:>3}pts  {h['property_name']}{em}{rm}")
    print(f"\n  -> output/dashboard.html")
    print(f"  -> output/leads_ranked.csv")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", type=str, default="Chicago")
    parser.add_argument("--max",  type=int, default=20)
    args = parser.parse_args()
    asyncio.run(run(args.city, args.max))
