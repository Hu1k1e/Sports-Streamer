import httpx
from fastapi.responses import StreamingResponse
import urllib.parse

async def proxy_m3u8(url: str, headers: dict):
    """
    Fetches the M3U8 playlist and rewrites URLs so Jellyfin requests
    segments through our proxy, allowing us to attach necessary headers.
    """
    async with httpx.AsyncClient() as client:
        try:
            proxy_headers = {
                "User-Agent": headers.get("user-agent", ""),
                "Referer": headers.get("referer", ""),
                "Origin": headers.get("origin", "")
            }
            proxy_headers = {k: v for k, v in proxy_headers.items() if v}
            
            response = await client.get(url, headers=proxy_headers)
            content = response.text
            base_url = str(response.url)
            
            lines = content.split('\n')
            rewritten_lines = []
            
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    # Segment or nested playlist URL
                    absolute_url = urllib.parse.urljoin(base_url, line)
                    encoded_url = urllib.parse.quote(absolute_url)
                    
                    if ".m3u8" in absolute_url:
                        # Nested playlist, recursively point to proxy
                        rewritten_lines.append(f"/proxy/m3u8?url={encoded_url}")
                    else:
                        # Video segment
                        rewritten_lines.append(f"/proxy/segment?url={encoded_url}")
                else:
                    rewritten_lines.append(line)
            
            return "\n".join(rewritten_lines)
            
        except Exception as e:
            print(f"Proxy M3U8 error: {e}")
            return ""

async def proxy_segment(url: str, headers: dict):
    """
    Streams a video segment from the provider to Jellyfin.
    """
    async def stream_generator():
        async with httpx.AsyncClient() as client:
            proxy_headers = {
                "User-Agent": headers.get("user-agent", ""),
                "Referer": headers.get("referer", "")
            }
            proxy_headers = {k: v for k, v in proxy_headers.items() if v}
            
            try:
                async with client.stream('GET', url, headers=proxy_headers) as response:
                    async for chunk in response.aiter_bytes():
                        yield chunk
            except Exception as e:
                print(f"Segment stream error: {e}")

    return StreamingResponse(stream_generator(), media_type="video/MP2T")
