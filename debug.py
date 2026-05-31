import asyncio
from scraper import get_stream_url

async def main():
    print("Testing get_stream_url for 'watch/ppv-brazil-vs-panama'")
    result = await get_stream_url("watch/ppv-brazil-vs-panama")
    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())
