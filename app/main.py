import os
import html
import asyncio
import logging
import subprocess
from datetime import datetime
import signal

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

app = FastAPI()

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
COIN_ID = "kinetiq"
VS_CURRENCY = "usd"

SERVICE_NAME = "crypto-kinetiq.service"

API_KEY = os.getenv("COINGECKO_API_KEY")

INFLUX_URL = os.getenv("INFLUX_URL")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")
INFLUX_ORG = os.getenv("INFLUX_ORG")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET")

influx = InfluxDBClient(
    url=INFLUX_URL,
    token=INFLUX_TOKEN,
    org=INFLUX_ORG
)

write_api = influx.write_api(write_options=SYNCHRONOUS)


def get_price_usd():
    headers = {}
    if API_KEY:
        headers["x-cg-demo-api-key"] = API_KEY

    try:
        r = requests.get(
            COINGECKO_URL,
            headers=headers,
            params={"ids": COIN_ID, "vs_currencies": VS_CURRENCY},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        return data.get(COIN_ID, {}).get(VS_CURRENCY)
    except Exception as e:
        logging.error(f"Failed to fetch price: {e}")
        return None


def get_last_logs(n: int = 100):
    try:
        result = subprocess.run(
            ["journalctl", "-u", SERVICE_NAME, "-n", str(n), "--no-pager"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except Exception as e:
        return f"Error reading logs: {e}"


@app.get("/", response_class=HTMLResponse)
def home():
    price = get_price_usd()
    logs = get_last_logs(100)

    price_str = f"${price}" if price is not None else "N/A"

    return f"""
    <html>
      <head>
        <title>Kinetiq Crypto</title>
        <meta charset="utf-8" />
        <meta http-equiv="refresh" content="15" />
        <style>
          body {{
            font-family: Arial, sans-serif;
            background: #0b1220;
            color: #e6e6e6;
            padding: 24px;
          }}
          .card {{
            background: #121a2b;
            border-radius: 16px;
            padding: 18px 22px;
            margin-bottom: 18px;
          }}
          h1 {{ margin: 0 0 10px 0; font-size: 28px; }}
          .price {{ font-size: 22px; font-weight: bold; }}
          .muted {{ color: #9aa7c0; font-size: 13px; }}
          pre {{
            white-space: pre-wrap;
            word-break: break-word;
            background: #0a0f1c;
            padding: 14px;
            border-radius: 12px;
            border: 1px solid rgba(255,255,255,0.06);
            font-size: 13px;
          }}
          a {{ color: #8ab4ff; }}
        </style>
      </head>
      <body>
        <div class="card">
          <h1>Kinetiq monitor</h1>
          <div class="price">Kinetiq price: {price_str}</div>
          <div class="muted">Updated: {datetime.now().isoformat()}</div>
        </div>

        <div class="card">
          <h2 style="margin-top:0;">Heartbeat logs (last 100)</h2>
          <pre>{html.escape(logs)}</pre>
        </div>
      </body>
    </html>
    """


def save_price(price):
    if price is None:
        logging.warning("Price is None, skipping write")
        return

    logging.info(f"Writing to InfluxDB: {price}")

    point = (
        Point("crypto_price")
        .tag("coin", COIN_ID)
        .field("price", float(price))
    )

    try:
        write_api.write(
            bucket=INFLUX_BUCKET,
            record=point
        )
        logging.info("Write successful")
    except Exception as e:
        logging.error(f"Influx write failed: {e}")


@app.get("/kill")
def kill_service():
    logging.warning("Kill endpoint called -> exiting now")
    os.kill(os.getpid(), signal.SIGTERM)
    return {"status": "killed"}

@app.get("/metrics")
def get_metrics():
    query_api = influx.query_api()

    query = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -10m)
      |> filter(fn: (r) => r["_measurement"] == "crypto_price")
      |> filter(fn: (r) => r["_field"] == "price")
      |> filter(fn: (r) => r["coin"] == "{COIN_ID}")
      |> keep(columns: ["_value"])
    '''

    try:
        result = query_api.query(query)
    except Exception as e:
        return {"error": str(e)}

    values = []
    for table in result:
        for record in table.records:
            values.append(record.get_value())

    if not values:
        return {"error": "no data"}

    return {
        "avg_price": sum(values) / len(values),
        "min_price": min(values),
        "max_price": max(values)
    }

@app.on_event("startup")
async def startup_event():
    logging.info(f"INFLUX_BUCKET={INFLUX_BUCKET}")
    logging.info(f"INFLUX_URL={INFLUX_URL}")
    asyncio.create_task(heartbeat())


async def heartbeat():
    while True:
        price = get_price_usd()
        now = datetime.now().isoformat()
        save_price(price)
        logging.info(f"Heartbeat: {now} | {COIN_ID}_{VS_CURRENCY}={price}")
        await asyncio.sleep(60)
