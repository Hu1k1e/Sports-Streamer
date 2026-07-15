import logging
from scraper import scrape_embed_urls

logger = logging.getLogger("scraper_livextv")

LIVEXTV_CHANNELS = [
    {"id": "fox4k-usa", "title": "Fox 4K", "embedUrl": "https://ritzembeds.pages.dev/play/fox4k-usa", "category": "24/7 Channels"},
    {"id": "bbcone-uk", "title": "BBC One", "embedUrl": "https://ritzembeds.pages.dev/play/bbcone-uk", "category": "24/7 Channels"},
    {"id": "beinsportsmax-sa", "title": "BeIN Sports Max", "embedUrl": "https://ritzembeds.pages.dev/play/beinsportsmax-sa", "category": "24/7 Channels"},
    {"id": "fox-usa", "title": "Fox", "embedUrl": "https://ritzembeds.pages.dev/play/fox-usa", "category": "24/7 Channels"},
    {"id": "telemundo-usa", "title": "Telemundo", "embedUrl": "https://ritzembeds.pages.dev/play/telemundo-usa", "category": "24/7 Channels"},
    {"id": "fussballtv1uhd-de", "title": "Fussball TV 1 UHD", "embedUrl": "https://ritzembeds.pages.dev/play/fussballtv1uhd-de", "category": "24/7 Channels"},
    {"id": "daznmundial-es", "title": "DAZN Mundial", "embedUrl": "https://ritzembeds.pages.dev/play/daznmundial-es", "category": "24/7 Channels"},
    {"id": "tsn1-ca", "title": "TSN 1", "embedUrl": "https://ritzembeds.pages.dev/play/tsn1-ca", "category": "24/7 Channels"},
    {"id": "cazetv-br", "title": "CazeTV", "embedUrl": "https://ritzembeds.pages.dev/play/cazetv-br", "category": "24/7 Channels"},
    {"id": "dsports-ar", "title": "DSports", "embedUrl": "https://ritzembeds.pages.dev/play/dsports-ar", "category": "24/7 Channels"},
]

def get_livextv_events():
    """
    Returns the LiveXTV 24/7 channels in a format compatible with the M3U generator.
    """
    events = []
    for channel in LIVEXTV_CHANNELS:
        events.append({
            "id": f"livextv-{channel['id']}",
            "name": channel["title"],
            "category": channel["category"],
            "date": 0,  # 24/7 channels
            "poster": "",
            "logo_url": "",  # Can add a generic LiveXTV logo here if needed
            "home_badge_url": "",
            "away_badge_url": "",
            "sources": [{"source": channel["title"], "id": channel["id"]}],
            "is_live": True,
            "teams": None,
        })
    return events


async def get_livextv_stream(channel_id: str, max_streams: int = 1):
    """
    Scrapes the LiveXTV embed URL to extract the m3u8 playlist.
    """
    # Strip the 'livextv-' prefix if it was passed in
    if channel_id.startswith("livextv-"):
        channel_id = channel_id.replace("livextv-", "")
        
    channel = next((c for c in LIVEXTV_CHANNELS if c["id"] == channel_id), None)
    if not channel:
        logger.error("LiveXTV channel %s not found", channel_id)
        return []

    logger.info("Fetching stream for LiveXTV channel: %s", channel["title"])
    
    # Format exactly like _get_embed_urls returns
    embed_urls = [{"url": channel["embedUrl"], "source": channel["title"]}]
    return await scrape_embed_urls(embed_urls, max_streams)
