import logging
import urllib.parse
from curl_cffi.requests import AsyncSession
from fastapi.responses import StreamingResponse

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


def rewrite_m3u8(content: str, base_url: str) -> str:
    """
    Rewrite an m3u8 playlist so all URLs point through our proxy.
    This is used both for captured content and freshly fetched content.
    """
    lines = content.split('\n')
    rewritten_lines = []
    
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#'):
            # Segment or nested playlist URL
            absolute_url = urllib.parse.urljoin(base_url, line)
            encoded_url = urllib.parse.quote(absolute_url, safe='')
            
            if ".m3u8" in absolute_url:
                rewritten_lines.append(f"/proxy/m3u8?url={encoded_url}")
            else:
                rewritten_lines.append(f"/proxy/segment?url={encoded_url}")
        else:
            rewritten_lines.append(line)
    
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


async def proxy_segment(url: str, headers: dict):
    """
    Fetches a video segment using curl_cffi (Chrome TLS impersonation)
    and streams it to Jellyfin.
    """
    try:
        proxy_headers = _build_proxy_headers(headers)
        
        async with AsyncSession(impersonate="chrome") as session:
            response = await session.get(url, headers=proxy_headers, timeout=15)
        
        if response.status_code != 200:
            logger.error("Segment fetch failed: %d for %s", response.status_code, url[:100])
            return StreamingResponse(iter([b""]), media_type="video/MP2T", status_code=502)
        
        return StreamingResponse(
            iter([response.content]),
            media_type="video/MP2T",
            headers={"Content-Length": str(len(response.content))}
        )
    except Exception as e:
        logger.error("Segment stream error: %s", e)
        return StreamingResponse(iter([b""]), media_type="video/MP2T", status_code=502)
