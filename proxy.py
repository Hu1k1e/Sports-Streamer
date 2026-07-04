import logging
import base64
import urllib.parse
import asyncio
from curl_cffi.requests import AsyncSession
from fastapi.responses import StreamingResponse, Response
from playwright.async_api import async_playwright
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
            if 'CODECS=' not in inf:
                inf += ',CODECS="avc1.640028,mp4a.40.2"'
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
                if 'CODECS=' not in line:
                    line = line + ',CODECS="avc1.640028,mp4a.40.2"'
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
        
        return rewrite_m3u8(response.text, str(response.url), proxy_base_url, stream_id=stream_id)
        
    except Exception as e:
        logger.error("Proxy M3U8 error: %s", e, exc_info=True)
        return ""


async def proxy_media(url: str, headers: dict, media_type: str = "video/MP2T"):
    """
    Fetches a video segment or encryption key using curl_cffi (Chrome TLS impersonation)
    and streams it to Jellyfin.
    """
    session = AsyncSession(impersonate="chrome")
    try:
        proxy_headers = _build_proxy_headers(headers)
        # timeout=None allows streaming indefinitely without killing connection midway
        response = await session.get(url, headers=proxy_headers, stream=True, timeout=None)
        
        if response.status_code != 200:
            logger.error("Media fetch failed: %d for %s", response.status_code, url[:100])
            await session.close()
            return Response(content=b"", media_type=media_type, status_code=502)
            
        # Get the async generator for chunks
        content_iter = response.aiter_content()
        
        # Read the first chunk to inspect for PNG steganography
        try:
            first_chunk = await content_iter.__anext__()
        except StopAsyncIteration:
            first_chunk = b""
            
        stripped_bytes = 0
        if first_chunk.startswith(b'\x89PNG\r\n\x1a\n'):
            iend_idx = first_chunk.find(b'IEND')
            if iend_idx != -1:
                # Strip PNG header completely
                png_header_len = iend_idx + 8
                first_chunk = first_chunk[png_header_len:]
                stripped_bytes = png_header_len
        
        forward_headers = {}
        # Do NOT forward Content-Length because ASGI handles Transfer-Encoding: chunked automatically.
        # If we manually set Content-Length and it's slightly off or the stream drops, clients like FFmpeg will fail with EOF.
        async def stream_generator():
            try:
                if first_chunk:
                    yield first_chunk
                async for chunk in content_iter:
                    yield chunk
            finally:
                await session.close()

        return StreamingResponse(
            stream_generator(),
            media_type=media_type,
            headers=forward_headers
        )
    except Exception as e:
        logger.error("Media stream error: %s", e)
        await session.close()
        return Response(content=b"", media_type=media_type, status_code=502)
