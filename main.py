from fastapi import FastAPI, Response, Request
from scraper import get_events, get_stream_url
from proxy import proxy_m3u8, proxy_segment
from config import PROXY_HOST
import urllib.parse

app = FastAPI(title="Streamed.pk IPTV Proxy")

# Simple in-memory cache to store the latest headers 
# because segment proxying requests don't know the event origin.
stream_headers_cache = {}

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
        m3u.append(f'#EXTINF:-1 tvg-id="{i}" tvg-name="{name}" tvg-logo="" group-title="Live Sports",{name}')
        stream_url = f"{PROXY_HOST}/stream/{urllib.parse.quote(path, safe='')}"
        m3u.append(stream_url)
        
    return Response(content="\n".join(m3u), media_type="application/vnd.apple.mpegurl")

@app.api_route("/stream/{event_path:path}", methods=["GET", "HEAD"])
async def stream_event(event_path: str, request: Request):
    """
    Called by Jellyfin when a channel is played.
    We scrape the live M3U8 URL and return a rewritten proxy M3U8.
    """
    if request.method == "HEAD":
        # Fast response for Jellyfin probe
        return Response(status_code=200)
        
    stream_data = await get_stream_url(event_path)
    
    if not stream_data or not stream_data["url"]:
        return Response(content="Stream not found or offline", status_code=404)
        
    url = stream_data["url"]
    headers = stream_data["headers"]
    
    # Store headers globally for segment proxying later
    stream_headers_cache["latest"] = headers
    
    m3u8_content = await proxy_m3u8(url, headers)
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
