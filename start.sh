#!/bin/bash
pip install websockets aiohttp
python confi_scalper/demo.py --symbol "GOLD(XAUT)USDT" --balance 1000 --sl 0.25 --tp 0.5 --confidence 70 --max-losses 20
