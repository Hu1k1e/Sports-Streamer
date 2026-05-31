import os

# Base configuration
STREAMED_PK_URL = os.getenv("STREAMED_PK_URL", "https://streamed.pk")

# Scraper configuration
# Note: These selectors might need updates if the site structure changes.
SELECTORS = {
    "EVENT_LINK": "a.event-link", # Example selector, needs actual site analysis
    "IFRAME_EMBED": "iframe[src*='embed']",
    "VIDEO_PLAYER": "video"
}

# Proxy settings
PROXY_HOST = os.getenv("PROXY_HOST", "http://127.0.0.1:5000")

# HTTP Client configurations
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
