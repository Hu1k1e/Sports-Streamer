import asyncio
import logging
import httpx
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from config import STREAMED_PK_URL, DEFAULT_HEADERS, DEBUG_LOGGING

logger = logging.getLogger("scraper")
if DEBUG_LOGGING:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

API_BASE_URL = f"{STREAMED_PK_URL}/api"

async def get_events():
    """
    Fetch all events from the streamed.pk REST API.
    Returns a list of parsed event dicts:
    [
        {
            "id": "match-id",
            "name": "Match Title",
            "category": "football",
            "sources": [{"source": "echo", "id": "match-id"}, ...]
        }, ...
    ]
    """
    logger.info("Fetching events from %s/matches/all", API_BASE_URL)
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(f"{API_BASE_URL}/matches/all")
            response.raise_for_status()
            data = response.json()
            
            events = []
            for item in data:
                # Skip events with no sources
                if not item.get("sources"):
                    continue
                    
                events.append({
                    "id": item["id"],
                    "name": item.get("title", "Unknown Event"),
                    "category": item.get("category", "other"),
                    "date": item.get("date", 0),
                    "sources": item["sources"]
                })
                
            logger.info("Found %d events via API", len(events))
            return events
        except Exception as e:
            logger.error("Error fetching events from API: %s", e, exc_info=True)
            return []


async def _get_embed_url(match_id: str):
    """
    Get the embed URL for a match using its sources via the Stream API.
    Since we only have the match_id here, we first fetch the matches list 
    to find its sources, then try sources in order of preference.
    """
    # 1. Get sources for this match
    events = await get_events()
    match = next((m for m in events if m["id"] == match_id), None)
    
    if not match:
        logger.error("Match %s not found in API", match_id)
        return None
        
    sources = match["sources"]
    logger.info("Found %d sources for match %s", len(sources), match_id)
    
    # 2. Try sources in preferred order
    PREFERRED_SOURCES = ["echo", "golf", "admin", "delta", "charlie"]
    
    # Sort sources by preference
    sorted_sources = sorted(
        sources,
        key=lambda s: PREFERRED_SOURCES.index(s["source"]) if s["source"] in PREFERRED_SOURCES else 999
    )
    
    async with httpx.AsyncClient(timeout=10) as client:
        for src in sorted_sources:
            try:
                src_name = src["source"]
                src_id = src["id"]
                logger.debug("Trying source %s for match %s", src_name, match_id)
                response = await client.get(f"{API_BASE_URL}/stream/{src_name}/{src_id}")
                if response.status_code == 200:
                    streams = response.json()
                    if streams and len(streams) > 0:
                        # Find HD stream if possible, else just first one
                        hd_streams = [s for s in streams if s.get("hd")]
                        selected = hd_streams[0] if hd_streams else streams[0]
                        embed_url = selected.get("embedUrl")
                        if embed_url:
                            logger.info("Got embed URL from source %s: %s", src_name, embed_url)
                            return embed_url
            except Exception as e:
                logger.debug("Failed to get stream from source %s: %s", src.get("source"), e)
                
    return None


async def get_stream_url(match_id: str):
    """
    End-to-end process:
    1. Resolve the embedUrl using the REST API
    2. Navigate Playwright to the embedUrl to capture the m3u8
    """
    logger.info("=== get_stream_url START ===")
    logger.info("Match ID: %s", match_id)
    
    # Step 1: Get embed URL
    embed_url = await _get_embed_url(match_id)
    if not embed_url:
        logger.error("Could not resolve any embed URL for match %s", match_id)
        return {"url": None, "headers": {}, "content": None}
        
    # Step 2: Use Playwright just to evaluate the embed page and capture m3u8
    logger.info("Navigating to embed URL: %s", embed_url)
    
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        # We must use a real user agent
        context = await browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"])
        page = await context.new_page()
        
        m3u8_url = None
        m3u8_headers = {}
        m3u8_content = None

        def on_request(request):
            nonlocal m3u8_url, m3u8_headers
            req_url = request.url
            if ".m3u8" in req_url and not m3u8_url:
                m3u8_url = req_url
                m3u8_headers = dict(request.headers)
                logger.info("  ✓ Captured m3u8 URL: %s", req_url[:150])

        async def on_response(response):
            nonlocal m3u8_content
            if ".m3u8" in response.url and m3u8_content is None:
                try:
                    body = await response.text()
                    if body and "#EXTM3U" in body:
                        m3u8_content = body
                        logger.info("  ✓ Captured m3u8 response body (%d bytes)", len(body))
                except Exception as e:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)
        
        try:
            await page.goto(embed_url, wait_until="domcontentloaded", timeout=20000)
            logger.info("Embed page loaded")
            
            # Wait for m3u8 network request
            for _ in range(15):
                if m3u8_url: break
                await page.wait_for_timeout(1000)
            
            # If not yet captured, try clicking video/player elements
            if not m3u8_url:
                logger.info("Trying to click video/player elements...")
                for selector in ["video", ".player", "[class*='player']", ".video-container", "iframe", "body"]:
                    if m3u8_url: break
                    try:
                        element = await page.query_selector(selector)
                        if element:
                            await element.click(timeout=2000)
                            await page.wait_for_timeout(2000)
                    except Exception:
                        pass
                        
            if m3u8_url:
                logger.info("=== get_stream_url END (SUCCESS) ===")
                return {"url": m3u8_url, "headers": m3u8_headers, "content": m3u8_content}
                
            logger.warning("=== get_stream_url END (FAILED) ===")
            return {"url": None, "headers": {}, "content": None}
            
        except Exception as e:
            logger.error("Error evaluating embed URL: %s", e)
            return {"url": None, "headers": {}, "content": None}
        finally:
            await browser.close()
