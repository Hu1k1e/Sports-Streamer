import logging
import re
import httpx



logger = logging.getLogger(__name__)

async def get_webcric_events():
    """
    Scrapes go.webcric.com for live cricket matches.
    Returns a list of dicts: {"id": "url_slug", "title": "Match Title"}
    """
    events = []
    logger.info("Scraping WebCric for live events...")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://go.webcric.com/index.html", headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            
            # Extract matches using regex to be robust against missing BeautifulSoup dependencies
            matches = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', response.text, re.DOTALL)
            
            for url, title_raw in matches:
                title = re.sub(r'<[^>]+>', ' ', title_raw).strip()
                title = ' '.join(title.split())
                
                # Filter for likely matches
                if 'live' in url.lower() or 'live' in title.lower() or 'streaming' in title.lower() or 'v' in title.lower() or 'cricket' in title.lower() or 'stream' in url.lower() or url.endswith('.htm'):
                    # The URL slug will be used as the match ID
                    # e.g. "https://go.webcric.com/ipl-2025-cricket-live-streaming.htm" -> "ipl-2025-cricket-live-streaming"
                    filename = url.split('/')[-1]
                    if filename.endswith('.htm'):
                        match_id = filename[:-4]
                    else:
                        match_id = filename.split('.')[0]
                        
                    # Ignore matches with empty or garbage titles
                    if title and len(title) > 3 and title.upper() != "MATCH END":
                        events.append({
                            "id": match_id,
                            "title": title
                        })
            
            # De-duplicate events by ID while preserving order
            seen = set()
            unique_events = []
            for event in events:
                if event["id"] not in seen:
                    seen.add(event["id"])
                    unique_events.append(event)
                    
            logger.info(f"Found {len(unique_events)} WebCric events")
            return unique_events
            
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
                    "User-Agent": USER_AGENT,
                    "Referer": "https://one.superover1.top/"
                }
            }
            
    except Exception as e:
        logger.error(f"Error extracting WebCric stream for {match_id}: {e}")
        return None
