import time
import logging
import base64
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Response, Request, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory cache: { event_path: {"url": ..., "headers": ..., "timestamp": ...} }
stream_cache: dict[str, dict] = {}
MAX_STREAM_CACHE_SIZE = 50  # hard cap to prevent unbounded growth

# Per-stream header cache for segment proxying.
# Also stores a "latest" key as fallback.
stream_headers_cache: dict[str, dict] = {}


def _evict_expired_streams():
    """Remove all expired entries from the stream cache."""
    now = time.time()
    expired = [k for k, v in stream_cache.items() if (now - v["timestamp"]) >= STREAM_CACHE_TTL]
    for k in expired:
        stream_cache.pop(k, None)
        stream_headers_cache.pop(k, None)
    if expired:
        logger.debug("Evicted %d expired stream cache entries", len(expired))


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
        stream_headers_cache.pop(oldest_key, None)
    stream_cache[event_path] = {
        "url": url,
        "headers": headers,
        "timestamp": time.time()
    }
    logger.debug("Cached stream for '%s' (TTL: %ds, total: %d)", event_path, STREAM_CACHE_TTL, len(stream_cache))


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


@app.get("/playlist.m3u")
async def generate_playlist(request: Request):
    """
    Generate M3U playlist with all streams currently broadcasting (live + early starts).
    Uses /api/matches/all-today filtered by active streams.
    """
    events = await get_all_events()
    sports = await get_sports()
    
    base_url = str(request.base_url).rstrip('/')
    
    m3u = ["#EXTM3U"]
    for event in events:
        name = event["name"]
        match_id = event["id"]
        category_id = event.get("category", "other")
        # Use the display name from /api/sports, fallback to capitalized id
        group_title = sports.get(category_id, category_id.capitalize())
        logo = event.get("logo_url", "")
        
        m3u.append(
            f'#EXTINF:-1 tvg-id="{match_id}" tvg-name="{name}"'
            f' tvg-logo="{logo}" group-title="{group_title}",{name}'
        )
        stream_url = f"{base_url}/stream/{match_id}"
        m3u.append(stream_url)
    
    logger.info("Generated M3U playlist with %d live channels", len(events))
    return Response(content="\n".join(m3u), media_type="application/vnd.apple.mpegurl")


@app.get("/epg.xml")
async def generate_epg():
    """
    Generate XMLTV EPG guide.
    Uses /api/matches/all for the full schedule.
    Live events get their end time extended so they show as 'On Now'.
    """
    events = await get_all_events()
    sports = await get_sports()
    
    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<tv generator-info-name="Streamed.pk Proxy">')
    
    # 1. Channels
    for event in events:
        match_id = event["id"]
        name = event["name"]
        safe_name = _xml_escape(name)
        logo = event.get("logo_url", "")
        
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
        
        # Event date is UNIX timestamp in ms
        timestamp_ms = event.get("date", 0)
        
        if timestamp_ms > 0:
            start_dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        else:
            start_dt = now
            
        # Determine end time
        if is_live and start_dt < now:
            # Event is currently live — extend end to at least 2h from now
            # so it stays visible as "On Now" in Jellyfin
            end_dt = max(
                start_dt + timedelta(hours=EPG_DEFAULT_DURATION_HOURS),
                now + timedelta(hours=2)
            )
        else:
            end_dt = start_dt + timedelta(hours=EPG_DEFAULT_DURATION_HOURS)
        
        # XMLTV date format: YYYYMMDDHHMMSS +0000
        start_str = start_dt.strftime("%Y%m%d%H%M%S +0000")
        end_str = end_dt.strftime("%Y%m%d%H%M%S +0000")
        
        xml.append(f'  <programme start="{start_str}" stop="{end_str}" channel="{match_id}">')
        xml.append(f'    <title lang="en">{safe_name}</title>')
        xml.append(f'    <desc lang="en">Live {group_title} stream for {safe_name}</desc>')
        if logo:
            xml.append(f'    <icon src="{_xml_escape(logo)}" />')
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
        stream_headers_cache[event_path] = cached["headers"]
        stream_headers_cache["latest"] = cached["headers"]
        
        proxy_base_url = str(request.base_url).rstrip('/')
        m3u8_content = await proxy_m3u8(cached["url"], cached["headers"], proxy_base_url)
        if m3u8_content:
            return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")
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
    
    # Store headers for segment proxying (per-stream + latest fallback)
    stream_headers_cache[event_path] = headers
    stream_headers_cache["latest"] = headers
    
    # Use captured content if available (avoids refetch + TLS fingerprint issues)
    proxy_base_url = str(request.base_url).rstrip('/')
    if captured_content:
        logger.info("GET: using captured m3u8 content (%d bytes)", len(captured_content))
        m3u8_content = rewrite_m3u8(captured_content, url, proxy_base_url)
    else:
        logger.info("GET: no captured content, fetching via proxy")
        m3u8_content = await proxy_m3u8(url, headers, proxy_base_url)
    
    if not m3u8_content:
        logger.error("GET: proxy_m3u8 returned empty content for %s", url[:120])
        return Response(content="Failed to fetch stream playlist", status_code=502)
    
    return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")


@app.api_route("/proxy/m3u8/{b64_url}.m3u8", methods=["GET", "HEAD"])
async def handle_proxy_m3u8(b64_url: str, request: Request):
    b64_url += "=" * ((4 - len(b64_url) % 4) % 4)
    url = base64.urlsafe_b64decode(b64_url).decode('utf-8')
    headers = stream_headers_cache.get("latest", {})
    proxy_base_url = str(request.base_url).rstrip('/')
    m3u8_content = await proxy_m3u8(url, headers, proxy_base_url)
    return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")


@app.api_route("/proxy/media/{filename}", methods=["GET", "HEAD"])
async def handle_proxy_media(filename: str):
    is_ts = filename.endswith(".ts")
    b64_url = filename.rsplit('.', 1)[0]
    b64_url += "=" * ((4 - len(b64_url) % 4) % 4)
    url = base64.urlsafe_b64decode(b64_url).decode('utf-8')
    headers = stream_headers_cache.get("latest", {})
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
    stream_headers_cache["latest"] = headers
    
    m3u8_content = await proxy_m3u8(m3u8_data["url"], headers, proxy_base_url)
    if m3u8_content:
        return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")
    return Response(content="Failed to proxy webcric m3u8", status_code=500)


if __name__ == "__main__":
    import uvicorn
    # When running locally without Docker
    uvicorn.run(app, host="0.0.0.0", port=5000)


def _sync_poster(sportsurge_title: str, streamed_events: list) -> str | None:
    best_score = 0.5
    best_logo = None
    
    s_clean = sportsurge_title.lower().replace(' vs ', ' ').replace('-', ' ')
    s_words = set(s_clean.split())
    if not s_words:
        return None
        
    for event in streamed_events:
        e_clean = event['name'].lower().replace(' vs ', ' ').replace('-', ' ')
        e_words = set(e_clean.split())
        if not e_words:
            continue
            
        overlap = len(s_words & e_words) / max(1, len(s_words | e_words))
        if overlap > best_score:
            best_score = overlap
            best_logo = event.get('logo_url')
            
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
    stream_headers_cache[f"sportsurge-{event_id}"] = headers
    stream_headers_cache["latest"] = headers
    
    proxy_base_url = str(request.base_url).rstrip('/')
    m3u8_content = await proxy_m3u8(url, headers, proxy_base_url)
    
    if m3u8_content:
        return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")
    return Response(content="Failed to proxy sportsurge m3u8", status_code=500)

