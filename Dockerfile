FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install websockets aiohttp
CMD ["python", "confi_scalper/demo.py", "--symbol", "GOLD(XAUT)USDT", "--balance", "1000", "--sl", "0.08", "--tp", "0.16", "--confidence", "70", "--max-losses", "20"]
