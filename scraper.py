import asyncio
import logging
import re
import time
from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from config import STREAMED_PK_URL, DEFAULT_HEADERS, DEBUG_LOGGING

playwright_semaphore = asyncio.Semaphore(3)

logger = logging.getLogger("scraper")
if DEBUG_LOGGING:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

API_BASE_URL = f"{STREAMED_PK_URL}/api"

# ---------------------------------------------------------------------------
# In-memory cache for API responses (avoids hammering the API on every
# Jellyfin playlist / EPG refresh).  TTL = 5 minutes.
# ---------------------------------------------------------------------------
_cache: dict[str, dict] = {}
_CACHE_TTL = 300  # 5 minutes in seconds


def _evict_expired():
    """Remove all expired entries from the API cache."""
    now = time.time()
    expired = [k for k, v in _cache.items() if (now - v["ts"]) >= _CACHE_TTL]
    for k in expired:
        del _cache[k]
    if expired:
        logger.debug("Evicted %d expired API cache entries: %s", len(expired), expired)


def _get_cached(key: str):
    _evict_expired()
    entry = _cache.get(key)
    if entry:
        logger.debug("API cache HIT for '%s' (age %.0fs)", key, time.time() - entry["ts"])
        return entry["data"]
    return None


def _set_cached(key: str, data):
    _evict_expired()
    _cache[key] = {"data": data, "ts": time.time()}


# ---------------------------------------------------------------------------
# Sports lookup  (/api/sports)
# ---------------------------------------------------------------------------
_sports_map: dict[str, str] = {}


async def get_sports() -> dict[str, str]:
    """
    Fetch sport categories from /api/sports.
    Returns a dict mapping sport id -> display name,
    e.g. {"fight": "Fight (UFC, Boxing)", "football": "Football"}
    """
    global _sports_map
    cached = _get_cached("sports")
    if cached is not None:
        return cached

    logger.info("Fetching sports from %s/sports", API_BASE_URL)
    async with AsyncSession(impersonate="chrome", timeout=10) as client:
        try:
            response = await client.get(f"{API_BASE_URL}/sports")
            response.raise_for_status()
            data = response.json()
            _sports_map = {s["id"]: s["name"] for s in data}
            logger.info("Loaded %d sport categories", len(_sports_map))
            _set_cached("sports", _sports_map)
            return _sports_map
        except Exception as e:
            logger.error("Error fetching sports: %s", e, exc_info=True)
            return _sports_map  # return stale data if available


# ---------------------------------------------------------------------------
# Live events  (/api/matches/live)  —  used for the M3U playlist
# ---------------------------------------------------------------------------
async def get_live_events() -> list[dict]:
    """
    Fetch currently-live events from /api/matches/live.
    Only events that are actively broadcasting right now are returned.
    """
    cached = _get_cached("live_events")
    if cached is not None:
        return cached

    logger.info("Fetching live events from %s/matches/live", API_BASE_URL)
    async with AsyncSession(impersonate="chrome", timeout=10) as client:
        try:
            response = await client.get(f"{API_BASE_URL}/matches/live")
            response.raise_for_status()
            data = response.json()

            events = _parse_events(data, is_live=True)
            logger.info("Found %d live events", len(events))
            _set_cached("live_events", events)
            return events
        except Exception as e:
            logger.error("Error fetching live events: %s", e, exc_info=True)
            return []


# ---------------------------------------------------------------------------
# All events  (/api/matches/all)  —  used for the EPG schedule
# ---------------------------------------------------------------------------
async def _has_any_stream(client: AsyncSession, event: dict, semaphore: asyncio.Semaphore) -> bool:
    """Check if the event has at least one source with actual streams."""
    async with semaphore:
        for src in event.get("sources", []):
            src_name = src.get("source")
            src_id = src.get("id")
            if not src_name or not src_id:
                continue
            try:
                resp = await client.get(f"{API_BASE_URL}/stream/{src_name}/{src_id}", timeout=5)
                if resp.status_code == 200:
                    streams = resp.json()
                    if isinstance(streams, list) and len(streams) > 0:
                        return True
            except Exception:
                pass
        return False


async def get_all_events() -> list[dict]:
    """
    Fetch all events (upcoming + live + recent) from /api/matches/all.
    Used for EPG guide data.
    """
    cached = _get_cached("all_events")
    if cached is not None:
        return cached

    logger.info("Fetching all events from %s/matches/all-today", API_BASE_URL)
    async with AsyncSession(impersonate="chrome", timeout=10) as client:
        try:
            # Fetch both endpoints concurrently so we can tag live status
            response_all, response_live = await asyncio.gather(
                client.get(f"{API_BASE_URL}/matches/all-today"),
                client.get(f"{API_BASE_URL}/matches/live")
            )
            response_all.raise_for_status()
            response_live.raise_for_status()

            all_data = response_all.json()
            live_data = response_live.json()
            live_ids = {item["id"] for item in live_data}

            events = _parse_events(all_data, live_ids=live_ids)
            
            # Filter out events that have no actual streams available, UNLESS they are live
            semaphore = asyncio.Semaphore(15)
            tasks = []
            for event in events:
                if event["id"] in live_ids:
                    # Always include live games
                    tasks.append(asyncio.sleep(0, result=True))
                else:
                    tasks.append(_has_any_stream(client, event, semaphore))
            
            results = await asyncio.gather(*tasks)
            
            valid_events = [ev for ev, has_stream in zip(events, results) if has_stream]
            
            logger.info("Found %d total events (%d valid, %d live)", len(events), len(valid_events), len(live_ids))
            _set_cached("all_events", valid_events)
            return valid_events
        except Exception as e:
            logger.error("Error fetching all events: %s", e, exc_info=True)
            return []


# ---------------------------------------------------------------------------
# Shared event parser
# ---------------------------------------------------------------------------

def _parse_events(data: list, is_live: bool = False, live_ids: set = None) -> list[dict]:
    """Parse raw API match data into our internal event dicts."""
    events = []
    
    for item in data:
        if not item.get("sources"):
            continue

        event_id = item["id"]
        event_is_live = is_live or (live_ids is not None and event_id in live_ids)
        event_date = item.get("date", 0)

        raw_title = item.get("title", "Unknown Event")
        # Remove leading 4-digit year like "2026 " from the title
        clean_title = re.sub(r'^\d{4}\s+', '', raw_title)

        event_id = item["id"]
        poster = item.get("poster", "")
        teams = item.get("teams")

        # Build image URL for the event
        logo_url = ""
        if poster:
            # poster field is already a path like "/api/images/proxy/..."
            logo_url = f"{STREAMED_PK_URL}{poster}"
        elif teams:
            # Try home team badge
            home_badge = (teams.get("home") or {}).get("badge", "")
            if home_badge:
                logo_url = f"{STREAMED_PK_URL}/api/images/badge/{home_badge}.webp"

        events.append({
            "id": event_id,
            "name": clean_title,
            "category": item.get("category", "other"),
            "date": item.get("date", 0),
            "poster": poster,
            "logo_url": logo_url,
            "sources": item["sources"],
            "is_live": is_live or (live_ids is not None and event_id in live_ids),
            "teams": teams,
        })

    return events


# ---------------------------------------------------------------------------
# Stream URL resolution  (embed URL → m3u8 via Playwright)
# ---------------------------------------------------------------------------

# All documented stream sources + "admin" which appears in real data
PREFERRED_SOURCES = [
    "alpha", "bravo", "echo", "golf", "admin",
    "charlie", "delta", "foxtrot", "hotel", "intel",
]


async def _get_embed_urls(match_id: str) -> list[str]:
    """
    Get the embed URLs for a match using its sources via the Stream API.
    Fetches from /api/matches/live first (since the user is playing a live stream),
    falls back to /api/matches/all if not found.
    Returns a sorted list of embed URLs to try.
    """
    # Try live events first, then all events
    for fetch_fn in [get_live_events, get_all_events]:
        events = await fetch_fn()
        match = next((m for m in events if m["id"] == match_id), None)
        if match:
            break

    if not match:
        logger.error("Match %s not found in API", match_id)
        return None

    sources = match["sources"]
    logger.info("Found %d sources for match %s", len(sources), match_id)

    async with AsyncSession(impersonate="chrome", timeout=10) as client:
        # Fetch all streams concurrently from all sources
        tasks = []
        for src in sources:
            src_name = src["source"]
            src_id = src["id"]
            tasks.append(client.get(f"{API_BASE_URL}/stream/{src_name}/{src_id}"))
        
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_streams = []
        for response in responses:
            if isinstance(response, Exception):
                continue
            if response.status_code == 200:
                try:
                    streams = response.json()
                    all_streams.extend(streams)
                except Exception:
                    pass

        if not all_streams:
            logger.error("No streams found across %d sources for match %s", len(sources), match_id)
            return []

        # Sort all streams by:
        # 1. Admin source priority
        # Sort streams according to priority
        def stream_priority(s):
            # Prioritize admin streams, then sort by viewers
            is_admin = s.get('source') == 'admin'
            return (is_admin, s.get('viewers', 0))
            
        all_streams.sort(key=stream_priority, reverse=True)
        
        urls = []
        for s in all_streams:
            u = s.get("embedUrl")
            if u and u not in urls:
                urls.append(u)
                logger.info("Found candidate stream from %s (viewers=%s, HD=%s, lang=%s): %s",
                            s.get("source"), s.get("viewers"), s.get("hd"),
                            s.get("language"), u)
                            
        return urls

    return []


async def get_stream_url(match_id: str):
    """
    End-to-end process:
    1. Resolve the embedUrl using the REST API
    2. Navigate Playwright to the embedUrl to capture the m3u8
    """
    logger.info("=== get_stream_url START ===")
    logger.info("Match ID: %s", match_id)

    # Step 1: Get embed URLs
    embed_urls = await _get_embed_urls(match_id)
    if not embed_urls:
        logger.error("Could not resolve any embed URL for match %s", match_id)
        return {"url": None, "headers": {}, "content": None}

    # Step 2: Use Playwright just to evaluate the embed page and capture m3u8
    async with playwright_semaphore:
        async with Stealth().use_async(async_playwright()) as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"])
            
            for embed_url in embed_urls:
                logger.info("Navigating to embed URL: %s", embed_url)
                page = await context.new_page()

                m3u8_url = None
                m3u8_headers = {}
                m3u8_content = None

                # Block unneeded resources to speed up page load
                async def intercept_route(route):
                    if route.request.resource_type in ["image", "stylesheet", "font"]:
                        await route.abort()
                    else:
                        await route.continue_()
                
                await page.route("**/*", intercept_route)

                async def on_response(response):
                    nonlocal m3u8_url, m3u8_headers, m3u8_content
                    req_url = response.url
                    if ".m3u8" in req_url and not m3u8_url and response.status == 200:
                        m3u8_url = req_url
                        m3u8_headers = dict(response.request.headers)
                        logger.info("  👉 Captured VALID m3u8 URL: %s", req_url[:150])
                        try:
                            body = await response.text()
                            if body and "#EXTM3U" in body:
                                m3u8_content = body
                                logger.info("  ✓ Captured m3u8 response body (%d bytes)", len(body))
                        except Exception:
                            pass

                page.on("response", on_response)

                try:
                    # 'domcontentloaded' ensures the core HTML/scripts are loaded before we start the timeout clock
                    await page.goto(embed_url, wait_until="domcontentloaded", timeout=15000)
                    logger.info("Embed page loaded")

                    # Fast poll for m3u8 network request (checks every 500ms up to 15s max)
                    for _ in range(30):
                        if m3u8_url:
                            break
                        await page.wait_for_timeout(500)

                    # If not yet captured, try clicking video/player elements
                    if not m3u8_url:
                        logger.info("Trying to click video/player elements...")
                        for selector in ["video", ".player", "[class*='player']", ".video-container", "iframe", "body"]:
                            if m3u8_url:
                                break
                            try:
                                element = await page.query_selector(selector)
                                if element:
                                    await element.click(timeout=2000)
                                    await page.wait_for_timeout(2000)
                            except Exception:
                                pass

                    if m3u8_url:
                        # Capture cookies and attach to headers for proxy
                        cookies = await context.cookies()
                        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                        if cookie_str:
                            m3u8_headers["Cookie"] = cookie_str

                        logger.info("=== get_stream_url END (SUCCESS) ===")
                        await page.close()
                        await browser.close()
                        return {"url": m3u8_url, "headers": m3u8_headers, "content": m3u8_content}

                    logger.warning("Failed to capture m3u8 on %s. Trying next embed URL...", embed_url)
                except Exception as e:
                    logger.error("Error evaluating embed URL %s: %s", embed_url, e)
                finally:
                    await page.close()
                    
            # If we exhausted all embed URLs
            await browser.close()
            
    logger.warning("=== get_stream_url END (FAILED ALL SOURCES) ===")
    return {"url": None, "headers": {}, "content": None}
