import asyncio
import logging
import re
import time
from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright, Browser, Playwright
from playwright_stealth import Stealth
from config import STREAMED_PK_URL, DEFAULT_HEADERS, DEBUG_LOGGING

_playwright_instance: Playwright | None = None
_playwright_browser: Browser | None = None
_stealth_context = None

async def init_playwright():
    global _playwright_instance, _playwright_browser, _stealth_context
    if _playwright_browser is None:
        logger.info("Initializing global Playwright browser with Stealth...")
        _stealth_context = Stealth().use_async(async_playwright())
        _playwright_instance = await _stealth_context.__aenter__()
        _playwright_browser = await _playwright_instance.chromium.launch(
            headless=True, 
            args=["--disable-blink-features=AutomationControlled"]
        )

async def close_playwright():
    global _playwright_instance, _playwright_browser, _stealth_context
    if _playwright_browser:
        await _playwright_browser.close()
        _playwright_browser = None
    if _stealth_context:
        await _stealth_context.__aexit__(None, None, None)
        _stealth_context = None
        _playwright_instance = None

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

_shared_api_session: AsyncSession | None = None

def _get_api_session() -> AsyncSession:
    global _shared_api_session
    if _shared_api_session is None:
        _shared_api_session = AsyncSession(impersonate="chrome", timeout=30)
    return _shared_api_session

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
    client = _get_api_session()
    for attempt in range(3):
        try:
            response = await client.get(f"{API_BASE_URL}/sports")
            response.raise_for_status()
            data = response.json()
            
            for sport in data:
                _sports_map[sport["id"]] = sport["name"]
            
            _set_cached("sports", _sports_map)
            return _sports_map
        except Exception as e:
            if attempt == 2:
                logger.error("Error fetching sports: %s", e, exc_info=True)
                return _sports_map  # return stale data if available
            await asyncio.sleep(1)
    return _sports_map


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
    client = _get_api_session()
    for attempt in range(3):
        try:
            response = await client.get(f"{API_BASE_URL}/matches/live")
            response.raise_for_status()
            data = response.json()

            events = _parse_events(data, is_live=True)
            logger.info("Found %d live events", len(events))
            _set_cached("live_events", events)
            return events
        except Exception as e:
            if attempt == 2:
                logger.error("Error fetching live events: %s", e, exc_info=True)
                return []
            await asyncio.sleep(1)
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
    client = _get_api_session()
    for attempt in range(3):
        try:
            # Fetch both endpoints concurrently so we can tag live status
            response_all, response_live = await asyncio.gather(
                client.get(f"{API_BASE_URL}/matches/all-today"),
                client.get(f"{API_BASE_URL}/matches/live"),
                return_exceptions=True
            )
            
            if isinstance(response_all, Exception):
                raise response_all
            if isinstance(response_live, Exception):
                raise response_live

            response_all.raise_for_status()
            response_live.raise_for_status()
            
            all_data = response_all.json()
            live_data = response_live.json()
            
            live_ids = {m["id"] for m in live_data}
            
            # CRITICAL: At midnight UTC, /all-today resets. Any live event that
            # started before midnight will disappear from all_data, even though it's
            # still in live_data. We must merge them to prevent channels dropping.
            all_ids = {m["id"] for m in all_data}
            for live_match in live_data:
                if live_match["id"] not in all_ids:
                    all_data.append(live_match)
                    
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
            if attempt == 2:
                logger.error("Error fetching all events: %s", e, exc_info=True)
                return []
            await asyncio.sleep(1)
    return []


# ---------------------------------------------------------------------------
# Shared event parser
# ---------------------------------------------------------------------------

def _parse_events(data: list, is_live: bool = False, live_ids: set = None) -> list[dict]:
    """Parse raw API match data into our internal event dicts and deduplicate by title."""
    events_dict = {}
    
    for item in data:
        if not item.get("sources"):
            continue

        event_id = item["id"]
        event_is_live = is_live or (live_ids is not None and event_id in live_ids)
        event_date = item.get("date", 0)

        raw_title = item.get("title", "Unknown Event")
        # Remove leading 4-digit year like "2026 " from the title
        clean_title = re.sub(r'^\d{4}\s+', '', raw_title)

        poster = item.get("poster", "")
        teams = item.get("teams")

        # Build image URL for the event
        # Fallback chain: poster → combined badge poster → home badge → away badge
        logo_url = ""
        home_badge_url = ""
        away_badge_url = ""
        home_badge = ""
        away_badge = ""
        if teams:
            home_badge = (teams.get("home") or {}).get("badge", "")
            away_badge = (teams.get("away") or {}).get("badge", "")
            if home_badge:
                home_badge_url = f"{STREAMED_PK_URL}/api/images/badge/{home_badge}.webp"
            if away_badge:
                away_badge_url = f"{STREAMED_PK_URL}/api/images/badge/{away_badge}.webp"

        if poster:
            # poster field is already a path like "/api/images/proxy/..."
            logo_url = f"{STREAMED_PK_URL}{poster}"
        elif home_badge and away_badge:
            # Generate a combined poster with both team logos using the
            # streamed.pk poster API: /api/images/poster/{home}/{away}.webp
            logo_url = f"{STREAMED_PK_URL}/api/images/poster/{home_badge}/{away_badge}.webp"
        elif home_badge_url:
            logo_url = home_badge_url
        elif away_badge_url:
            logo_url = away_badge_url

        new_event = {
            "id": event_id,
            "name": clean_title,
            "category": item.get("category", "other"),
            "date": event_date,
            "poster": poster,
            "logo_url": logo_url,
            "home_badge_url": home_badge_url,
            "away_badge_url": away_badge_url,
            "sources": item["sources"],
            "is_live": event_is_live,
            "teams": teams,
        }
        
        # Deduplicate: if we already have this event name, prefer the live one.
        # If neither or both are live, prefer the one that happens sooner.
        existing = events_dict.get(clean_title)
        if existing:
            if new_event["is_live"] and not existing["is_live"]:
                events_dict[clean_title] = new_event
            elif (new_event["is_live"] == existing["is_live"]) and new_event["date"] > 0 and (existing["date"] == 0 or new_event["date"] < existing["date"]):
                events_dict[clean_title] = new_event
        else:
            events_dict[clean_title] = new_event

    return list(events_dict.values())


# ---------------------------------------------------------------------------
# Stream URL resolution  (embed URL → m3u8 via Playwright)
# ---------------------------------------------------------------------------

# All documented stream sources + "admin" which appears in real data
PREFERRED_SOURCES = [
    "alpha", "bravo", "echo", "golf", "admin",
    "charlie", "delta", "foxtrot", "hotel", "intel",
]


async def _fetch_stream_from_source(client: AsyncSession, source_name: str, source_id: str) -> list:
    """Helper to fetch streams for a specific source."""
    try:
        resp = await client.get(f"{API_BASE_URL}/stream/{source_name}/{source_id}")
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


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

    client = _get_api_session()
    
    # Fetch all streams concurrently from all sources
    tasks = []
    for src in sources:
        tasks.append(_fetch_stream_from_source(client, src["source"], src["id"]))
    
    results = await asyncio.gather(*tasks)
    
    all_streams = []
    for res in results:
        if isinstance(res, list):
            all_streams.extend(res)
    
    if not all_streams:
        logger.error("No streams found across %d sources for match %s", len(sources), match_id)
        return []

    # Sort streams according to priority
    def stream_priority(s):
        # Prioritize admin streams, then sort by viewers
        is_admin = s.get('source') == 'admin'
        return (is_admin, s.get('viewers', 0))
        
    all_streams.sort(key=stream_priority, reverse=True)
    
    urls = []
    for s in all_streams:
        u = s.get("embedUrl")
        source = s.get("source")
        if u and not any(d["url"] == u for d in urls):
            urls.append({"url": u, "source": source})
            logger.info("Found candidate stream from %s (viewers=%s, HD=%s, lang=%s): %s",
                        source, s.get("viewers"), s.get("hd"),
                        s.get("language"), u)
                        
    return urls

async def check_better_source_available(match_id: str, cached_source: str) -> bool:
    """
    Quickly checks the REST API to see if a higher-priority source (like 'admin')
    has come online while we are stuck on a lower-priority cached source.
    Returns True if we should invalidate the cache.
    """
    if cached_source == 'admin':
        return False
        
    embeds = await _get_embed_urls(match_id)
    if embeds and embeds[0]["source"] == "admin" and cached_source != "admin":
        return True
    return False



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
        return {"url": None, "headers": {}, "content": None, "source": None}

    # Step 2: Use Playwright just to evaluate the embed page and capture m3u8
    global _playwright_browser
    if not _playwright_browser:
        await init_playwright()

    async with playwright_semaphore:
        context = await _playwright_browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"])
        try:
            
            for embed_dict in embed_urls:
                embed_url = embed_dict["url"]
                embed_source = embed_dict["source"]
                logger.info("Navigating to embed URL: %s (Source: %s)", embed_url, embed_source)
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
                    # Use 'commit' to skip waiting for the heavy DOM to parse, making it MUCH faster.
                    await page.goto(embed_url, wait_until="commit", timeout=15000)
                    logger.info("Embed page loaded")

                    # Fast poll for m3u8 network request (checks every 500ms up to 20s max)
                    for _ in range(40):
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

                        logger.info("=== get_stream_url SUCCESS ===")
                        return {"url": m3u8_url, "headers": m3u8_headers, "content": m3u8_content, "source": embed_source}
                    else:
                        logger.warning("Failed to extract m3u8 from %s", embed_url)
                except Exception as e:
                    logger.error("Playwright error during get_stream_url: %s", e)
                finally:
                    await page.close()
        finally:
            await context.close()
            
    logger.warning("=== get_stream_url FAILED ===")
    return {"url": None, "headers": {}, "content": None, "source": None}
