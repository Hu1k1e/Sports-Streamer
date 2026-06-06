import asyncio
import httpx
import logging
import time
from typing import List, Dict, Any
from config import PROXY_HOST
import json
import os

logger = logging.getLogger(__name__)

PPV_API_STREAMS = "https://api.ppv.to/api/streams"

# Cache for ppv data
_cache = {
    "streams": None,
    "last_updated": 0
}

async def get_ppv_streams() -> List[Dict[str, Any]]:
    """Fetch live streams from ppv.to API with caching."""
    current_time = time.time()
    if _cache["streams"] is not None and (current_time - _cache["last_updated"]) < (5 * 60):
        return _cache["streams"]

    logger.info(f"Fetching PPV streams from {PPV_API_STREAMS}")
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(PPV_API_STREAMS, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
                "Referer": "https://ppv.to/"
            })
            response.raise_for_status()
            data = response.json()
            
            all_matches = []
            if "streams" in data:
                categories = data["streams"]
                for category in categories:
                    if "streams" in category:
                        matches = category["streams"]
                        for match in matches:
                            # Filter out games that haven't started or are too old if necessary
                            # but API already returns live games
                            all_matches.append(match)
            
            _cache["streams"] = all_matches
            _cache["last_updated"] = current_time
            logger.info(f"Fetched {len(all_matches)} total PPV matches")
            return all_matches
            
        except Exception as e:
            logger.error(f"Error fetching PPV streams: {e}")
            return _cache["streams"] or []

async def generate_ppv_m3u() -> str:
    """Generate M3U playlist for PPV.to."""
    events = await get_ppv_streams()
    lines = ["#EXTM3U"]
    
    # We will point the M3U to our proxy
    base_url = PROXY_HOST.rstrip('/')
    
    for event in events:
        title = event.get("name", "Unknown Match")
        category = event.get("category_name", "Other")
        uri_name = event.get("uri_name")
        poster = event.get("poster", "")
        tvg_id = str(event.get("id", ""))
        
        if not uri_name:
            continue
            
        lines.append(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{title}" tvg-logo="{poster}" group-title="{category}",{title}')
        
        # The proxy route will be /ppv/stream/{uri_name}
        stream_url = f"{base_url}/ppv/stream/{uri_name}"
        lines.append(stream_url)
        
    return "\n".join(lines) + "\n"

async def generate_ppv_epg() -> str:
    """Generate XMLTV EPG for PPV.to."""
    events = await get_ppv_streams()
    
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="PPV Proxy" generator-info-url="https://github.com/Hu1k1e/Sports-Streamer">'
    ]
    
    # Generate channels
    for event in events:
        tvg_id = str(event.get("id", ""))
        title = event.get("name", "Unknown Match")
        poster = event.get("poster", "")
        
        lines.append(f'  <channel id="{tvg_id}">')
        lines.append(f'    <display-name lang="en">{title}</display-name>')
        if poster:
            lines.append(f'    <icon src="{poster}"/>')
        lines.append('  </channel>')
        
    # Generate programmes
    from datetime import datetime, timezone
    
    for event in events:
        tvg_id = str(event.get("id", ""))
        title = event.get("name", "Unknown Match")
        
        starts_at = event.get("starts_at")
        ends_at = event.get("ends_at")
        
        if not starts_at:
            continue
            
        if not ends_at:
            ends_at = starts_at + 10800 # default 3 hours
            
        dt_start = datetime.fromtimestamp(starts_at, tz=timezone.utc)
        dt_end = datetime.fromtimestamp(ends_at, tz=timezone.utc)
        
        start_str = dt_start.strftime("%Y%m%d%H%M%S %z")
        end_str = dt_end.strftime("%Y%m%d%H%M%S %z")
        
        lines.append(f'  <programme start="{start_str}" stop="{end_str}" channel="{tvg_id}">')
        lines.append(f'    <title lang="en">{title}</title>')
        lines.append(f'    <desc lang="en">Live stream for {title}</desc>')
        lines.append('  </programme>')
        
    lines.append('</tv>')
    return "\n".join(lines)

from playwright.async_api import async_playwright

async def fetch_ppv_m3u8_url(embed_url: str) -> str:
    """
    Use Playwright to resolve the actual .m3u8 link for ppv.to streams.
    This requires dismissing an ad overlay.
    """
    m3u8_url = None
    async with async_playwright() as p:
        # headless=False was needed in our test, but we can try headless=True with args
        # But we will use headless=True with standard args to bypass
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        async def handle_request(route, request):
            nonlocal m3u8_url
            if ".m3u8" in request.url and not m3u8_url:
                m3u8_url = request.url
            await route.continue_()

        await page.route("**/*", handle_request)

        try:
            logger.info(f"Navigating to PPV embed: {embed_url}")
            await page.goto(embed_url, timeout=30000, referer="https://ppv.to/")
            
            # Remove ad overlay if exists
            await page.evaluate("""
                const overlay = document.getElementById('dontfoid');
                if (overlay) overlay.remove();
            """)
            
            # Wait a bit then click the center to trigger play
            await page.wait_for_timeout(2000)
            await page.mouse.click(400, 300)
            await page.wait_for_timeout(500)
            await page.mouse.click(400, 300)
            
            # Wait up to 10 seconds for the m3u8 request to be intercepted
            for _ in range(20):
                if m3u8_url:
                    break
                await asyncio.sleep(0.5)
                
        except Exception as e:
            logger.error(f"Error resolving PPV M3U8 for {embed_url}: {e}")
        finally:
            await browser.close()
            
    return m3u8_url
