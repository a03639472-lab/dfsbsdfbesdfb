import urllib.request, json, time

url = 'https://api.mexc.com/api/v3/depth?symbol=BTCUSDT&limit=5'

for i in range(10):
    try:
        req = urllib.request.Request(url, headers={'Connection': 'close'})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
            print(f'{i+1}: OK bids={len(d["bids"])}', flush=True)
    except Exception as e:
        print(f'{i+1}: ERROR {e}', flush=True)
    time.sleep(2)
