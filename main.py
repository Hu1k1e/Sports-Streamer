import time
import logging
from fastapi import FastAPI, Response, Request
from scraper import get_events, get_stream_url
from proxy import proxy_m3u8, proxy_segment
from config import PROXY_HOST, STREAM_CACHE_TTL

logger = logging.getLogger("main")

app = FastAPI(title="Streamed.pk IPTV Proxy")

# In-memory cache: { event_path: {"url": ..., "headers": ..., "timestamp": ...} }
stream_cache = {}

# Simple in-memory cache to store the latest headers 
# because segment proxying requests don't know the event origin.
stream_headers_cache = {}


def _get_cached_stream(event_path: str):
    """Return cached stream data if still valid, else None."""
    entry = stream_cache.get(event_path)
    if entry and (time.time() - entry["timestamp"]) < STREAM_CACHE_TTL:
        logger.debug("Cache HIT for '%s' (age: %.0fs)", event_path, time.time() - entry["timestamp"])
        return {"url": entry["url"], "headers": entry["headers"]}
    if entry:
        logger.debug("Cache EXPIRED for '%s'", event_path)
        del stream_cache[event_path]
    return None


def _set_cached_stream(event_path: str, url: str, headers: dict):
    """Cache a successful stream result."""
    stream_cache[event_path] = {
        "url": url,
        "headers": headers,
        "timestamp": time.time()
    }
    logger.debug("Cached stream for '%s' (TTL: %ds)", event_path, STREAM_CACHE_TTL)


@app.get("/")
def read_root():
    return {"status": "ok", "message": "Streamed.pk IPTV Proxy is running. Use /playlist.m3u for Jellyfin."}


@app.get("/playlist.m3u")
async def generate_playlist():
    events = await get_events()
    
    m3u = ["#EXTM3U"]
    for i, event in enumerate(events):
        name = event["name"]
        path = event["path"]
        
        # M3U format for Live TV
        # path already looks like "watch/ppv-brazil-vs-panama"
        # FastAPI's {event_path:path} captures everything after /stream/
        m3u.append(f'#EXTINF:-1 tvg-id="{i}" tvg-name="{name}" tvg-logo="" group-title="Live Sports",{name}')
        stream_url = f"{PROXY_HOST}/stream/{path}"
        m3u.append(stream_url)
    
    logger.info("Generated M3U playlist with %d channels", len(events))
    return Response(content="\n".join(m3u), media_type="application/vnd.apple.mpegurl")


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
        stream_headers_cache["latest"] = cached["headers"]
        m3u8_content = await proxy_m3u8(cached["url"], cached["headers"])
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
    logger.info("GET: scraper found m3u8: %s", url[:120])
    
    # Cache the result
    _set_cached_stream(event_path, url, headers)
    
    # Store headers globally for segment proxying later
    stream_headers_cache["latest"] = headers
    
    m3u8_content = await proxy_m3u8(url, headers)
    if not m3u8_content:
        logger.error("GET: proxy_m3u8 returned empty content for %s", url[:120])
        return Response(content="Failed to fetch stream playlist", status_code=502)
    
    return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")


@app.get("/proxy/m3u8")
async def handle_proxy_nested_m3u8(url: str):
    """
    Handles nested playlists recursively.
    """
    headers = stream_headers_cache.get("latest", {})
    m3u8_content = await proxy_m3u8(url, headers)
    return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")


@app.get("/proxy/segment")
async def handle_proxy_segment(url: str):
    """
    Streams the actual video file chunk (.ts) to Jellyfin.
    """
    headers = stream_headers_cache.get("latest", {})
    return await proxy_segment(url, headers)


if __name__ == "__main__":
    import uvicorn
    # When running locally without Docker
    uvicorn.run(app, host="0.0.0.0", port=5000)
