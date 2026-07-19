import logging
import httpx
import re
import time
import urllib.parse
from scraper import scrape_embed_urls
from config import DEFAULT_HEADERS

logger = logging.getLogger("scraper_livextv")

# The original hardcoded premium channels the user explicitly wants to keep.
# embedUrl is a FALLBACK — we always check the DaddyLive API first for the latest URL.
HARDCODED_CHANNELS = [
    {"id": "fox4k-usa", "title": "Fox 4K", "embedUrl": "https://logic.icelanders.st/embed/fox4k-usa", "logo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/fox-us.png"},
    {"id": "bbcone-uk", "title": "BBC One", "embedUrl": "https://logic.icelanders.st/embed/bbcone-uk", "logo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-kingdom/bbc-one-uk.png"},
    {"id": "fox-usa", "title": "Fox (Hardcoded)", "embedUrl": "https://logic.icelanders.st/embed/fox-usa", "logo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/fox-us.png"},
    {"id": "tsn1-ca", "title": "TSN 1", "embedUrl": "https://logic.icelanders.st/embed/tsn1-ca", "logo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/canada/tsn-1-ca.png"},
]

# Maps hardcoded channel IDs to DaddyLive API slugs so we can auto-refresh URLs
# when the embed domain changes. If the API has the channel, its URL is always preferred.
HC_TO_API_SLUG = {
    "bbcone-uk": "bbc-one-london",
    "fox-usa": "fox",
    "tsn1-ca": None,      # not in API
    "fox4k-usa": None,    # not in API — 4K exclusive
}

# Overrides for API channels that have broken or missing logos in the source API
API_LOGO_OVERRIDES = {
    "bbc-america": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/bbc-america-us.png",
    "fox-sports-1": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/fox-sports-1-us.png",
    "fox-sports-2": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/fox-sports-2-us.png",
    "espnews": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/espnews-us.png",
}

# The whitelist of channel IDs from the JSON API the user explicitly requested
API_WHITELIST = {
    "abc", "bbc-america", "bbc-one-london", "bbc-two", "cbs", "cbs-sports-network",
    "espn", "espn2", "disney-xd", "espnews", "fox", "fox-sports-1", "fox-sports-2",
    "fox-sports-501-cricket", "fox-sports-502-league", "fox-sports-503", "fox-sports-504-footy",
    "fox-sports-505", "fox-sports-506", "fox-sports-507",
    "hbo", "hbo-comedy", "mlb-network", "nba-tv", "nbc", "nbc-sports-bay-area",
    "nbc-sports-philadelphia", "nfl-network", "nickelodeon"
}

_cached_api_events = []
_api_cache_time = 0
API_CACHE_TTL = 1800  # 30 minutes


async def fetch_daddylive_api():
    """Fetches the DaddyLive channels JSON API with caching."""
    global _cached_api_events, _api_cache_time
    now = time.time()
    
    if _cached_api_events and (now - _api_cache_time) < API_CACHE_TTL:
        return _cached_api_events
        
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.vixnuvew.uk/api/channels")
            if resp.status_code == 200:
                _cached_api_events = resp.json().get("channels", [])
                _api_cache_time = now
                logger.info("Successfully fetched %d channels from DaddyLive API", len(_cached_api_events))
            else:
                logger.error("DaddyLive API returned status %d", resp.status_code)
    except Exception as e:
        logger.error("Failed to fetch DaddyLive API: %s", e)
        
    return _cached_api_events


# ---------------------------------------------------------------------------
# Direct m3u8 extraction (no Playwright)
# ---------------------------------------------------------------------------
# The icelanders.st embed pages use XOR-obfuscated JavaScript that contains
# a pre-signed m3u8 URL. We can extract it with a simple HTTP GET + decode.
# This is 100x faster than Playwright and immune to anti-bot detection.
# ---------------------------------------------------------------------------

async def _extract_m3u8_direct(embed_url: str, source_name: str = "direct") -> dict | None:
    """
    Fetch the embed page HTML, decode the XOR-obfuscated JavaScript,
    and extract the pre-signed m3u8 URL.
    
    Returns a stream dict compatible with scrape_embed_urls output, or None.
    Works with any domain serving this embed format (icelanders.st, ritzembeds, etc.)
    """
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(embed_url, headers={
                "User-Agent": DEFAULT_HEADERS["User-Agent"]
            })
            if resp.status_code != 200:
                logger.error("Embed page %s returned HTTP %d", embed_url, resp.status_code)
                return None
            
            html = resp.text
            
            # Find the XOR-obfuscated script block.
            # Pattern: var _xxx=[num,num,...],_yyy=key1,_zzz=key2
            # The variable names change but the structure is always the same.
            match = re.search(r'var\s+\w+=\[([\d,]+)\],\s*\w+=(\d+),\s*\w+=(\d+)', html)
            if not match:
                logger.error("No XOR-obfuscated script found in %s", embed_url)
                return None
            
            arr = list(map(int, match.group(1).split(',')))
            xor_key = int(match.group(2))
            sub_key = int(match.group(3))
            
            # Decode: ((charCode ^ xorKey) - subKey + 256) % 256
            decoded = ""
            for code in arr:
                decoded += chr(((code ^ xor_key) - sub_key + 256) % 256)
            
            # Extract the pre-signed m3u8 URL from the decoded JavaScript
            url_match = re.search(r'(https?://[^\s"\'\\]+\.m3u8)', decoded)
            if not url_match:
                logger.error("No m3u8 URL found in decoded script from %s", embed_url)
                return None
            
            m3u8_url = url_match.group(1)
            logger.info("Direct-extracted m3u8 from %s: %s", embed_url, m3u8_url[:120])
            
            # Build headers. The CDN currently doesn't require any special headers,
            # but we set Origin/Referer for safety in case they add checks later.
            parsed = urllib.parse.urlparse(embed_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            
            headers = {
                "accept": "*/*",
                "origin": origin,
                "referer": f"{origin}/",
                "user-agent": DEFAULT_HEADERS["User-Agent"],
            }
            
            return {
                "url": m3u8_url,
                "headers": headers,
                "source": source_name,
                "embed_url": embed_url,
            }
    except Exception as e:
        logger.error("Direct m3u8 extraction failed for %s: %s", embed_url, e)
        return None


async def get_livextv_events():
    """
    Returns the LiveXTV / DaddyLive 24/7 channels in a format compatible with the M3U generator.
    Merges the hardcoded 4K channels with the whitelisted API channels.
    """
    events = []
    
    # 1. Add the hardcoded channels first
    for channel in HARDCODED_CHANNELS:
        events.append({
            "id": f"livextv-hc-{channel['id']}",
            "name": channel["title"],
            "category": "24/7 Channels",
            "date": 0,
            "poster": "",
            "logo_url": channel.get("logo", ""), 
            "home_badge_url": "",
            "away_badge_url": "",
            "sources": [{"source": channel["title"], "id": f"hc-{channel['id']}Ctx"}],
            "is_live": True,
            "teams": None,
        })
        
    # 2. Fetch API channels and filter by whitelist
    api_channels = await fetch_daddylive_api()
    for channel in api_channels:
        ch_id = channel.get("url", "")
        if ch_id in API_WHITELIST:
            events.append({
                "id": f"livextv-api-{ch_id}",
                "name": channel.get("name", ch_id),
                "category": "24/7 Channels",
                "date": 0,
                "poster": "",
                "logo_url": API_LOGO_OVERRIDES.get(ch_id) or channel.get("logo", ""),
                "home_badge_url": "",
                "away_badge_url": "",
                "sources": [{"source": channel.get("name", ""), "id": ch_id}],
                "is_live": True,
                "teams": None,
            })
            
    return events


async def get_livextv_stream(channel_id: str, max_streams: int = 1):
    """
    Resolves a LiveXTV/DaddyLive channel to a playable m3u8 stream.
    
    Strategy:
    1. Resolve the embed URL (from API first, hardcoded fallback)
    2. Try DIRECT extraction (fast HTTP + XOR decode, ~100ms)
    3. Fall back to Playwright if direct extraction fails
    
    This is domain-resilient: if the embed provider changes domains,
    the DaddyLive API will return the new URLs automatically.
    """
    embed_url = None
    source_name = "LiveXTV Stream"
    
    if channel_id.startswith("livextv-hc-"):
        hc_id = channel_id.replace("livextv-hc-", "")
        
        # RESILIENCE: Check DaddyLive API first for the latest URL.
        # If the embed domain changes, the API will reflect it immediately.
        api_slug = HC_TO_API_SLUG.get(hc_id)
        if api_slug:
            api_channels = await fetch_daddylive_api()
            api_channel = next((c for c in api_channels if c.get("url") == api_slug), None)
            if api_channel and api_channel.get("streams"):
                embed_url = api_channel["streams"][0].get("url")
                source_name = api_channel.get("name", "API Refresh")
                logger.info("Auto-refreshed hardcoded channel '%s' URL from API: %s", hc_id, embed_url)
        
        # Fallback to hardcoded URL if API doesn't have this channel
        if not embed_url:
            hc_channel = next((c for c in HARDCODED_CHANNELS if c["id"] == hc_id), None)
            if hc_channel:
                embed_url = hc_channel["embedUrl"]
                source_name = hc_channel["title"]
            
    elif channel_id.startswith("livextv-api-"):
        api_id = channel_id.replace("livextv-api-", "")
        api_channels = await fetch_daddylive_api()
        channel = next((c for c in api_channels if c.get("url") == api_id), None)
        
        if channel and channel.get("streams"):
            embed_url = channel["streams"][0].get("url")
            source_name = channel.get("name", "API Stream")

    if not embed_url:
        logger.error("LiveXTV channel %s not found or has no embed URL", channel_id)
        return []

    logger.info("Fetching stream for LiveXTV channel: %s (embed: %s)", source_name, embed_url)
    
    # --- Strategy 1: Direct HTTP extraction (fast, no Playwright) ---
    result = await _extract_m3u8_direct(embed_url, source_name)
    if result:
        logger.info("Direct extraction SUCCESS for '%s'", channel_id)
        return [result]
    
    # --- Strategy 2: Playwright fallback (slow but robust) ---
    logger.warning("Direct extraction failed for '%s', falling back to Playwright", channel_id)
    embed_urls = [{"url": embed_url, "source": source_name, "referer": "https://livextv.pro/"}]
    return await scrape_embed_urls(embed_urls, max_streams)
