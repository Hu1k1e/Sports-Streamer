import logging
import re
import httpx



import time
import asyncio

logger = logging.getLogger(__name__)

_events_cache = {
    "data": None,
    "timestamp": 0
}
CACHE_TTL = 180  # 3 minutes

async def get_webcric_events():
    """
    Scrapes go.webcric.com for live cricket matches.
    Only returns streams that are verified to be online.
    Returns a list of dicts: {"id": "url_slug", "title": "Match Title"}
    """
    if _events_cache["data"] is not None and (time.time() - _events_cache["timestamp"]) < CACHE_TTL:
        logger.debug("API cache HIT for 'webcric_events' (age %ds)", time.time() - _events_cache["timestamp"])
        return _events_cache["data"]

    events = []
    logger.info("Scraping WebCric for live events...")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://go.webcric.com/index.html", headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            
            # split by '<div class="card ' to iterate over match cards
            cards = response.text.split('<div class="card ')[1:]
            
            for c in cards:
                # Extract match name from <strong> tag
                h3_strong = re.search(r'<h3[^>]*>.*?<strong>(.*?)</strong>', c, re.DOTALL)
                match_name = h3_strong.group(1).strip() if h3_strong else 'Unknown'
                
                if match_name == 'Unknown':
                    continue
                    
                # Clean up match name for XML safety
                match_name = match_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    
                links = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', c, re.DOTALL)
                best_link = None
                best_text = ''
                
                # Priority list for matching best quality stream names
                priorities = [
                    "hd", "willow", "tnt", "sky", "fox", "premier", "star", "astro", "sports",
                    "high quality", "medium quality", "low quality"
                ]
                
                current_priority = 999
                
                for url, text in links:
                    text = re.sub(r'<[^>]+>', '', text).strip()
                    text_lower = text.lower()
                    
                    if text_lower == "live stream":
                        continue
                        
                    for idx, keyword in enumerate(priorities):
                        if keyword in text_lower:
                            if idx < current_priority:
                                current_priority = idx
                                best_link = url
                                best_text = text
                            break
                            
                if best_link:
                    # extract the id from the link
                    filename = best_link.split('/')[-1]
                    if filename.endswith('.htm'):
                        match_id = filename[:-4]
                    else:
                        match_id = filename.split('.')[0]
                        
                    # Extract logo image
                    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', c, re.IGNORECASE)
                    logo_url = ""
                    if img_match:
                        logo_url = img_match.group(1).strip()
                        if not logo_url.startswith('http'):
                            logo_url = f"https://go.webcric.com/{logo_url}"
                            
                    events.append({
                        "id": match_id,
                        "title": match_name,
                        "logo_url": logo_url
                    })
            
            # De-duplicate events by ID while preserving order
            seen = set()
            unique_events = []
            for event in events:
                if event["id"] not in seen:
                    seen.add(event["id"])
                    unique_events.append(event)
                    
            logger.info(f"Found {len(unique_events)} WebCric events, verifying live status...")
            
            # Verify which streams are actually live concurrently
            tasks = []
            for event in unique_events:
                tasks.append(get_webcric_stream(event["id"]))
                
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            live_events = []
            for event, result in zip(unique_events, results):
                if result is not None and not isinstance(result, Exception):
                    live_events.append(event)
            
            logger.info(f"Verified {len(live_events)} live WebCric events")
            
            _events_cache["data"] = live_events
            _events_cache["timestamp"] = time.time()
            
            return live_events
    except Exception as e:
        logger.error(f"Error scraping WebCric events: {e}")
        return []

async def get_webcric_stream(match_id: str):
    """
    Extracts the stream variables (ea, id, pk) for a given WebCric match ID.
    Returns the M3U8 URL and headers if successful, None otherwise.
    """
    logger.info(f"Extracting WebCric stream for {match_id}")
    url = f"https://go.webcric.com/{match_id}.htm"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            
            # Extract channel and g
            channel_match = re.search(r"channel\s*=\s*['\"]([^'\"]+)['\"]", response.text)
            g_match = re.search(r"g\s*=\s*['\"]([^'\"]+)['\"]", response.text)
            
            if not (channel_match and g_match):
                # Fallback: check if there's an iframe to another .htm page (like willow.htm)
                iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+\.htm)["\']', response.text)
                if iframe_match:
                    iframe_src = iframe_match.group(1)
                    iframe_url = f"https://go.webcric.com/{iframe_src}"
                    logger.info(f"Variables not found, fetching iframe {iframe_src} for {match_id}")
                    iframe_resp = await client.get(iframe_url, headers={"User-Agent": "Mozilla/5.0", "Referer": url})
                    iframe_resp.raise_for_status()
                    
                    channel_match = re.search(r"channel\s*=\s*['\"]([^'\"]+)['\"]", iframe_resp.text)
                    g_match = re.search(r"g\s*=\s*['\"]([^'\"]+)['\"]", iframe_resp.text)
            
            if not (channel_match and g_match):
                logger.error(f"Could not extract channel/g for {match_id}. Stream might be offline.")
                return None
                
            channel = channel_match.group(1)
            g = g_match.group(1)
            
            # Fetch upstream iframe from one.superover1.top
            iframe_url = f"https://one.superover1.top/hembedplayer/{channel}/{g}/850/480"
            iframe_headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://go.webcric.com/"}
            
            iframe_resp = await client.get(iframe_url, headers=iframe_headers)
            iframe_resp.raise_for_status()
            
            # Extract ea, pk, and id
            ea_match = re.search(r'ea\s*=\s*[\'"]([^\'"]+)[\'"]', iframe_resp.text)
            pk_match = re.search(r'var\s+pk\s*=\s*[\'"]([^\'"]+)[\'"]', iframe_resp.text)
            id_match = re.search(r'\?id=(\d+)', iframe_resp.text)
            
            if not (ea_match and pk_match and id_match):
                logger.error(f"Could not extract stream variables for {match_id}. Stream might be offline.")
                return None
                
            ea = ea_match.group(1)
            pk = pk_match.group(1)
            stream_id = id_match.group(1)
            
            # Decrypt the pk token (remove the 54th character, index 53)
            decrypted_pk = pk[:53] + pk[54:]
            
            # WebCric m3u8 uses port 8088
            m3u8_url = f"https://{ea}:8088/live/{channel}/playlist.m3u8?id={stream_id}&pk={decrypted_pk}"
            
            return {
                "url": m3u8_url,
                "headers": {
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://one.superover1.top/"
                }
            }
            
    except Exception as e:
        logger.error(f"Error extracting WebCric stream for {match_id}: {e}")
        return None
