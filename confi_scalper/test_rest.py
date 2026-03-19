import asyncio, urllib.request, json, time

url = 'https://api.mexc.com/api/v3/depth?symbol=BTCUSDT&limit=5'

async def test():
    loop = asyncio.get_event_loop()
    for i in range(5):
        print(f'запрос {i+1}...')
        t0 = time.time()
        def fetch():
            with urllib.request.urlopen(url, timeout=8) as r:
                return json.loads(r.read())
        data = await loop.run_in_executor(None, fetch)
        print(f'  OK за {time.time()-t0:.2f}с, bids: {len(data["bids"])}')
        await asyncio.sleep(2)

asyncio.run(test())
