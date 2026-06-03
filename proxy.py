import logging
import httpx
from fastapi.responses import StreamingResponse
import urllib.parse

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
            # Capitalize header names for conventional HTTP style
            proxy_headers[key] = value
    
    # Ensure Origin is set — many CDNs require it
    # Derive from Referer if not present
    if "origin" not in proxy_headers and "Origin" not in proxy_headers:
        referer = captured_headers.get("referer", "")
        if referer:
            try:
                parsed = urllib.parse.urlparse(referer)
                origin = f"{parsed.scheme}://{parsed.netloc}"
                proxy_headers["Origin"] = origin
                logger.debug("Derived Origin from Referer: %s", origin)
            except Exception:
                pass
    
    logger.debug("Proxy headers: %s", {k: v[:60] if isinstance(v, str) else v for k, v in proxy_headers.items()})
    return proxy_headers


async def proxy_m3u8(url: str, headers: dict):
    """
    Fetches the M3U8 playlist and rewrites URLs so Jellyfin requests
    segments through our proxy, allowing us to attach necessary headers.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            proxy_headers = _build_proxy_headers(headers)
            
            response = await client.get(url, headers=proxy_headers)
            
            logger.info("M3U8 fetch: %d %s (url: %s)", response.status_code, response.reason_phrase, url[:100])
            
            if response.status_code != 200:
                logger.error("M3U8 fetch failed: %d — %s", response.status_code, response.text[:200])
                return ""
            
            content = response.text
            base_url = str(response.url)
            
            lines = content.split('\n')
            rewritten_lines = []
            
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    # Segment or nested playlist URL
                    absolute_url = urllib.parse.urljoin(base_url, line)
                    encoded_url = urllib.parse.quote(absolute_url, safe='')
                    
                    if ".m3u8" in absolute_url:
                        # Nested playlist, recursively point to proxy
                        rewritten_lines.append(f"/proxy/m3u8?url={encoded_url}")
                    else:
                        # Video segment
                        rewritten_lines.append(f"/proxy/segment?url={encoded_url}")
                else:
                    rewritten_lines.append(line)
            
            logger.info("M3U8 rewritten: %d lines", len(rewritten_lines))
            return "\n".join(rewritten_lines)
            
        except Exception as e:
            logger.error("Proxy M3U8 error: %s", e, exc_info=True)
            return ""


async def proxy_segment(url: str, headers: dict):
    """
    Streams a video segment from the provider to Jellyfin.
    """
    async def stream_generator():
        async with httpx.AsyncClient(timeout=15.0) as client:
            proxy_headers = _build_proxy_headers(headers)
            
            try:
                async with client.stream('GET', url, headers=proxy_headers) as response:
                    if response.status_code != 200:
                        logger.error("Segment fetch failed: %d for %s", response.status_code, url[:100])
                        return
                    async for chunk in response.aiter_bytes():
                        yield chunk
            except Exception as e:
                logger.error("Segment stream error: %s", e)

    return StreamingResponse(stream_generator(), media_type="video/MP2T")
