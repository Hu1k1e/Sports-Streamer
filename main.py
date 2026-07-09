import time
import logging
import base64
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Response, Request, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse
import httpx
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from scraper import get_live_events, get_all_events, get_sports, get_stream_url
from scraper_sportsurge import get_sportsurge_events, get_sportsurge_stream
from scraper_webcric import get_webcric_events, get_webcric_stream
from proxy import proxy_m3u8, proxy_media, rewrite_m3u8
from config import PROXY_HOST, STREAM_CACHE_TTL, STREAMED_PK_URL, EPG_DEFAULT_DURATION_HOURS

logger = logging.getLogger("main")

app = FastAPI(title="Streamed.pk IPTV Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Headers for all m3u8 playlist responses.
# - Cache-Control: iOS AVPlayer MUST re-fetch live playlists to discover new segments.
# - CORS: explicit backup in case CORSMiddleware doesn't fire (no Origin header).
_M3U8_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
}

# In-memory cache: { event_path: {"url": ..., "headers": ..., "timestamp": ...} }
stream_cache: dict[str, dict] = {}
MAX_STREAM_CACHE_SIZE = 50  # hard cap to prevent unbounded growth

# Per-stream header cache for segment proxying.
# Stored as { stream_id: {"headers": ..., "last_access": ...} }
# Has its OWN TTL (6 hours) independent of stream_cache, so headers survive
# long after the stream URL cache expires. Actively-used streams refresh
# their timestamp on every segment/playlist fetch.
stream_headers_cache: dict[str, dict] = {}
_HEADERS_CACHE_TTL = 6 * 3600  # 6 hours — covers any live sports event


@app.on_event("startup")
async def startup_event():
    from scraper import init_playwright
    await init_playwright()
    asyncio.create_task(prewarm_popular_streams_task())

@app.on_event("shutdown")
async def shutdown_event():
    from scraper import close_playwright
    await close_playwright()

async def prewarm_popular_streams_task():
    """
    Background task that periodically scrapes the top 10 live streams.
    Ensures that when a user clicks play on a popular game, it's an instant Cache HIT.
    """
    logger.info("Starting background pre-warming task...")
    # Let the server settle before first scrape
    await asyncio.sleep(10)
    
    while True:
        try:
            logger.info("[Pre-warm] Fetching live events for pre-warming...")
            events = await get_live_events()
            
            # Grab the top 5 streams
            top_events = events[:5]
            if top_events:
                logger.info("[Pre-warm] Found %d events, pre-warming top %d...", len(events), len(top_events))
            
            for event in top_events:
                event_path = event["id"]
                
                # Check if it's already properly cached and fresh
                cached = _get_cached_stream(event_path)
                if not cached:
                    logger.info("[Pre-warm] Pre-warming stream '%s'...", event_path)
                    stream_data = await get_stream_url(event_path)
                    
                    if stream_data and stream_data.get("url"):
                        _set_cached_stream(event_path, stream_data["url"], stream_data["headers"])
                        _set_stream_headers(event_path, stream_data["headers"])
                        logger.info("[Pre-warm] Successfully pre-warmed '%s'", event_path)
                    else:
                        logger.warning("[Pre-warm] Failed to pre-warm '%s'", event_path)
                
                # Small delay to prevent spiking CPU and triggering anti-bot
                await asyncio.sleep(2)
                
            # Success: Wait 600 seconds before next full cycle (10 mins)
            await asyncio.sleep(600)
            
        except Exception as e:
            logger.error("[Pre-warm] Error in background task: %s. Retrying in 5 seconds...", e)
            # Failure: Wait 5 seconds and retry the loop immediately
            await asyncio.sleep(5)

def _evict_expired_streams():
    """Remove all expired entries from the stream cache."""
    now = time.time()
    expired = [k for k, v in stream_cache.items() if (now - v["timestamp"]) >= STREAM_CACHE_TTL]
    for k in expired:
        stream_cache.pop(k, None)
        # DO NOT evict stream_headers_cache here — it has its own TTL.
        # Evicting headers kills active playback sessions.
    if expired:
        logger.debug("Evicted %d expired stream cache entries", len(expired))

    # Separately evict truly stale headers (no access in 6 hours)
    stale_headers = [k for k, v in stream_headers_cache.items()
                     if (now - v.get("last_access", 0)) >= _HEADERS_CACHE_TTL]
    for k in stale_headers:
        stream_headers_cache.pop(k, None)
    if stale_headers:
        logger.debug("Evicted %d stale header cache entries", len(stale_headers))


def _get_cached_stream(event_path: str):
    """Return cached stream data if still valid, else None."""
    _evict_expired_streams()
    entry = stream_cache.get(event_path)
    if entry and (time.time() - entry["timestamp"]) < STREAM_CACHE_TTL:
        logger.debug("Cache HIT for '%s' (age: %.0fs)", event_path, time.time() - entry["timestamp"])
        return {"url": entry["url"], "headers": entry["headers"]}
    # Entry missing or expired (already cleaned by evict)
    return None


def _set_cached_stream(event_path: str, url: str, headers: dict):
    """Cache a successful stream result. Evicts old entries if over capacity."""
    _evict_expired_streams()
    # If at capacity, drop the oldest entry
    while len(stream_cache) >= MAX_STREAM_CACHE_SIZE:
        oldest_key = min(stream_cache, key=lambda k: stream_cache[k]["timestamp"])
        logger.debug("Stream cache full (%d), evicting oldest: %s", len(stream_cache), oldest_key)
        stream_cache.pop(oldest_key, None)
    stream_cache[event_path] = {
        "url": url,
        "headers": headers,
        "timestamp": time.time()
    }
    logger.debug("Cached stream for '%s' (TTL: %ds, total: %d)", event_path, STREAM_CACHE_TTL, len(stream_cache))


def _get_stream_headers(stream_id: str) -> dict | None:
    """Get cached headers for a stream, refreshing the access timestamp.
    Returns the headers dict or None if not found."""
    entry = stream_headers_cache.get(stream_id)
    if entry:
        entry["last_access"] = time.time()
        return entry["headers"]
    return None


def _set_stream_headers(stream_id: str, headers: dict):
    """Store headers for a stream with a fresh access timestamp."""
    stream_headers_cache[stream_id] = {
        "headers": headers,
        "last_access": time.time()
    }



def _xml_escape(text: str) -> str:
    """Escape XML special characters."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


@app.get("/")
def read_root():
    return {"status": "ok", "message": "Streamed.pk IPTV Proxy is running. Use /playlist.m3u for Jellyfin."}


@app.get("/proxy/image/{match_id}.webp")
async def proxy_image(match_id: str):
    """
    Proxy the event logo to prevent Jellyfin SQLite URL truncation and
    guarantee cache busting with a clean, short URL.
    """
    events = await get_all_events()
    event = next((e for e in events if e["id"] == match_id), None)
    
    if not event or not event.get("logo_url"):
        # Fallback to an empty 1x1 transparent WebP/PNG if not found
        return Response(status_code=404)
        
    logo_url = event["logo_url"]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(logo_url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/webp")
            return Response(content=resp.content, media_type=content_type)
    except Exception as e:
        logger.error("Failed to proxy image %s: %s", match_id, e)
        raise HTTPException(status_code=500, detail="Failed to load image")



@app.get("/playlist.m3u")
async def generate_playlist(request: Request):
    """
    Generate M3U playlist with all streams currently broadcasting (live + early starts).
    Uses /api/matches/all-today filtered by active streams.
    """
    events = await get_all_events()
    sports = await get_sports()
    
    base_url = str(request.base_url).rstrip('/')
    
    # Custom sort: football (soccer) first, then alphabetical by sport, then name
    def _sort_key(e):
        cat = e.get('category', 'other')
        display = sports.get(cat, cat.capitalize())
        # Football gets sort prefix '0' so it appears first
        prefix = '0' if cat == 'football' else '1'
        return (prefix, display, e.get('name', ''))
    
    events.sort(key=_sort_key)
    
    # Assign channel numbers: football gets 1-N, then other sports follow sequentially
    # Each sport group is contiguous so Jellyfin groups them together
    channel_number = 1
    
    m3u = ["#EXTM3U"]
    for event in events:
        name = event["name"]
        match_id = event["id"]
        category_id = event.get("category", "other")
        group_title = sports.get(category_id, category_id.capitalize())
        logo = event.get("logo_url", "")
        if logo:
            logo = f"{base_url}/proxy/image/{match_id}.webp?v=6"  # proxied, short URL with extension
        
        m3u.append(
            f'#EXTINF:-1 tvg-id="{match_id}" tvg-name="{name}"'
            f' tvg-chno="{channel_number}"'
            f' tvg-logo="{logo}" group-title="{group_title}",{name}'
        )
        stream_url = f"{base_url}/stream/{match_id}"
        m3u.append(stream_url)
        channel_number += 1
    
    logger.info("Generated M3U playlist with %d live channels", len(events))
    return Response(content="\n".join(m3u), media_type="application/vnd.apple.mpegurl")


@app.get("/epg.xml")
async def generate_epg(request: Request):
    """
    Generate XMLTV EPG guide.
    Uses /api/matches/all for the full schedule.
    Live events get their end time extended so they show as 'On Now'.
    """
    events = await get_all_events()
    sports = await get_sports()
    
    # Custom sort: football (soccer) first, then alphabetical by sport, then name
    def _sort_key(e):
        cat = e.get('category', 'other')
        display = sports.get(cat, cat.capitalize())
        prefix = '0' if cat == 'football' else '1'
        return (prefix, display, e.get('name', ''))
    
    events.sort(key=_sort_key)
    
    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<tv generator-info-name="Streamed.pk Proxy">')
    
    base_url = str(request.base_url).rstrip('/')
    
    # 1. Channels
    for event in events:
        match_id = event["id"]
        name = event["name"]
        safe_name = _xml_escape(name)
        logo = event.get("logo_url", "")
        if logo:
            logo = f"{base_url}/proxy/image/{match_id}.webp?v=6"
        
        xml.append(f'  <channel id="{match_id}">')
        xml.append(f'    <display-name>{safe_name}</display-name>')
        if logo:
            xml.append(f'    <icon src="{_xml_escape(logo)}" />')
        xml.append(f'  </channel>')
        
    # 2. Programmes
    now = datetime.now(timezone.utc)
    for event in events:
        match_id = event["id"]
        name = event["name"]
        category_id = event.get("category", "other")
        group_title = sports.get(category_id, category_id.capitalize())
        safe_name = _xml_escape(name)
        is_live = event.get("is_live", False)
        
        # Use the best available image for this programme's icon.
        programme_icon = event.get("logo_url", "")
        if programme_icon:
            programme_icon = f"{base_url}/proxy/image/{match_id}.webp?v=6"
        
        # Event date is UNIX timestamp in ms
        timestamp_ms = event.get("date", 0)
        
        if timestamp_ms > 0:
            start_dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        else:
            start_dt = now
            
        # Determine end time
        if is_live and start_dt < now:
            end_dt = max(
                start_dt + timedelta(hours=EPG_DEFAULT_DURATION_HOURS),
                now + timedelta(hours=2)
            )
        else:
            end_dt = start_dt + timedelta(hours=EPG_DEFAULT_DURATION_HOURS)
        
        start_str = start_dt.strftime("%Y%m%d%H%M%S +0000")
        end_str = end_dt.strftime("%Y%m%d%H%M%S +0000")
        
        xml.append(f'  <programme start="{start_str}" stop="{end_str}" channel="{match_id}">')
        xml.append(f'    <title lang="en">{safe_name}</title>')
        xml.append(f'    <desc lang="en">Live {group_title} stream for {safe_name}</desc>')
        if programme_icon:
            xml.append(f'    <icon src="{_xml_escape(programme_icon)}" />')
        xml.append(f'    <category lang="en">{_xml_escape(group_title)}</category>')
        xml.append(f'  </programme>')
        
    xml.append('</tv>')
    
    logger.info("Generated EPG XMLTV with %d channels", len(events))
    return Response(content="\n".join(xml), media_type="application/xml")


import time

@app.api_route("/stream/{event_path:path}", methods=["GET", "HEAD"])
async def stream_event(event_path: str, request: Request):
    """
    Called by Jellyfin when a channel is played.
    We scrape the live M3U8 URL and return a rewritten proxy M3U8.
    """
    logger.info("[%s] /stream/%s", request.method, event_path)
    
    if request.method == "HEAD":
        # Check cache first — if we have a recent valid stream, confirm it
        cached = _get_cached_stream(event_path)
        if cached and cached["url"]:
            logger.info("HEAD: cache hit, returning 200")
            return Response(status_code=200, headers={"Content-Type": "application/vnd.apple.mpegurl"})
        
        # No cache — return 200 optimistically (Jellyfin expects quick HEAD responses;
        # doing a full Playwright scrape here would time out the probe)
        logger.info("HEAD: no cache, returning 200 optimistically")
        return Response(status_code=200, headers={"Content-Type": "application/vnd.apple.mpegurl"})
    
    # --- GET request: actually resolve and proxy the stream ---
    
    # Check cache first
    cached = _get_cached_stream(event_path)
    if cached and cached["url"]:
        logger.info("GET: serving from cache: %s", cached["url"][:120])
        _set_stream_headers(event_path, cached["headers"])
        
        proxy_base_url = str(request.base_url).rstrip('/')
        m3u8_content = await proxy_m3u8(cached["url"], cached["headers"], proxy_base_url, stream_id=event_path)
        if m3u8_content:
            return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl", headers=_M3U8_HEADERS)
        else:
            logger.warning("GET: cached URL returned empty m3u8, invalidating cache")
            stream_cache.pop(event_path, None)
    
    # Cache miss or invalid — do a fresh scrape
    logger.info("GET: cache miss, starting Playwright scrape for '%s'", event_path)
    stream_data = await get_stream_url(event_path)
    
    if not stream_data or not stream_data.get("url"):
        logger.error("GET: scraper returned no m3u8 URL for '%s'", event_path)
        return Response(content="Stream not found or offline", status_code=404)
    
    url = stream_data["url"]
    headers = stream_data["headers"]
    captured_content = stream_data.get("content")
    logger.info("GET: scraper found m3u8: %s", url[:120])
    
    # Cache the result
    _set_cached_stream(event_path, url, headers)
    
    # Store headers for segment proxying
    _set_stream_headers(event_path, headers)
    
    # Use captured content if available (avoids refetch + TLS fingerprint issues)
    proxy_base_url = str(request.base_url).rstrip('/')
    if captured_content:
        logger.info("GET: using captured m3u8 content (%d bytes)", len(captured_content))
        m3u8_content = rewrite_m3u8(captured_content, url, proxy_base_url, stream_id=event_path)
    else:
        logger.info("GET: no captured content, fetching via proxy")
        m3u8_content = await proxy_m3u8(url, headers, proxy_base_url, stream_id=event_path)
    
    if not m3u8_content:
        logger.error("GET: proxy_m3u8 returned empty content for %s", url[:120])
        return Response(content="Failed to fetch stream playlist", status_code=502)
    
    return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl", headers=_M3U8_HEADERS)


@app.api_route("/proxy/m3u8/{b64_url}.m3u8", methods=["GET", "HEAD"])
async def handle_proxy_m3u8(b64_url: str, request: Request):
    b64_url += "=" * ((4 - len(b64_url) % 4) % 4)
    url = base64.urlsafe_b64decode(b64_url).decode('utf-8')
    stream_id = request.query_params.get("sid", "")
    headers = _get_stream_headers(stream_id)
    if not headers:
        logger.warning("No cached headers for stream '%s' — session may have expired", stream_id)
        return Response(content="Stream session expired", status_code=502)
    proxy_base_url = str(request.base_url).rstrip('/')
    m3u8_content = await proxy_m3u8(url, headers, proxy_base_url, stream_id=stream_id)
    return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl", headers=_M3U8_HEADERS)


@app.api_route("/proxy/media/{filename}", methods=["GET", "HEAD"])
async def handle_proxy_media(filename: str, request: Request):
    is_ts = filename.endswith(".ts")
    b64_url = filename.rsplit('.', 1)[0]
    b64_url += "=" * ((4 - len(b64_url) % 4) % 4)
    url = base64.urlsafe_b64decode(b64_url).decode('utf-8')
    stream_id = request.query_params.get("sid", "")
    headers = _get_stream_headers(stream_id)
    if not headers:
        logger.warning("No cached headers for stream '%s' — session may have expired", stream_id)
        return Response(content=b"", status_code=502)
    media_type = "video/MP2T" if is_ts else "application/octet-stream"
    return await proxy_media(url, headers, media_type)



@app.api_route("/webcric.m3u", methods=["GET", "HEAD"])
async def generate_webcric_playlist(request: Request):
    events = await get_webcric_events()
    base_url = str(request.base_url).rstrip('/')
    
    m3u = ["#EXTM3U"]
    for event in events:
        logo_url = event.get('logo_url') or f"{base_url}/api/images/badge/default"
        title = "[LIVE] " + event['title']
            
        m3u.append(f'#EXTINF:-1 tvg-id="{event["id"]}" tvg-name="{event["title"]}" tvg-logo="{logo_url}" group-title="Cricket",{title}')
        m3u.append(f"{base_url}/webcric/stream/{event['id']}")
        
    return Response(content="\n".join(m3u), media_type="application/vnd.apple.mpegurl")

@app.api_route("/webcric.xml", methods=["GET", "HEAD"])
async def generate_webcric_epg(request: Request):
    events = await get_webcric_events()
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="Streamed.pk Proxy">']
    
    for event in events:
        xml.append(f'  <channel id="{event["id"]}">')
        xml.append(f'    <display-name>{_xml_escape(event["title"])}</display-name>')
        
        logo = event.get('logo_url')
        if logo:
            xml.append(f'    <icon src="{_xml_escape(logo)}" />')
        xml.append(f'  </channel>')
        
        now = datetime.now(timezone.utc)
        start_str = now.strftime('%Y%m%d%H%M%S +0000')
        end_str = (now + timedelta(hours=24)).strftime('%Y%m%d%H%M%S +0000')
        
        xml.append(f'  <programme start="{start_str}" stop="{end_str}" channel="{event["id"]}">')
        xml.append(f'    <title lang="en">{_xml_escape(event["title"])}</title>')
        xml.append(f'    <desc lang="en">Watch Live on WebCric</desc>')
        logo = event.get('logo_url')
        if logo:
            xml.append(f'    <icon src="{_xml_escape(logo)}" />')
        xml.append(f'    <category lang="en">Cricket</category>')
        xml.append(f'  </programme>')
        
    xml.append('</tv>')
    return Response(content="\n".join(xml), media_type="application/xml")

@app.api_route("/webcric/stream/{match_id}", methods=["GET", "HEAD"])
async def webcric_stream_proxy(match_id: str, request: Request):
    """
    Proxy route for WebCric that intercepts the M3U8 payload
    and injects proxy paths for segment URLs.
    """
    if request.method == "HEAD":
        return Response(status_code=200, headers={"Content-Type": "application/vnd.apple.mpegurl"})
        
    m3u8_data = await get_webcric_stream(match_id)
    if not m3u8_data:
        return Response("Stream not found or could not be decrypted", status_code=404)
        
    proxy_base_url = str(request.base_url).rstrip('/')
    headers = m3u8_data["headers"]
    stream_id = f"webcric-{match_id}"
    _set_stream_headers(stream_id, headers)
    
    m3u8_content = await proxy_m3u8(m3u8_data["url"], headers, proxy_base_url, stream_id=stream_id)
    if m3u8_content:
        return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")
    return Response(content="Failed to proxy webcric m3u8", status_code=500)


if __name__ == "__main__":
    import uvicorn
    # When running locally without Docker
    uvicorn.run(app, host="0.0.0.0", port=5000)


def _sync_poster(sportsurge_title: str, streamed_events: list) -> str | None:
    best_score = 0.85
    best_logo = None
    best_is_live = False
    
    import re
    # Remove leading channel numbers like "11 " from the title
    s_clean = re.sub(r'^\d+\s+', '', sportsurge_title.lower())
    s_clean = s_clean.replace(' vs ', ' ').replace('-', ' ')
    s_words = set(s_clean.split())
    if not s_words:
        return None
        
    for event in streamed_events:
        e_clean = event['name'].lower().replace(' vs ', ' ').replace('-', ' ')
        e_words = set(e_clean.split())
        if not e_words:
            continue
            
        overlap = len(s_words & e_words) / max(1, len(s_words | e_words))
        
        # If one title is entirely contained in the other, treat as a very strong match
        if s_words.issubset(e_words) or e_words.issubset(s_words):
            overlap = max(overlap, 0.9)
            
        is_live = event.get('is_live', False)
        
        # Prioritize live games. If a game is live, we give it a slight edge
        # so an active game's poster overwrites an expired game's poster.
        if overlap > best_score or (overlap >= best_score and is_live and not best_is_live):
            best_score = overlap
            best_logo = event.get('logo_url')
            best_is_live = is_live
            
    return best_logo

@app.api_route("/sportsurge.m3u", methods=["GET", "HEAD"])
async def generate_sportsurge_playlist(request: Request):
    events = await get_sportsurge_events()
    streamed_events = await get_all_events()
    base_url = str(request.base_url).rstrip('/')
    
    m3u = ["#EXTM3U"]
    for event in events:
        if not event.get('is_live'):
            continue
            
        synced_logo = _sync_poster(event['title'], streamed_events)
        logo_url = synced_logo or event.get('logo') or f"{base_url}/api/images/badge/default"
        title = "[LIVE] " + event['title']
            
        m3u.append(f'#EXTINF:-1 tvg-id="{event["id"]}" tvg-name="{event["title"]}" tvg-logo="{logo_url}" group-title="{event["sport"]}",{title}')
        m3u.append(f"{base_url}/sportsurge/stream/{event['id']}")
        
    return Response(content="\n".join(m3u), media_type="application/vnd.apple.mpegurl")


@app.api_route("/sportsurge.xml", methods=["GET", "HEAD"])
async def generate_sportsurge_epg(request: Request):
    events = await get_sportsurge_events()
    streamed_events = await get_all_events()
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="Streamed.pk Proxy">']
    
    for event in events:
        if not event.get('is_live'):
            continue
            
        xml.append(f'  <channel id="{event["id"]}">')
        xml.append(f'    <display-name>{_xml_escape(event["title"])}</display-name>')
        
        synced_logo = _sync_poster(event['title'], streamed_events)
        logo_url = synced_logo or event.get('logo')
        if logo_url:
            xml.append(f'    <icon src="{_xml_escape(logo_url)}" />')
        xml.append(f'  </channel>')
        
        # We don\'t have real times for sportsurge, so just use current time to +24h
        start_time = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S +0000")
        stop_time = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y%m%d%H%M%S +0000")
        
        xml.append(f'  <programme start="{start_time}" stop="{stop_time}" channel="{event["id"]}">')
        xml.append(f'    <title lang="en">{_xml_escape(event["title"])}</title>')
        xml.append(f'    <desc lang="en">{_xml_escape(event["sport"])} - Watch Live on Sportsurge</desc>')
        if logo_url:
            xml.append(f'    <icon src="{_xml_escape(logo_url)}" />')
        xml.append(f'  </programme>')
        
    xml.append('</tv>')
    return Response(content="\n".join(xml), media_type="application/xml")


@app.api_route("/sportsurge/stream/{event_id}", methods=["GET", "HEAD"])
async def stream_sportsurge_event(request: Request, event_id: str):
    if request.method == "HEAD":
        return Response(status_code=200, headers={"Content-Type": "application/vnd.apple.mpegurl"})
        
    logger.info(f"Sportsurge GET request for {event_id}")
    stream_data = await get_sportsurge_stream(event_id)
    
    if not stream_data or not stream_data.get("url"):
        return Response(content="Stream not found or offline", status_code=404)
        
    url = stream_data["url"]
    headers = stream_data["headers"]
    
    # Store headers for segment proxying
    _set_stream_headers(f"sportsurge-{event_id}", headers)
    
    proxy_base_url = str(request.base_url).rstrip('/')
    m3u8_content = await proxy_m3u8(url, headers, proxy_base_url, stream_id=f"sportsurge-{event_id}")
    
    if m3u8_content:
        return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")
    return Response(content="Failed to proxy sportsurge m3u8", status_code=500)

