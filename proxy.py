import logging
import base64
import urllib.parse
from curl_cffi.requests import AsyncSession
from fastapi.responses import StreamingResponse, Response
from config import PROXY_HOST

logger = logging.getLogger("proxy")

# Headers that should NOT be forwarded (hop-by-hop or internal)
_SKIP_HEADERS = {
    "host", "connection", "accept-encoding", "content-length",
    "transfer-encoding", "keep-alive", "upgrade",
}


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
    
    # Ensure Origin is set — many CDNs require it
    if "origin" not in proxy_headers and "Origin" not in proxy_headers:
        referer = captured_headers.get("referer", "")
        if referer:
            try:
                parsed = urllib.parse.urlparse(referer)
                origin = f"{parsed.scheme}://{parsed.netloc}"
                proxy_headers["Origin"] = origin
            except Exception:
                pass
    
    return proxy_headers


import re

def rewrite_m3u8(content: str, base_url: str) -> str:
    """
    Rewrite an m3u8 playlist so all URLs point through our proxy.
    This is used both for captured content and freshly fetched content.
    """
    lines = content.split('\n')
    rewritten_lines = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if line.startswith('#'):
            if line.startswith('#EXT-X-STREAM-INF:') and 'CODECS=' not in line:
                line = line + ',CODECS="avc1.640028,mp4a.40.2"'
            elif line.startswith('#EXT-X-KEY:'):
                # Rewrite URI in EXT-X-KEY to route through proxy
                match = re.search(r'URI="([^"]+)"', line)
                if match:
                    key_url = match.group(1)
                    absolute_url = urllib.parse.urljoin(base_url, key_url)
                    b64_url = base64.urlsafe_b64encode(absolute_url.encode('utf-8')).decode('utf-8')
                    proxy_key_url = f"{PROXY_HOST}/proxy/media/{b64_url}.key"
                    line = line[:match.start(1)] + proxy_key_url + line[match.end(1):]
            rewritten_lines.append(line)
        else:
            # Segment or nested playlist URL
            absolute_url = urllib.parse.urljoin(base_url, line)
            
            # Base64 encode the url so FFmpeg doesn't see .jpg in the path and get confused
            b64_url = base64.urlsafe_b64encode(absolute_url.encode('utf-8')).decode('utf-8')
            
            if ".m3u8" in absolute_url:
                rewritten_lines.append(f"{PROXY_HOST}/proxy/m3u8/{b64_url}.m3u8")
            else:
                rewritten_lines.append(f"{PROXY_HOST}/proxy/media/{b64_url}.ts")
    
    logger.info("M3U8 rewritten: %d lines", len(rewritten_lines))
    return "\n".join(rewritten_lines)


async def proxy_m3u8(url: str, headers: dict):
    """
    Fetches the M3U8 playlist using curl_cffi (Chrome TLS impersonation)
    and rewrites URLs so Jellyfin requests segments through our proxy.
    """
    try:
        proxy_headers = _build_proxy_headers(headers)
        logger.debug("Proxy headers: %s", {k: v[:60] if isinstance(v, str) else v for k, v in proxy_headers.items()})
        
        async with AsyncSession(impersonate="chrome") as session:
            response = await session.get(url, headers=proxy_headers, timeout=10)
        
        logger.info("M3U8 fetch: %d (url: %s)", response.status_code, url[:100])
        
        if response.status_code != 200:
            logger.error("M3U8 fetch failed: %d — %s", response.status_code, response.text[:200])
            return ""
        
        return rewrite_m3u8(response.text, str(response.url))
        
    except Exception as e:
        logger.error("Proxy M3U8 error: %s", e, exc_info=True)
        return ""


async def proxy_media(url: str, headers: dict, media_type: str = "video/MP2T"):
    """
    Fetches a video segment or encryption key using curl_cffi (Chrome TLS impersonation)
    and streams it to Jellyfin.
    """
    try:
        proxy_headers = _build_proxy_headers(headers)
        
        async def stream_generator():
            async with AsyncSession(impersonate="chrome") as session:
                response = await session.get(url, headers=proxy_headers, stream=True, timeout=15)
                if response.status_code != 200:
                    logger.error("Media fetch failed: %d for %s", response.status_code, url[:100])
                    yield b""
                    return
                
                async for chunk in response.aiter_content():
                    yield chunk

        return StreamingResponse(
            stream_generator(),
            media_type=media_type
        )
    except Exception as e:
        logger.error("Media stream error: %s", e)
        return Response(content=b"", media_type=media_type, status_code=502)
