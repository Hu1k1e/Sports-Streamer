import asyncio
import logging
import re
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
from config import STREAMED_PK_URL, DEFAULT_HEADERS, DEBUG_LOGGING

logger = logging.getLogger("scraper")
if DEBUG_LOGGING:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)


async def get_events():
    logger.info("Fetching events from %s", STREAMED_PK_URL)
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"])
        page = await context.new_page()
        try:
            await page.goto(STREAMED_PK_URL, wait_until="domcontentloaded", timeout=15000)
            content = await page.content()
            soup = BeautifulSoup(content, 'html.parser')
            
            events = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                if href.startswith('/watch/'):
                    h1 = a.find('h1')
                    if h1:
                        title = h1.get('title') or h1.get_text(strip=True)
                        path = href.lstrip('/')
                        if title and title not in [e['name'] for e in events]:
                            events.append({"name": title, "path": path})
            
            logger.info("Found %d events", len(events))
            for ev in events:
                logger.debug("  Event: %s -> %s", ev["name"], ev["path"])
            return events
        except Exception as e:
            logger.error("Error fetching events: %s", e, exc_info=True)
            return []
        finally:
            await browser.close()


async def get_stream_url(event_path: str):
    """
    Navigate to the event's source-selection page, pick the first stream
    source link, navigate to the actual stream page, then capture the
    m3u8 URL from network requests or JS extraction.

    Site flow:
      /watch/{event}              → source selection page (no player)
      /watch/{event}/{src}/{num}  → actual stream page (player + iframe)
    """
    watch_url = f"{STREAMED_PK_URL}/{event_path}"
    logger.info("=== get_stream_url START ===")
    logger.info("Event path: %s", event_path)
    logger.info("Step 1: Navigating to source-selection page: %s", watch_url)

    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"])
        page = await context.new_page()
        
        m3u8_url = None
        m3u8_headers = {}
        m3u8_content = None
        all_requests_log = []

        # --- Passive listener: observe requests without interfering ---
        def on_request(request):
            nonlocal m3u8_url, m3u8_headers
            req_url = request.url
            # Log interesting requests (skip static assets)
            if any(ext in req_url for ext in [".m3u8", ".mpd", ".ts", "playlist", "master"]):
                logger.debug("  [NET] %s %s", request.method, req_url[:120])
                all_requests_log.append(req_url)
            
            if ".m3u8" in req_url and not m3u8_url:
                m3u8_url = req_url
                m3u8_headers = dict(request.headers)
                logger.info("  ✓ Captured m3u8 URL: %s", req_url[:150])
                logger.debug("  ✓ Headers: %s", m3u8_headers)

        # --- Response listener: capture the m3u8 body so we don't need to refetch ---
        async def on_response(response):
            nonlocal m3u8_content
            if ".m3u8" in response.url and m3u8_content is None:
                try:
                    body = await response.text()
                    if body and "#EXTM3U" in body:
                        m3u8_content = body
                        logger.info("  ✓ Captured m3u8 response body (%d bytes)", len(body))
                except Exception as e:
                    logger.debug("  Could not read m3u8 response body: %s", e)

        page.on("request", on_request)
        page.on("response", on_response)
        
        try:
            # ── Step 1: Load the source-selection page ──
            await page.goto(watch_url, wait_until="domcontentloaded", timeout=20000)
            logger.info("Source-selection page loaded")
            await page.wait_for_timeout(3000)
            
            # ── Step 2: Find and navigate to the first stream source ──
            # Source links follow the pattern: /watch/{event}/{source}/{number}
            # e.g., /watch/roland-garros-french-open-roland-2026/echo/1
            logger.info("Step 2: Looking for stream source links...")
            
            content = await page.content()
            soup = BeautifulSoup(content, 'html.parser')
            
            # Find links that are sub-paths of the current watch page
            # event_path is like "watch/roland-garros-french-open-roland-2026"
            # source links are like "/watch/roland-garros-french-open-roland-2026/echo/1"
            source_link = None
            event_href_prefix = f"/{event_path}/"
            
            for a in soup.find_all('a', href=True):
                href = a['href']
                if href.startswith(event_href_prefix):
                    source_link = href
                    logger.info("  Found source link: %s", source_link)
                    break
            
            if not source_link:
                # Fallback: try finding any link deeper than the watch page
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    # Match pattern: /watch/event-name/source/number
                    if href.startswith('/watch/') and href.count('/') >= 4:
                        source_link = href
                        logger.info("  Found source link (fallback): %s", source_link)
                        break
            
            if not source_link:
                logger.error("No stream source links found on the source-selection page")
                logger.warning("Page title: %s", await page.title())
                # Dump all links for debugging
                all_links = [a['href'] for a in soup.find_all('a', href=True) if a['href'].startswith('/watch/')]
                logger.warning("All /watch/ links found: %s", all_links)
                return {"url": None, "headers": {}}
            
            # Navigate to the actual stream page
            stream_page_url = f"{STREAMED_PK_URL}{source_link}"
            logger.info("Step 3: Navigating to stream page: %s", stream_page_url)
            await page.goto(stream_page_url, wait_until="domcontentloaded", timeout=20000)
            logger.info("Stream page loaded")
            
            # ── Step 3: Wait for m3u8 on the actual stream page ──
            
            # Phase 1: Wait for the page/iframe to naturally trigger the stream
            logger.info("Phase 1: Waiting 10s for automatic m3u8 request...")
            await page.wait_for_timeout(10000)
            
            if m3u8_url:
                logger.info("Phase 1 success — m3u8 captured during stream page load")
                return {"url": m3u8_url, "headers": m3u8_headers, "content": m3u8_content}
            
            # Phase 2: Try clicking video/player elements
            logger.info("Phase 2: Trying to click video/player elements...")
            click_selectors = ["video", ".player", "[class*='player']", ".video-container", "iframe"]
            for selector in click_selectors:
                if m3u8_url:
                    break
                try:
                    element = await page.query_selector(selector)
                    if element:
                        logger.debug("  Clicking '%s'", selector)
                        await element.click(timeout=2000)
                        await page.wait_for_timeout(3000)
                except Exception:
                    pass
            
            if m3u8_url:
                logger.info("Phase 2 success — m3u8 captured after element click")
                return {"url": m3u8_url, "headers": m3u8_headers, "content": m3u8_content}
            
            # Phase 3: Click through iframes and body elements
            logger.info("Phase 3: Clicking through frames...")
            for attempt in range(3):
                if m3u8_url:
                    break
                for frame in page.frames:
                    if m3u8_url:
                        break
                    try:
                        logger.debug("  Frame %d click attempt %d: %s", 
                                     page.frames.index(frame), attempt, frame.url[:80])
                        await frame.click("body", timeout=1500)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass
            
            if m3u8_url:
                logger.info("Phase 3 success — m3u8 captured after frame clicks")
                return {"url": m3u8_url, "headers": m3u8_headers, "content": m3u8_content}
            
            # Phase 4: JavaScript-based fallback — search for m3u8 URLs in DOM
            logger.info("Phase 4: JS-based m3u8 extraction fallback...")
            js_m3u8 = await _extract_m3u8_via_js(page)
            if js_m3u8:
                logger.info("Phase 4 success — found m3u8 via JS: %s", js_m3u8[:150])
                return {"url": js_m3u8, "headers": {"Referer": stream_page_url, "User-Agent": DEFAULT_HEADERS["User-Agent"]}, "content": None}
            
            # All phases failed
            logger.warning("=== ALL PHASES FAILED ===")
            logger.warning("Logged %d network requests total", len(all_requests_log))
            for r in all_requests_log:
                logger.warning("  [REQ] %s", r[:150])
            
            # Dump page info for debugging
            page_title = await page.title()
            frame_count = len(page.frames)
            logger.warning("Page title: %s | Frames: %d", page_title, frame_count)
            for i, frame in enumerate(page.frames):
                logger.warning("  Frame %d: %s", i, frame.url[:120])
            
            return {"url": None, "headers": {}, "content": None}
            
        except Exception as e:
            logger.error("Error in get_stream_url: %s", e, exc_info=True)
            return None
        finally:
            await browser.close()
            logger.info("=== get_stream_url END ===")


async def _extract_m3u8_via_js(page) -> str | None:
    """
    Search all frames for m3u8 URLs via JavaScript:
    1. Check performance resource entries
    2. Search inline script tags
    3. Check video source elements
    """
    m3u8_pattern = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*')
    
    for frame in page.frames:
        try:
            # Method 1: performance.getEntries()
            perf_urls = await frame.evaluate("""
                () => {
                    try {
                        return performance.getEntries()
                            .map(e => e.name)
                            .filter(n => n.includes('.m3u8'));
                    } catch(e) { return []; }
                }
            """)
            if perf_urls:
                logger.debug("  JS perf entries found: %s", perf_urls)
                return perf_urls[0]
            
            # Method 2: Search script tag contents
            script_content = await frame.evaluate("""
                () => {
                    try {
                        return Array.from(document.querySelectorAll('script'))
                            .map(s => s.textContent)
                            .join('\\n');
                    } catch(e) { return ''; }
                }
            """)
            matches = m3u8_pattern.findall(script_content)
            if matches:
                logger.debug("  JS script tag matches: %s", matches)
                return matches[0]
            
            # Method 3: Check video/source elements
            video_src = await frame.evaluate("""
                () => {
                    try {
                        const video = document.querySelector('video');
                        if (video && video.src && video.src.includes('.m3u8')) return video.src;
                        const source = document.querySelector('video source');
                        if (source && source.src && source.src.includes('.m3u8')) return source.src;
                        return null;
                    } catch(e) { return null; }
                }
            """)
            if video_src:
                logger.debug("  JS video src found: %s", video_src)
                return video_src
                
        except Exception as e:
            logger.debug("  JS extraction error in frame: %s", e)
            continue
    
    return None
