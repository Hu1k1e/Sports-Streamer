import os

# Base configuration
STREAMED_PK_URL = os.getenv("STREAMED_PK_URL", "https://streamed.pk")

# Proxy settings
PROXY_HOST = os.getenv("PROXY_HOST", "http://127.0.0.1:7694")

# Stream cache TTL in seconds — prevents repeated Playwright scrapes
# when Jellyfin retries GET requests rapidly
STREAM_CACHE_TTL = int(os.getenv("STREAM_CACHE_TTL", "900"))

# Default programme block length (hours) for EPG entries where
# we don't know the exact end time
EPG_DEFAULT_DURATION_HOURS = int(os.getenv("EPG_DEFAULT_DURATION_HOURS", "4"))

# Enable verbose diagnostic logging for scraper and proxy
DEBUG_LOGGING = os.getenv("DEBUG_LOGGING", "true").lower() in ("true", "1", "yes")

# HTTP Client configurations
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
