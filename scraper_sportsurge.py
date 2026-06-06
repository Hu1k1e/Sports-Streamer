import httpx
import asyncio
import re
import logging
import base64
import time

logger = logging.getLogger("scraper_sportsurge")
logging.basicConfig(level=logging.INFO)

SPORTSURGE_URL = "https://sportsurge.ws"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Cache for events
_events_cache = None
_events_cache_ts = 0
CACHE_TTL = 300 # 5 minutes

async def get_sportsurge_events():
    global _events_cache, _events_cache_ts
    if _events_cache and (time.time() - _events_cache_ts) < CACHE_TTL:
        return _events_cache

    async with httpx.AsyncClient(timeout=15, headers={'User-Agent': USER_AGENT}) as client:
        try:
            response = await client.get(SPORTSURGE_URL)
            response.raise_for_status()
            html = response.text
            
            links = re.findall(r'<a[^>]+href=[\"\'](https://sportsurge\.ws/(?:event|watch)/[^\'\"]+)[\"\'][^>]*>(.*?)</a>', html, re.DOTALL)
            events = []
            
            for url, inner_html in links:
                # Extract title
                title_match = re.search(r'<h4[^>]*>(.*?)</h4>', inner_html, re.DOTALL | re.IGNORECASE)
                if title_match:
                    title = title_match.group(1).strip()
                else:
                    # Try to extract teams for /watch/ URLs
                    teams = re.findall(r'<div class="team-name-event-row">.*?<span[^>]*>(.*?)</span>', inner_html, re.DOTALL | re.IGNORECASE)
                    if len(teams) == 2:
                        title = f"{teams[0].strip()} vs {teams[1].strip()}"
                    else:
                        title = 'Unknown Event'
                
                title = re.sub(r'<[^>]+>', '', title) # strip any remaining tags just in case
                title = " ".join(title.split()) # clean up extra spaces/newlines
                
                # Extract sport
                sport_match = re.search(r'<div[^>]*ListelemeDuzen[^>]*text-center[^>]*>(.*?)</div>', inner_html, re.DOTALL | re.IGNORECASE)
                sport = sport_match.group(1).strip() if sport_match else 'Unknown'
                sport = re.sub(r'<[^>]+>', '', sport)
                
                # Extract date/time? Sportsurge doesn't easily expose this, but we can assume they are all "today" or "live"
                is_live = 'LIVE' in inner_html.upper()
                
                # Extract logo
                img_srcs = re.findall(r'<img[^>]+src=[\"\'](https?://[^\"\']+)[\"\']', inner_html, re.IGNORECASE)
                logo_url = img_srcs[0] if img_srcs else None
                
                # Create a unique ID for the M3U/Proxy
                event_id = url.strip('/').split('/')[-1]
                
                events.append({
                    "id": event_id,
                    "url": url,
                    "title": title,
                    "sport": sport,
                    "is_live": is_live,
                    "logo": logo_url
                })
            
            _events_cache = events
            _events_cache_ts = time.time()
            return events
            
        except Exception as e:
            logger.error(f"Error fetching sportsurge events: {e}")
            return []

async def get_sportsurge_stream(event_id: str):
    """
    Given a sportsurge event ID, finds the gooz embed, extracts the base64 m3u8, and returns it.
    """
    # event_id is the last part of the url, so we reconstruct it:
    # However, sometimes sportsurge URLs have category in them. 
    # But event_id usually matches across the board.
    # Actually, we should fetch the event URL from the cache, because the category could be anything (e.g. /event/ufc/...)
    
    events = await get_sportsurge_events()
    event_url = None
    for e in events:
        if e['id'] == event_id:
            event_url = e['url']
            break
            
    if not event_url:
        logger.error(f"Event ID {event_id} not found in sportsurge events cache.")
        return None

    async with httpx.AsyncClient(timeout=15, headers={'User-Agent': USER_AGENT}) as client:
        try:
            # 1. Fetch event page
            r1 = await client.get(event_url)
            r1.raise_for_status()
            
            # Find iframe or directly gooz.aapmains.net link
            iframe_match = re.search(r'href=[\"\'](https://gooz\.aapmains\.net/[^\'\"]+)[\"\']', r1.text)
            if not iframe_match:
                iframe_match = re.search(r'src=[\"\'](https://gooz\.aapmains\.net/[^\'\"]+)[\"\']', r1.text)
                
            if not iframe_match:
                # Sometimes the URL is single quotes or different path
                iframe_match = re.search(r'(https://gooz\.aapmains\.net/[^\'\"]+)', r1.text)
                
            if not iframe_match:
                logger.error(f"Could not find gooz.aapmains.net iframe on {event_url}")
                return None
                
            embed_url = iframe_match.group(1)
            logger.info(f"Found embed URL: {embed_url}")
            
            # 2. Fetch embed page
            r2 = await client.get(embed_url, headers={'Referer': SPORTSURGE_URL})
            r2.raise_for_status()
            
            # 3. Extract base64 source
            source_match = re.search(r'window\.atob\([\'\"]([a-zA-Z0-9=]+)[\'\"]\)', r2.text)
            if not source_match:
                logger.error("Could not find window.atob base64 string in embed page.")
                return None
                
            b64_string = source_match.group(1)
            try:
                m3u8_url = base64.b64decode(b64_string).decode('utf-8')
            except Exception as e:
                logger.error(f"Failed to decode base64 stream URL: {e}")
                return None
                
            logger.info(f"Successfully decoded m3u8 URL: {m3u8_url}")
            
            return {
                "url": m3u8_url,
                "headers": {
                    "Referer": "https://gooz.aapmains.net/",
                    "Origin": "https://gooz.aapmains.net",
                    "User-Agent": USER_AGENT
                }
            }
            
        except Exception as e:
            logger.error(f"Error extracting sportsurge stream: {e}")
            return None
