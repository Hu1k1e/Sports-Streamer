import httpx
import asyncio
import re
import time
import logging

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # 5 minutes
STREAM_CACHE_TTL = 3600  # 1 hour

_events_cache = None
_events_cache_time = 0
_stream_cache = {}

async def get_crichd_events() -> list[dict]:
    """
    Fetch live matches from crichd.is and parse them.
    Caches the result for 5 minutes.
    """
    global _events_cache, _events_cache_time
    now = time.time()
    
    if _events_cache is not None and (now - _events_cache_time) < CACHE_TTL:
        return _events_cache

    logger.info("Fetching CricHD events...")
    url = "https://crichd.is/index.php"
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text
            
            # Find all links on homepage
            links = re.findall(r'<a[^>]+href=["\'](https://crichd\.is/video/[^"\']+)["\'][^>]*>(.*?)</a>', html, re.DOTALL)
            events = []
            
            for url, inner_html in links:
                title = re.sub(r'<[^>]+>', '', inner_html).strip()
                title = " ".join(title.split()) # normalize spaces
                
                # Check if it's a live match (either has 'v' for 'vs' or says 'live')
                is_live = 'v' in title.lower() or 'live' in url.lower() or 'live' in title.lower()
                
                # Create a unique ID from the URL slug
                event_id = url.strip('/').split('/')[-1]
                
                # CricHD is strictly cricket
                sport = "Cricket"
                
                if is_live and title:
                    events.append({
                        "id": event_id,
                        "url": url,
                        "title": title,
                        "sport": sport,
                        "is_live": True, # CricHD homepage generally only lists live items
                        "logo": None # Handled via _sync_poster in main.py
                    })
            
            # Deduplicate by ID
            unique_events = {}
            for e in events:
                if e['id'] not in unique_events:
                    unique_events[e['id']] = e
            
            _events_cache = list(unique_events.values())
            _events_cache_time = now
            logger.info(f"Successfully fetched {len(_events_cache)} live CricHD events")
            return _events_cache
            
        except Exception as e:
            logger.error(f"Error fetching CricHD events: {e}")
            return _events_cache or []

async def get_crichd_stream(match_id: str) -> str | None:
    """
    Given a CricHD match_id (url slug), fetch the video page,
    navigate through the 1freecdn iframe, decrypt the pk token,
    and return the final M3U8 URL.
    """
    now = time.time()
    
    # Check cache and evict old entries
    keys_to_delete = [k for k, v in _stream_cache.items() if now - v["time"] > STREAM_CACHE_TTL]
    for k in keys_to_delete:
        del _stream_cache[k]
        
    if match_id in _stream_cache:
        return _stream_cache[match_id]["url"]

    url = f"https://crichd.is/video/{match_id}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    logger.info(f"Extracting CricHD stream for {match_id}")
    
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15) as client:
        try:
            # 1. Fetch main video page
            r = await client.get(url)
            r.raise_for_status()
            
            # Extract channel
            channel_match = re.search(r'channel\s*=\s*[\'"]([^\'"]+)[\'"]', r.text)
            if not channel_match:
                logger.error(f"Could not find channel for {match_id}")
                return None
            channel = channel_match.group(1)
            
            # 2. Fetch 1freecdn iframe
            iframe_url = f"https://1freecdn.xyz/hembedplayer/{channel}/1/960/540"
            iframe_headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://crichd.is/'}
            r_iframe = await client.get(iframe_url, headers=iframe_headers)
            r_iframe.raise_for_status()
            
            # 3. Extract variables from iframe
            ea_match = re.search(r'ea\s*=\s*[\'"]([^\'"]+)[\'"]', r_iframe.text)
            pk_match = re.search(r'var\s+pk\s*=\s*[\'"]([^\'"]+)[\'"]', r_iframe.text)
            id_match = re.search(r'\?id=(\d+)', r_iframe.text)
            
            if not (ea_match and pk_match and id_match):
                logger.error(f"Could not extract stream variables for {match_id}")
                return None
                
            ea = ea_match.group(1)
            pk = pk_match.group(1)
            stream_id = id_match.group(1)
            
            # 4. Decrypt pk (delete the 55th character / index 54)
            if len(pk) > 55:
                pk_decrypted = pk[:54] + pk[55:]
            else:
                pk_decrypted = pk
                
            # 5. Build final M3U8 URL
            final_m3u8 = f"https://{ea}/live/{channel}/playlist.m3u8?id={stream_id}&pk={pk_decrypted}"
            
            logger.info(f"Successfully decrypted CricHD stream for {match_id}")
            
            _stream_cache[match_id] = {
                "url": final_m3u8,
                "time": now
            }
            
            return final_m3u8
            
        except Exception as e:
            logger.error(f"Error fetching CricHD stream {match_id}: {e}")
            return None
