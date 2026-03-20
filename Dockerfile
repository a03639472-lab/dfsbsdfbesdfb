FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install websockets aiohttp
CMD ["python", "confi_scalper/demo.py", "--symbol", "GOLD(XAUT)USDT", "--balance", "1000", "--sl", "0.25", "--tp", "0.5", "--confidence", "70", "--max-losses", "20"]
