# Hotel Web-Scraper and Lead Generator

A Python-based automated lead generation pipeline for the hospitality industry. This tool extracts data from Google Maps and individual hotel websites to identify high-value sales prospects. It uses room counts, existing technology stacks, and contact availability to rank leads into a prioritized funnel.

## How to run
1. **Install dependencies**: `pip install playwright asyncio`
2. **Setup browser**: `playwright install chromium`
3. **Run the scraper**: `python scraper2.py --city "New York" --limit 25`

## Process Map 

| Stage | Purpose |
|-------|---------|
| **Discovery** | Scrapes Google Maps for property names, star ratings, customer reviews, and official URLs. |
| **Deep Crawl** | Navigates hotel websites to find emails and identify tech stacks (chatbots). |
| **Validation** | Executes secondary searches to confirm room counts and property types. |
| **Scoring** | Ranks hotels as Hot, Warm, or Cold based on custom sales criteria. |
| **Export** | Generates a ranked CSV and a visual HTML dashboard for lead review. |

## What’s implemented

- **Automated Chatbot Detection** — Scans site source code for Intercom, Drift, HubSpot, Tidio, and 10+ other platforms to identify digital gaps.
- **Intelligent Lead Scoring** — Proprietary logic that assigns priorities based on property size, review density, and current technology adoption.
- **Review Keyword Sentiment Analysis** — Scrapes Google Maps reviews to identify pain points or selling opportunities. It scans for negative keywords (e.g., "slow response," "hard to book," "outdated") which increase the lead score as they represent a sales opportunity, and positive keywords (e.g., "modern app," "great chatbot") which may lower the priority.
- **Multi-Page Website Crawling** — Programmatically follows links to "Contact," "About," and "Rooms" pages within each hotel website to ensure maximum data coverage.
- **Email Validation & Cleaning** — Filters out junk addresses (e.g., info@google.com) and standardizes obfuscated email formats.
- **Amenity Mapping** — Keyword-based detection for high-value amenities like Spas, Gyms, and Pools.
- **Visual Dashboard** — A built-in HTML exporter (`dashboard.html`) that provides a clean interface for viewing detailed information regarding each hotel that was scraped.

## Lead Priority Logic

The engine uses a weighted scoring system (out of 84 total points) to categorize leads into three tiers:

- **Hot Leads (60+ points)** — Typically independent or boutique hotels with 40–120 rooms. These are prioritized if they lack an AI chatbot but have high guest engagement (500+ reviews).
- **Warm Leads (40-59 points)** — Standard independent properties or inns with some tech adoption but missing key integrations like mobile check-in or guest messaging.
- **Cold Leads (<40 points)** — Chain hotels or very small properties (under 10 rooms) that do not fit the primary sales profile.

Points are also awarded for location context, such as being near major airports, business districts, or tourist hubs.

## Tech

- **Python 3.10+**
- **Playwright** (Headless browser automation)
- **Asyncio** (Concurrent web crawling)
- **Regular Expressions** (Pattern matching for emails and room counts)
- **HTML/CSS** (Static dashboard generation)
