# Sports Streamer (IPTV Proxy)

A Dockerized middleware application that scrapes streamed.pk for active live streams, parses them, and provides a dynamic M3U playlist and proxy stream for Jellyfin's Live TV feature.

## Features
- **Dynamic Scraper**: Uses Playwright to navigate the site, extract live event metadata, and bypass JavaScript obfuscation.
- **Proxy Engine**: Spoofs HTTP headers (like Referer and User-Agent) on behalf of Jellyfin to ensure smooth playback of `.m3u8` manifests and `.ts` transport stream segments.
- **Docker Ready**: Pre-configured for deployment via Docker Compose or Portainer.

## Deployment via Portainer

1. Open your Portainer dashboard and go to **Stacks** -> **Add stack**.
2. Name the stack (e.g., `sports-streamer`).
3. Select **Web editor** as the build method.
4. Copy and paste the contents of `docker-compose.yml` into the editor.
5. Crucially, set the **Environment Variables**:
   - `PROXY_HOST`: Set this to `http://<YOUR_DOCKER_HOST_IP>:7694` (e.g., `http://192.168.1.50:7694`). If left as `127.0.0.1`, Jellyfin will not be able to connect.
6. Click **Deploy the stack**.

## Jellyfin Setup

1. In your Jellyfin Admin Dashboard, navigate to **Live TV** in the sidebar.
2. Under **Tuner Devices**, click the **+** icon to add a new tuner.
3. Select **M3U Tuner**.
4. In the **File or URL** field, enter the URL of your local proxy's playlist endpoint:
   `http://<YOUR_DOCKER_HOST_IP>:7694/playlist.m3u`
5. Save and let Jellyfin refresh the channels. 

> **Note**: Because this acts as a proxy, the container will download the video streams and relay them to Jellyfin. Ensure your host machine has adequate bandwidth. Playwright might take 5-15 seconds to initially spin up a headless browser and extract the stream when you click "Play" in Jellyfin.
