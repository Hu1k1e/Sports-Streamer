import asyncio
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
from config import STREAMED_PK_URL, DEFAULT_HEADERS

async def get_events():
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"])
        page = await context.new_page()
        try:
            await page.goto(STREAMED_PK_URL, wait_until="domcontentloaded", timeout=15000)
            content = await page.content()
            soup = BeautifulSoup(content, 'html.parser')
            
            events = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                if href.startswith('/watch/'):
                    h1 = a.find('h1')
                    if h1:
                        title = h1.get('title') or h1.get_text(strip=True)
                        path = href.lstrip('/')
                        if title and title not in [e['name'] for e in events]:
                            events.append({"name": title, "path": path})
            return events
        except Exception as e:
            print(f"Error fetching events: {e}")
            return []
        finally:
            await browser.close()

async def get_stream_url(event_path):
    url = f"{STREAMED_PK_URL}/{event_path}"
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"])
        page = await context.new_page()
        
        m3u8_url = None
        m3u8_headers = {}

        async def route_handler(route):
            nonlocal m3u8_url, m3u8_headers
            request = route.request
            if ".m3u8" in request.url:
                m3u8_url = request.url
                m3u8_headers = request.headers
            await route.continue_()

        await page.route("**/*", route_handler)
        
        try:
            # Using domcontentloaded because streaming sites have ad trackers that keep network busy
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            
            # Wait to see if m3u8 was caught
            await page.wait_for_timeout(5000)
            
            if not m3u8_url:
                try:
                    await page.click("video", timeout=1000)
                    await page.wait_for_timeout(2000)
                except:
                    pass

            if not m3u8_url:
                # Click to bypass popups and trigger play
                for _ in range(3):
                    for frame in page.frames:
                        try:
                            await frame.click("body", timeout=1000)
                            await page.wait_for_timeout(1500)
                            if m3u8_url:
                                break
                        except:
                            pass
                    if m3u8_url:
                        break

            return {"url": m3u8_url, "headers": m3u8_headers}
        except Exception as e:
            print(f"Error fetching stream URL: {e}")
            return None
        finally:
            await browser.close()
