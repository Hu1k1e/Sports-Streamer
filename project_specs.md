# Project Specifications: Streamed.pk IPTV Proxy

## Overview
A Dockerized middleware application (Portainer compatible) that scrapes `streamed.pk` for active live streams, parses them, and provides a dynamic M3U playlist and proxy stream for Jellyfin's Live TV feature.

## Architecture
- **Backend Framework**: Python with FastAPI (lightweight, asynchronous, fast).
- **Scraping Engine**: Playwright (headless browser) with Stealth plugins to bypass basic Cloudflare/bot protections and evaluate JavaScript to find dynamic iframe/m3u8 sources.
- **Proxy Mechanism**: The app acts as an intermediary. It fetches the m3u8 playlist and segments on behalf of Jellyfin, attaching required `Referer` and `User-Agent` headers.
- **Containerization**: Docker & docker-compose for easy Portainer deployment.

## Potential Bugs & Mitigations
1. **Cloudflare/Bot Protection**: 
   - *Fix*: Use Playwright with stealth configurations or flaresolverr integration if necessary.
2. **Expiring Tokens & Dynamic Links**: 
   - *Fix*: M3U links provided to Jellyfin will point to our local API (e.g., `/stream/{match_id}`). The API will scrape the fresh link in real-time when Jellyfin requests the stream.
3. **Header Validation (CORS/Referer)**: 
   - *Fix*: The internal proxy endpoint will append the required headers when downloading the `.m3u8` files and `.ts` segments, serving standard video data back to Jellyfin.
4. **DOM/Site Structure Changes**:
   - *Fix*: Keep CSS selectors and URL patterns in a centralized configuration file (`config.py` or `.env`) so they can be easily updated without digging through code.

## Next Implementation
1. Deploy updated container and verify streams play in Jellyfin.
2. Monitor logs for m3u8 capture success/failure rates.
3. If Cloudflare blocks persist, integrate FlareSolverr.

## Implementation History
- **[2026-05-31]**: Initial project specifications and instructions created.
- **[2026-05-31]**: Added `HEAD` request support for Jellyfin and integrated `playwright-stealth` to bypass bot protection and added click-to-play simulations.
- **[2026-06-03]**: Fixed stream 404 bug — replaced `page.route()` interception with passive `page.on("request")` listener that was blocking page load. Added 4-phase m3u8 capture strategy with JS fallback. Added TTL-based stream cache to prevent repeated Playwright scrapes on Jellyfin retries. Removed `urllib.parse.quote()` double-encoding from M3U URLs. Added comprehensive diagnostic logging.
- **[2026-06-03]**: Fixed ghost streams bug by filtering out concluded events (streams that started more than 6 hours ago) in scraper and adjusting EPG timing to accurately reflect stream duration without artificial extensions.
- **[2026-06-03]**: **Major refactor** — Full streamed.pk API integration. M3U playlist now uses `/api/matches/live` endpoint (only currently broadcasting streams from all sports). EPG uses `/api/matches/all` cross-referenced with live status. Added `/api/sports` for display names. Added 15-minute API response cache. Proper image URLs via `/api/images/badge/` and `/api/images/proxy/`. Updated source preference list to all documented sources. HD+English stream preference. Removed unused SELECTORS config. Cleaned up config.py.
- **[2026-06-04]**: Fixed caching logic to enforce maximum sizes and proactive eviction.
- **[2026-06-04]**: Shifted EPG to use `/api/matches/all-today` for better relevance. Updated stream scraper to concurrently fetch all sources and select the one with the highest viewer count (with HD+English tiebreakers). Removed the manual age filter entirely, relying solely on the API's `all-today` endpoint to provide accurate daily schedules without hardcoded time limits.

## GitHub / Version Control Instructions
To push this project to GitHub:
```bash
git init
git add .
git commit -m "Initial commit: Setup IPTV Proxy architecture"
git branch -M main
git remote add origin <YOUR_GITHUB_REPO_URL>
git push -u origin main
```

## Deployment Instructions (Docker / Portainer)
1. Build the image and start the container using Docker Compose.
2. In Portainer, create a new "Stack" and paste the contents of `docker-compose.yml`.
3. Set any required environment variables (e.g., PORT, HOST).
4. Deploy the stack.

Example `docker-compose.yml` (To be implemented):
```yaml
version: '3.8'
services:
  streamed-proxy:
    build: .
    ports:
      - "7694:5000"
    restart: unless-stopped
```

- **[2026-06-04]**: Updated stream fetching logic. Renamed _get_embed_url to _get_embed_urls to return all available stream embed URLs sorted by priority instead of just the top one. Prioritized 'admin' streams above others. Added failover mechanism in Playwright where it will automatically try the next embed URL if the first one fails to load an .m3u8 playlist, ensuring streams always load.


- **[2026-06-04]**: Fixed 403 Forbidden errors during stream playback. (1) Extracted cookies from the Playwright context and appended them to the proxy headers, as some streams use session cookies for playlist access. (2) Updated M3U8 proxy rewrite logic to properly parse and rewrite \#EXT-X-KEY\ encryption URIs, ensuring that AES keys are fetched through our proxy rather than requested directly by Jellyfin (which caused CORS/Referer blocks). Renamed segment proxy route to handle generic media (both \.ts\ and \.key\).


- **[2026-06-04]**: Fixed strict player compatibility (e.g. Dispatcharr, Jellyfin) where segments were returning HTTP 200 OK but dropping the stream due to an incorrect generic \pplication/octet-stream\ Content-Type. Reverted \.ts\ segment media type back to \ideo/MP2T\ while keeping \.key\ as \pplication/octet-stream\ dynamically based on the file extension in the proxy route.


- **[2026-06-04]**: Fixed player hang issues (stuck on 'Loading stream...') in strict players like Dispatcharr. Resolved by manually extracting the \Content-Length\ and \Content-Type\ headers from the upstream CDN's \.ts\ segment responses before streaming the body, and passing those headers explicitly into the FastAPI \StreamingResponse\. This prevents chunked transfer encoding issues where strict players refuse to buffer without knowing the exact file size.


- **[2026-06-04]**: Fixed streaming failures on strict players caused by TikTok CDN disguising \.ts\ video segments as \image/png\ files. Removed upstream \Content-Type\ inheritance in the proxy to ensure \ideo/MP2T\ is strictly sent to the player, allowing demuxers to properly parse the segments.


- **[2026-06-04]**: Completely resolved upstream anti-piracy blocking (steganography) where CDNs actively wrap \.ts\ streams within literal 70-byte \image/png\ files. Updated the proxy to parse the first chunk of every media segment, detect PNG magic headers (\\x89PNG\), find the \IEND\ chunk boundary, slice off the wrapper, dynamically subtract the wrapper size from the \Content-Length\, and stream pure unadulterated MPEG-TS payload to the video player. This prevents decoders from rejecting streams instantly.


- **[2026-06-04]**: Updated stream scraping logic to natively test and profile sources automatically via byte-level detection in the proxy (e.g. dynamically bypassing PNG wrappers on \strmd.top\ networks). Adjusted stream priority fallback logic to strictly use \iewers\ (descending) after the \dmin\ source.

- **[2026-06-05]**: Fixed fallback logic failure where dead streams returning HTTP 500 or 404 were being cached as successful. Updated `scraper.py` to intercept `on_response` and strictly require `response.status == 200` before accepting an M3U8 URL.
- **[2026-06-05]**: Fixed `403 Forbidden` errors for alternative streams (like `golf`) by removing hardcoded `embedsports.top` headers in `proxy.py`. The proxy now correctly preserves and forwards the original `Referer` and `Origin` captured by Playwright during the scrape.

- **[2026-06-06]**: Added native support for sportsurge.ws. Created scraper_sportsurge.py to scrape live sports links natively without Playwright. The scraper fetches the event page, extracts the hidden base64 m3u8 playlist URL from the clappr iframe config, decodes it, and proxies the HLS stream identically to streamed.pk. Added independent endpoints for Sportsurge: /sportsurge.m3u and /sportsurge.xml.
- **[2026-06-07]**: Added native support for crichd.is to provide 24/7 dedicated cricket streams. Created scraper_crichd.py to automatically parse homepage events, extract dynamic iframe embeds (1freecdn), and decrypt the stream `pk` token by slicing obfuscated characters. Integrated new M3U and XML endpoints (`/crichd.m3u`, `/crichd.xml`) which automatically hijack match posters from the streamed.pk API using Jaccard string similarity.
- **[2026-06-16]**: Fixed a severe logic bug in `_sync_poster` that mapped incorrect event posters (mismatched images) to live streams due to a highly permissive fuzzy matching algorithm (0.5 threshold). Increased the threshold to 0.85, added strict subset boundary checks, removed leading list numbers from alternative source titles, and heavily prioritized currently live events (`is_live=True`) to prevent expired games from improperly overriding new streams.
- **[2026-07-03]**: Fixed critical multi-user crash in Dispatcharr when 2+ users stream simultaneously. (1) Eliminated `stream_headers_cache["latest"]` global race condition — proxy URLs now carry a `?sid=<stream_id>` query parameter so each stream's m3u8 sub-playlists and `.ts` segments use the correct per-stream Referer/Cookie/Origin headers instead of a globally overwritten "latest" key that caused cross-stream header contamination and 403s. (2) Replaced global `asyncio.Lock()` in `scraper.py` with `asyncio.Semaphore(3)` to allow up to 3 concurrent Playwright scrapes, preventing cascading timeouts when multiple users trigger cache misses.
- **[2026-07-04]**: Fixed FFmpeg `End of file` stream drops in Dispatcharr caused by the proxy's async streaming pipeline. (1) Replaced `StreamingResponse` async generator in `proxy_media()` with full-buffered `Response` that includes an explicit `Content-Length` header — FFmpeg's aggressive HTTP Keep-Alive socket reuse was causing EOF when the Python async generator couldn't keep up. (2) Added a shared `curl_cffi` `AsyncSession` (`_get_session()`) for connection pooling instead of creating a new session per request. (3) Fully removed the residual `stream_headers_cache["latest"]` fallback — proxy routes now return 502 if the per-stream `sid` lookup fails, preventing silent cross-stream header contamination.
- **[2026-07-05]**: Investigated stream crash after ~73 seconds of stable playback (Mexico vs England). **Confirmed NOT our app** — all proxy requests returned HTTP 200, M3U8 playlists and `.ts` segments were served without errors. Root cause is Dispatcharr's `live_proxy.server` triggering a `client_disconnect` event and tearing down the stream manager (`Stopped stream manager`, `Removed stream buffer`, `Removed client manager`) after the stream was stable for 72.8 seconds. Additional Dispatcharr logs show unrelated internal issues: Celery `check_plugin_health` task unregistered, plugin loader failing to deserialize options (expects dicts, got strings). Fix needs to be applied in Dispatcharr's live_proxy configuration (stream timeout, client keepalive, buffer settings).
- **[2026-07-05]**: Implemented Playwright speed optimizations and proactive pre-warming to prevent initial playback timeouts on strict players. (1) Added `page.route` interception in `scraper.py` to block image, font, and stylesheet assets, reducing Playwright scrape time from ~15s to ~3-5s. Changed `page.goto` wait condition to `commit` for faster execution. (2) Added a background asyncio task in `main.py` that automatically triggers every 90 seconds to pre-warm (scrape and cache) the top 10 most popular live streams, ensuring instant Cache HITs for high-profile events.
- **[2026-07-06]**: Fixed three Jellyfin Live TV issues when using direct M3U/EPG without Dispatcharr. (1) Improved image fallback chain in `_parse_events()` to try poster → home badge → away badge, ensuring 100% image coverage (previously events without a poster only tried the home badge and ignored the away badge entirely). (2) Sorted M3U and EPG channels by sport category then name so Jellyfin groups related events together (Basketball, Baseball, Football, etc.) instead of random API order. (3) Fixed "On Now" section showing duplicate images by ensuring each EPG `<programme>` entry uses its own unique `logo_url` via the improved fallback chain.
- **[2026-07-08]**: iOS playback fix, channel ordering, and stream robustness improvements. (1) **iOS fix:** Added `#EXT-X-VERSION:3` to all rewritten HLS playlists (required by iOS AVPlayer), removed `Connection: close` from all proxy responses to allow HTTP keep-alive (iOS needs TCP socket reuse for fast segment loading), added explicit CORS headers (`Access-Control-Allow-Origin: *`) to all proxy responses. (2) **Channel numbering:** Added `tvg-chno` attribute to M3U entries. Football (soccer) channels are numbered starting at 1, followed by all other sports sequentially grouped by category. Custom sort ensures football always appears first regardless of Jellyfin sort mode. (3) **Stream robustness:** Implemented curl_cffi session recycling every 30 minutes to prevent stale CDN connections, switched retry logic from flat 1s delays to exponential backoff (0.5s → 1s → 2s), increased M3U8 fetch timeout from 15s to 20s, added `Accept: application/vnd.apple.mpegurl` header to M3U8 requests for CDN compatibility. (4) **Cleanup:** Removed one-time `-v2` channel ID cache-buster suffix. Deduplicated API events by title in `_parse_events()` to prevent duplicate channels.
- **[2026-07-09]**: Fixed iOS playback failure and stream dropout issues. (1) **iOS fix:** Removed hardcoded `CODECS="avc1.640028,mp4a.40.2"` injection from `rewrite_m3u8()` — this declared H.264 High Profile but many upstream sources use Main/Baseline profiles, causing iOS AVPlayer to reject the stream on codec mismatch. Without declared codecs, AVPlayer probes actual content. (2) **Stream dropout fix:** Made curl_cffi session recycling activity-aware — sessions are only recycled after 30 min of age AND 5 min of idle time. Previously, session recycling mid-stream destroyed the TLS fingerprint/cookies, causing CDN 403s and immediate stream drops. (3) **CORS fix:** Changed `allow_credentials=True` to `False` in CORSMiddleware (credentials+wildcard is invalid per CORS spec, silently dropping headers). Added explicit `Cache-Control: no-cache` and CORS headers to all m3u8 responses for iOS AVPlayer live-stream refetching.
