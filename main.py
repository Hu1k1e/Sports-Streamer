import time
import logging
import base64
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Response, Request, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse
import httpx
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from scraper import get_live_events, get_all_events, get_sports, get_stream_urls
from scraper_webcric import get_webcric_events, get_webcric_stream
from scraper_livextv import get_livextv_events, get_livextv_stream
from proxy import proxy_m3u8, proxy_media, rewrite_m3u8, fetch_and_rewrite_best_sub_playlist
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
    asyncio.create_task(stream_health_check_task())
    asyncio.create_task(active_stream_keeper_task())

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
                    streams = await get_stream_urls(event_path, max_streams=3)
                    
                    if streams:
                        _set_cached_streams(event_path, streams)
                        _set_stream_headers(event_path, streams[0]["headers"])
                        logger.info("[Pre-warm] Successfully pre-warmed '%s' with %d streams", event_path, len(streams))
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

async def stream_health_check_task():
    """
    Periodically check the health of cached streams. If a stream is dead,
    evict it from the cache so the next request triggers a fresh scrape.
    Runs every 2 minutes.
    """
    logger.info("Starting stream health check task...")
    await asyncio.sleep(15)
    
    while True:
        try:
            now = time.time()
            # Only check streams that are currently cached
            keys_to_check = list(stream_cache.keys())
            if not keys_to_check:
                await asyncio.sleep(120)
                continue
                
            logger.info("[Health Check] Checking %d cached streams...", len(keys_to_check))
            for event_path in keys_to_check:
                entry = stream_cache.get(event_path)
                if not entry:
                    continue
                    
                # Skip if it's very fresh (< 30s) or about to expire anyway
                age = now - entry["timestamp"]
                if age < 30 or age > (STREAM_CACHE_TTL - 30):
                    continue
                    
                active_stream = entry["streams"][entry["active_index"]]
                url = active_stream["url"]
                headers = active_stream["headers"]
                
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        # Fast check — use GET with Range to avoid CDNs that block HEAD
                        check_headers = headers.copy()
                        check_headers["Range"] = "bytes=0-1024"
                        resp = await client.get(url, headers=check_headers)
                        if resp.status_code not in (200, 206):
                            logger.warning("[Health Check] Stream '%s' returned %d. Evicting from cache.", event_path, resp.status_code)
                            stream_cache.pop(event_path, None)
                except Exception as e:
                    logger.warning("[Health Check] Failed to check stream '%s': %s. Evicting.", event_path, e)
                    stream_cache.pop(event_path, None)
                    
                # Small delay between checks to avoid spamming
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error("[Health Check] Error in background task: %s", e)
            
        await asyncio.sleep(120)


async def active_stream_keeper_task():
    """
    Runs every 30 seconds. Finds streams Jellyfin is actively watching (last_access < 60s),
    and sends lightweight heartbeat requests to their BACKUP streams to prevent CDN tokens
    from expiring. If backups die, triggers a background Playwright scrape to replenish them.
    """
    logger.info("Starting active stream keeper task...")
    await asyncio.sleep(20)
    
    while True:
        try:
            now = time.time()
            for stream_id, headers_info in list(stream_headers_cache.items()):
                if now - headers_info["last_access"] < 60:
                    cached = stream_cache.get(stream_id)
                    if not cached:
                        continue
                        
                    active_idx = cached["active_index"]
                    valid_backups = []
                    
                    # Ping all backups that are AFTER the active_index
                    for i in range(active_idx + 1, len(cached["streams"])):
                        backup = cached["streams"][i]
                        if backup.get("dead"):
                            continue
                            
                        try:
                            check_headers = backup["headers"].copy()
                            check_headers["Range"] = "bytes=0-1024"
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(backup["url"], headers=check_headers, timeout=10)
                                if resp.status_code in (200, 206):
                                    valid_backups.append(backup)
                                else:
                                    logger.warning("[Keeper] Backup stream %s for %s died (HTTP %d).", backup.get("source"), stream_id, resp.status_code)
                                    backup["dead"] = True
                        except Exception:
                            backup["dead"] = True
                            
                    # If active stream has NO valid backups left, spawn a background scrape!
                    if not valid_backups:
                        logger.info("[Keeper] Active stream '%s' has NO valid backups! Spawning background scrape...", stream_id)
                        # We use asyncio.create_task so it doesn't block the keeper loop
                        asyncio.create_task(_replenish_backups(stream_id))
                        
        except Exception as e:
            logger.error("[Keeper] Error in background task: %s", e)
            
        await asyncio.sleep(30)


async def _replenish_backups(stream_id: str):
    """Silently fetches fresh streams and merges them into the cache."""
    try:
        new_streams = await get_stream_urls(stream_id, max_streams=3)
        if not new_streams:
            return
            
        cached = stream_cache.get(stream_id)
        if cached:
            # We don't want to overwrite the ACTIVE stream if it's still playing perfectly.
            # Just append the new streams as backups.
            active_stream = cached["streams"][cached["active_index"]]
            merged_streams = [active_stream]
            for ns in new_streams:
                if ns["url"] != active_stream["url"]:
                    merged_streams.append(ns)
            
            cached["streams"] = merged_streams
            cached["active_index"] = 0
            logger.info("[Keeper] Successfully replenished backups for '%s' (total: %d)", stream_id, len(merged_streams))
    except Exception as e:
        logger.error("[Keeper] Failed to replenish backups for '%s': %s", stream_id, e)


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


def _get_cached_entry(event_path: str):
    """Return the raw cache entry (with streams list and active_index) if valid."""
    _evict_expired_streams()
    entry = stream_cache.get(event_path)
    if entry and (time.time() - entry["timestamp"]) < STREAM_CACHE_TTL:
        return entry
    return None


def _get_cached_stream(event_path: str):
    """Return the currently active stream data if still valid, else None."""
    entry = _get_cached_entry(event_path)
    if entry:
        active_stream = entry["streams"][entry["active_index"]]
        logger.debug("Cache HIT for '%s' (active_index: %d, age: %.0fs)", event_path, entry["active_index"], time.time() - entry["timestamp"])
        return active_stream
    return None


def _set_cached_streams(event_path: str, streams: list):
    """Cache a list of successful streams. Evicts old entries if over capacity."""
    if not streams: return
    _evict_expired_streams()
    # If at capacity, drop the oldest entry
    if len(stream_cache) >= MAX_STREAM_CACHE_SIZE and event_path not in stream_cache:
        oldest_key = min(stream_cache.keys(), key=lambda k: stream_cache[k]["timestamp"])
        stream_cache.pop(oldest_key, None)
    
    stream_cache[event_path] = {
        "streams": streams,
        "active_index": 0,
        "timestamp": time.time()
    }
    logger.debug("Cached %d streams for '%s' (TTL: %ds, total: %d)", len(streams), event_path, STREAM_CACHE_TTL, len(stream_cache))


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
    livextv_events = await get_livextv_events()
    events = livextv_events + events
    event = next((e for e in events if e["id"] == match_id), None)
    
    if not event or not event.get("logo_url"):
        # Fallback to an empty 1x1 transparent WebP/PNG if not found
        return Response(status_code=404)
        
    logo_url = event["logo_url"]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
            }
            resp = await client.get(logo_url, headers=headers)
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
    
    # Custom sort: 24/7 Channels first, then football (soccer), then alphabetical by sport, then name
    def _sort_key(e):
        cat = e.get('category', 'other')
        display = sports.get(cat, cat.capitalize())
        if cat == '24/7 Channels':
            prefix = '00'
        elif cat == 'football':
            prefix = '01'
        else:
            prefix = '02'
        return (prefix, display, e.get('name', ''))
    
    events.sort(key=_sort_key)
    
    # Prepend LiveXTV events at the very top
    livextv_events = await get_livextv_events()
    events = livextv_events + events
    
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
    
    # Custom sort: 24/7 Channels first, then football (soccer), then alphabetical by sport, then name
    def _sort_key(e):
        cat = e.get('category', 'other')
        display = sports.get(cat, cat.capitalize())
        if cat == '24/7 Channels':
            prefix = '00'
        elif cat == 'football':
            prefix = '01'
        else:
            prefix = '02'
        return (prefix, display, e.get('name', ''))
    
    events.sort(key=_sort_key)
    
    livextv_events = await get_livextv_events()
    events = livextv_events + events
    
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

    # For streamed.pk events, check if a better embed source is available.
    # Skip this for livextv channels — they don't exist in the streamed.pk API.
    if cached and cached.get("url") and not event_path.startswith("livextv-"):
        from scraper import _get_embed_urls
        embed_urls = await _get_embed_urls(event_path)
        if embed_urls:
            best_embed = embed_urls[0]
            if cached.get("embed_url") and cached["embed_url"] != best_embed["url"]:
                logger.info("GET: Better stream source detected (%s vs %s). Invalidating cache to upgrade stream.", best_embed["source"], cached.get("source"))
                stream_cache.pop(event_path, None)
                cached = None

    if cached and cached.get("url"):
        logger.info("GET: serving from cache: %s", cached["url"][:120])
        _set_stream_headers(event_path, cached["headers"])
        
        proxy_base_url = str(request.base_url).rstrip('/')
        m3u8_content = await proxy_m3u8(cached["url"], cached["headers"], proxy_base_url, stream_id=event_path)
        if m3u8_content:
            return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl", headers=_M3U8_HEADERS)
        else:
            logger.warning("GET: cached URL returned empty m3u8, invalidating cache and triggering failover scrape")
            stream_cache.pop(event_path, None)
    
    # Cache miss or invalid (or failover) — do a fresh scrape
    logger.info("GET: cache miss or failover, starting stream resolution for '%s'", event_path)
    if event_path.startswith("livextv-"):
        streams = await get_livextv_stream(event_path, max_streams=1)
    else:
        streams = await get_stream_urls(event_path, max_streams=1)
    
    if not streams:
        logger.error("GET: scraper returned no m3u8 URL for '%s'", event_path)
        return Response(content="Stream not found or offline", status_code=404)
    
    active_stream = streams[0]
    url = active_stream["url"]
    headers = active_stream["headers"]
    captured_content = active_stream.get("content")
    logger.info("GET: scraper found m3u8: %s", url[:120])
    
    # Cache the result
    _set_cached_streams(event_path, streams)
    
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

    cached_entry = _get_cached_entry(stream_id)
    if cached_entry and cached_entry["active_index"] > 0:
        active_stream = cached_entry["streams"][cached_entry["active_index"]]
        if "sub_playlist_url" in active_stream:
            url = active_stream["sub_playlist_url"]

    headers = _get_stream_headers(stream_id)
    if not headers:
        logger.warning("No cached headers for stream '%s' — session may have expired", stream_id)
        return Response(content="Stream session expired", status_code=502)
    proxy_base_url = str(request.base_url).rstrip('/')
    m3u8_content = await proxy_m3u8(url, headers, proxy_base_url, stream_id=stream_id)
    if not m3u8_content:
        logger.warning("Sub-playlist fetch failed for stream '%s', attempting failover", stream_id)
        
        # --- LiveXTV auto-refresh: re-extract a fresh signed URL ---
        # Signed CDN URLs expire after some time. For livextv channels, we can
        # get a new one in ~100ms via direct HTTP extraction (no Playwright).
        if stream_id.startswith("livextv-"):
            from scraper_livextv import get_livextv_stream
            logger.info("[AutoRefresh] Re-extracting signed URL for '%s'", stream_id)
            fresh_streams = await get_livextv_stream(stream_id, max_streams=1)
            if fresh_streams:
                fresh = fresh_streams[0]
                # Update cache with fresh signed URL
                _set_cached_streams(stream_id, fresh_streams)
                _set_stream_headers(stream_id, fresh["headers"])
                # Fetch the fresh chunklist
                m3u8_content = await proxy_m3u8(fresh["url"], fresh["headers"], proxy_base_url, stream_id=stream_id)
                if m3u8_content:
                    logger.info("[AutoRefresh] Successfully refreshed signed URL for '%s'", stream_id)
                    return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl", headers=_M3U8_HEADERS)
        
        # --- Streamed.pk seamless failover to backup streams ---
        cached = _get_cached_entry(stream_id)
        if cached and cached["active_index"] + 1 < len(cached["streams"]):
            cached["active_index"] += 1
            new_stream = cached["streams"][cached["active_index"]]
            logger.info("Seamless failover to backup stream: %s", new_stream.get("source"))
            
            _set_stream_headers(stream_id, new_stream["headers"])
            
            m3u8_content, new_sub_playlist_url = await fetch_and_rewrite_best_sub_playlist(new_stream["url"], new_stream["headers"], proxy_base_url, stream_id=stream_id)
            
            if m3u8_content:
                new_stream["sub_playlist_url"] = new_sub_playlist_url
                logger.info("Seamless failover successful for '%s'", stream_id)
                return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl", headers=_M3U8_HEADERS)
                
        # Total failure
        logger.error("No valid backup streams available for '%s', dropping connection.", stream_id)
        stream_cache.pop(stream_id, None)
        return Response(content="Failed to fetch sub-playlist", status_code=502)
        
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

