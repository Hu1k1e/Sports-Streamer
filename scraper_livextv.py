import logging
import httpx
import time
from scraper import scrape_embed_urls

logger = logging.getLogger("scraper_livextv")

# The original hardcoded premium channels the user explicitly wants to keep
HARDCODED_CHANNELS = [
    {"id": "fox4k-usa", "title": "Fox 4K", "embedUrl": "https://ritzembeds.pages.dev/play/fox4k-usa", "logo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/fox-us.png"},
    {"id": "bbcone-uk", "title": "BBC One", "embedUrl": "https://ritzembeds.pages.dev/play/bbcone-uk", "logo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-kingdom/bbc-one-uk.png"},
    {"id": "fox-usa", "title": "Fox (Hardcoded)", "embedUrl": "https://ritzembeds.pages.dev/play/fox-usa", "logo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/fox-us.png"},
    {"id": "tsn1-ca", "title": "TSN 1", "embedUrl": "https://ritzembeds.pages.dev/play/tsn1-ca", "logo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/canada/tsn-1-ca.png"},
]

# Overrides for API channels that have broken or missing logos in the source API
API_LOGO_OVERRIDES = {
    "bbc-america": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/bbc-america-us.png",
    "fox-sports-1": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/fox-sports-1-us.png",
    "fox-sports-2": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/fox-sports-2-us.png",
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
            "sources": [{"source": channel["title"], "id": f"hc-{channel['id']}Ctx"}], # Differentiate ID context
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
    Scrapes the embed URL to extract the m3u8 playlist.
    Handles both 'livextv-hc-' (hardcoded) and 'livextv-api-' (dynamic API) requests.
    """
    embed_url = None
    source_name = "LiveXTV Stream"
    
    if channel_id.startswith("livextv-hc-"):
        # Look up in hardcoded list
        hc_id = channel_id.replace("livextv-hc-", "")
        channel = next((c for c in HARDCODED_CHANNELS if c["id"] == hc_id), None)
        if channel:
            embed_url = channel["embedUrl"]
            source_name = channel["title"]
            
    elif channel_id.startswith("livextv-api-"):
        # Look up in API list
        api_id = channel_id.replace("livextv-api-", "")
        api_channels = await fetch_daddylive_api()
        channel = next((c for c in api_channels if c.get("url") == api_id), None)
        
        if channel and channel.get("streams"):
            # Usually the first stream object contains the ritzembeds URL
            embed_url = channel["streams"][0].get("url")
            source_name = channel.get("name", "API Stream")

    if not embed_url:
        logger.error("LiveXTV channel %s not found or has no embed URL", channel_id)
        return []

    logger.info("Fetching stream for LiveXTV channel: %s", source_name)
    
    # Format exactly like _get_embed_urls returns
    embed_urls = [{"url": embed_url, "source": source_name}]
    return await scrape_embed_urls(embed_urls, max_streams)
