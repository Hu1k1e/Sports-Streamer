import logging
import base64
import time
import urllib.parse
import asyncio
from curl_cffi.requests import AsyncSession
from fastapi.responses import Response
from config import PROXY_HOST

logger = logging.getLogger("proxy")

# CORS headers injected on every proxy response so iOS AVPlayer
# can fetch segments cross-origin without being blocked.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "Content-Length, Content-Type",
}

# Headers that should NOT be forwarded (hop-by-hop or internal)
_SKIP_HEADERS = {
    "host", "connection", "accept-encoding", "content-length",
    "transfer-encoding", "keep-alive", "upgrade",
}


# Shared HTTP session for all proxy requests (lazy-initialized).
# Reusing a single session avoids per-request overhead and lets curl_cffi
# pool connections to frequently-hit CDN hosts.
# The session is recycled every 30 minutes to prevent stale connections
# (CDNs rotate IPs and long-lived sockets go dead silently).
_shared_session: AsyncSession | None = None
_session_created_at: float = 0.0
_session_last_activity: float = 0.0
_SESSION_MAX_AGE = 1800  # 30 minutes
_SESSION_IDLE_THRESHOLD = 300  # 5 minutes — only recycle if idle this long


async def _get_session() -> AsyncSession:
    """Return the shared AsyncSession, recycling only when idle.
    
    Active streams continuously update _session_last_activity.
    We only recycle when BOTH conditions are met:
      1. Session is older than _SESSION_MAX_AGE (30 min)
      2. No segment/playlist traffic in the last _SESSION_IDLE_THRESHOLD (5 min)
    This prevents mid-stream CDN disconnections caused by TLS fingerprint changes.
    """
    global _shared_session, _session_created_at, _session_last_activity
    now = time.time()
    age = now - _session_created_at
    idle = now - _session_last_activity
    
    needs_recycle = (
        _shared_session is None
        or (age > _SESSION_MAX_AGE and idle > _SESSION_IDLE_THRESHOLD)
    )
    
    if needs_recycle:
        if _shared_session is not None:
            logger.info("Recycling curl_cffi session (age: %.0fs, idle: %.0fs)", age, idle)
            try:
                _shared_session.close()
            except Exception:
                pass
        _shared_session = AsyncSession(impersonate="chrome")
        _session_created_at = now
    return _shared_session


def _touch_session_activity():
    """Mark that the session was just used for a segment/playlist fetch."""
    global _session_last_activity
    _session_last_activity = time.time()


def _build_proxy_headers(captured_headers: dict) -> dict:
    """
    Build proxy headers from the captured browser request headers.
    Forwards all safe headers to mimic the original browser request.
    Derives Origin from Referer if missing.
    """
    proxy_headers = {}
    
    for key, value in captured_headers.items():
        if key.lower() not in _SKIP_HEADERS and value:
            proxy_headers[key] = value
    
    # Ensure Origin/Referer are present if missing, but do not overwrite if already present.
    if "Origin" not in proxy_headers and "origin" not in proxy_headers:
        referer = captured_headers.get("referer") or captured_headers.get("Referer")
        if referer:
            try:
                parsed = urllib.parse.urlparse(referer)
                origin = f"{parsed.scheme}://{parsed.netloc}"
                proxy_headers["Origin"] = origin
            except Exception:
                pass
    
    return proxy_headers


import re

def rewrite_m3u8(content: str, base_url: str, proxy_base_url: str, stream_id: str = "") -> str:
    """
    Rewrite an m3u8 playlist so all URLs point through our proxy.
    This is used both for captured content and freshly fetched content.
    stream_id is appended as ?sid= so proxy routes can look up per-stream headers.
    """
    sid_param = f"?sid={stream_id}" if stream_id else ""
    lines = content.split('\n')
    
    # Check if it's a master playlist
    is_master = any(line.startswith('#EXT-X-STREAM-INF:') for line in lines)
    
    if is_master:
        variants = []
        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith('#EXT-X-STREAM-INF:'):
                url_line = lines[i+1].strip() if i+1 < len(lines) else ""
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                bw = int(bw_match.group(1)) if bw_match else 0
                variants.append({"bw": bw, "inf": line, "url": url_line})
                
        best_variant = max(variants, key=lambda v: v["bw"]) if variants else None
        
        new_lines = []
        all_variant_urls = {v["url"] for v in variants}
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#EXT-X-STREAM-INF:') or line in all_variant_urls:
                continue
            new_lines.append(line)
            
        if best_variant:
            inf = best_variant["inf"]
            new_lines.append(inf)
            absolute_url = urllib.parse.urljoin(base_url, best_variant["url"])
            b64_url = base64.urlsafe_b64encode(absolute_url.encode('utf-8')).decode('utf-8')
            new_lines.append(f"{proxy_base_url}/proxy/m3u8/{b64_url}.m3u8{sid_param}")
            
        logger.info("M3U8 master rewritten: forced highest bandwidth variant")
        return "\n".join(new_lines)

    rewritten_lines = []
    is_stream_inf = False
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if line.startswith('#'):
            if line.startswith('#EXT-X-STREAM-INF:'):
                is_stream_inf = True
            elif line.startswith('#EXT-X-KEY:'):
                # Rewrite URI in EXT-X-KEY to route through proxy
                match = re.search(r'URI="([^"]+)"', line)
                if match:
                    key_url = match.group(1)
                    absolute_url = urllib.parse.urljoin(base_url, key_url)
                    b64_url = base64.urlsafe_b64encode(absolute_url.encode('utf-8')).decode('utf-8')
                    proxy_key_url = f"{proxy_base_url}/proxy/media/{b64_url}.key{sid_param}"
                    line = line[:match.start(1)] + proxy_key_url + line[match.end(1):]
            rewritten_lines.append(line)
        else:
            # Segment or nested playlist URL
            absolute_url = urllib.parse.urljoin(base_url, line)
            
            # Base64 encode the url so FFmpeg doesn't see .jpg in the path and get confused
            b64_url = base64.urlsafe_b64encode(absolute_url.encode('utf-8')).decode('utf-8')
            
            if is_stream_inf or ".m3u8" in absolute_url:
                rewritten_lines.append(f"{proxy_base_url}/proxy/m3u8/{b64_url}.m3u8{sid_param}")
                is_stream_inf = False
            else:
                rewritten_lines.append(f"{proxy_base_url}/proxy/media/{b64_url}.ts{sid_param}")
    
    logger.info("M3U8 rewritten: %d lines", len(rewritten_lines))
    return "\n".join(rewritten_lines)


async def proxy_m3u8(url: str, headers: dict, proxy_base_url: str, stream_id: str = ""):
    """
    Fetches the M3U8 playlist using curl_cffi (Chrome TLS impersonation)
    and rewrites URLs so Jellyfin requests segments through our proxy.
    Includes retry logic with exponential backoff.
    """
    proxy_headers = _build_proxy_headers(headers)
    # Tell CDNs we want an HLS playlist, not HTML
    proxy_headers["Accept"] = "application/vnd.apple.mpegurl, application/x-mpegURL, */*"
    logger.debug("Proxy headers: %s", {k: v[:60] if isinstance(v, str) else v for k, v in proxy_headers.items()})
    
    session = await _get_session()
    backoff_delays = [0.5, 1.0, 2.0]  # exponential backoff
    
    for attempt in range(3):
        try:
            response = await session.get(url, headers=proxy_headers, timeout=20)
            _touch_session_activity()
            
            if response.status_code != 200:
                logger.error("M3U8 fetch failed (attempt %d/3): %d — %s", attempt + 1, response.status_code, response.text[:200])
                if attempt < 2:
                    await asyncio.sleep(backoff_delays[attempt])
                    continue
                return ""
            
            logger.info("M3U8 fetch: %d (url: %s)", response.status_code, url[:100])
            return rewrite_m3u8(response.text, str(response.url), proxy_base_url, stream_id=stream_id)
            
        except Exception as e:
            logger.error("Proxy M3U8 error (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(backoff_delays[attempt])
            else:
                logger.error("Proxy M3U8 final failure", exc_info=True)
                
    return ""


async def fetch_and_rewrite_best_sub_playlist(master_url: str, headers: dict, proxy_base_url: str, stream_id: str = "") -> str:
    """
    Called during a seamless failover. 
    It fetches the NEW master playlist, extracts the best variant, fetches its sub-playlist,
    rewrites it, and INJECTS a #EXT-X-DISCONTINUITY tag.
    """
    logger.info("Executing seamless failover sub-playlist extraction from: %s", master_url[:100])
    proxy_headers = _build_proxy_headers(headers)
    proxy_headers["Accept"] = "application/vnd.apple.mpegurl, application/x-mpegURL, */*"
    session = await _get_session()
    
    try:
        # 1. Fetch master playlist
        resp = await session.get(master_url, headers=proxy_headers, timeout=15)
        if resp.status_code != 200:
            logger.error("Failover master fetch failed: %d", resp.status_code)
            return ""
            
        lines = resp.text.split('\n')
        best_variant_url = None
        highest_bw = -1
        
        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith('#EXT-X-STREAM-INF:'):
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                bw = int(bw_match.group(1)) if bw_match else 0
                if bw > highest_bw and i + 1 < len(lines):
                    highest_bw = bw
                    best_variant_url = urllib.parse.urljoin(str(resp.url), lines[i+1].strip())
                    
        if not best_variant_url:
            # If no variants, assume the master URL is actually a sub-playlist
            best_variant_url = str(resp.url)
            
        # 2. Fetch sub playlist
        logger.info("Failover selected variant: %s", best_variant_url[:100])
        resp2 = await session.get(best_variant_url, headers=proxy_headers, timeout=15)
        if resp2.status_code != 200:
            logger.error("Failover sub-playlist fetch failed: %d", resp2.status_code)
            return ""
            
        # 3. Rewrite it
        rewritten = rewrite_m3u8(resp2.text, str(resp2.url), proxy_base_url, stream_id=stream_id)
        
        # 4. Inject #EXT-X-DISCONTINUITY right after #EXTM3U
        if rewritten.startswith("#EXTM3U"):
            parts = rewritten.split('\n', 1)
            if len(parts) > 1:
                return f"#EXTM3U\n#EXT-X-DISCONTINUITY\n{parts[1]}", best_variant_url
                
        return rewritten, best_variant_url
    except Exception as e:
        logger.error("Seamless failover fetch failed: %s", e)
        return "", ""


async def proxy_media(url: str, headers: dict, media_type: str = "video/MP2T"):
    """
    Fetches a video segment or encryption key using curl_cffi (Chrome TLS
    impersonation) and returns the full buffered content with an explicit
    Content-Length header.
    Includes retry logic with exponential backoff.
    Uses keep-alive (no Connection: close) so iOS can reuse TCP sockets.
    """
    proxy_headers = _build_proxy_headers(headers)
    session = await _get_session()
    backoff_delays = [0.5, 1.0, 2.0]
    
    for attempt in range(3):
        try:
            response = await session.get(url, headers=proxy_headers, timeout=30)
            _touch_session_activity()

            if response.status_code != 200:
                logger.error("Media fetch failed (attempt %d/3): %d for %s", attempt + 1, response.status_code, url[:100])
                if attempt < 2:
                    await asyncio.sleep(backoff_delays[attempt])
                    continue
                return Response(content=b"", media_type=media_type, status_code=502, headers=_CORS_HEADERS)

            body = response.content

            # Strip PNG steganography wrapper if present (anti-piracy measure
            # where CDNs wrap .ts segments inside a minimal PNG file)
            if body.startswith(b'\x89PNG\r\n\x1a\n'):
                iend_idx = body.find(b'IEND')
                if iend_idx != -1:
                    png_header_len = iend_idx + 8
                    body = body[png_header_len:]

            return Response(
                content=body,
                media_type=media_type,
                headers={"Content-Length": str(len(body)), **_CORS_HEADERS},
            )
            
        except Exception as e:
            logger.error("Media stream error (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(backoff_delays[attempt])
            else:
                logger.error("Media stream final failure for %s", url[:100])
                
    return Response(content=b"", media_type=media_type, status_code=502, headers=_CORS_HEADERS)
