#!/usr/bin/env python3
"""
vis_server.py — WebSocket backend for visualiser.html live experiment mode.

Fires real HTTP requests at your Docker servers at a Poisson-distributed arrival
rate, streaming each completed request's metrics back to the browser.

Usage:
    pip install aiohttp websockets
    python vis_server.py

Then open visualiser.html and use the Live Experiment panel.
Port: ws://localhost:8765
"""
import asyncio
import json
import random
import time
import sys

try:
    import aiohttp
    import websockets
except ImportError:
    print("Missing deps — run:  pip install aiohttp websockets")
    sys.exit(1)

PORT = 8765

# Known servers from docker-compose.yml (sent to browser on connect)
SERVERS = [
    {"name": "Go lognormal (1c)",       "url": "http://localhost:8080", "c": 1},
    {"name": "Apache PHP (1c)",         "url": "http://localhost:8082", "c": 1},
    {"name": "Apache DSP/AES (1c)",     "url": "http://localhost:8083", "c": 1},
    {"name": "Node.js DSP (1c)",        "url": "http://localhost:8084", "c": 1},
    {"name": "Python DSP (1c)",         "url": "http://localhost:8085", "c": 1},
    {"name": "Java DSP 4-thread (mc)",  "url": "http://localhost:8086", "c": 4},
    {"name": "Go SQLite (1c)",          "url": "http://localhost:8087", "c": 1},
    {"name": "Node.js DSP 3-proc (mc)", "url": "http://localhost:8088", "c": 3},
    {"name": "Python DSP 3-proc (mc)",  "url": "http://localhost:8089", "c": 3},
]


async def load_generator(ws, url: str, rate_holder: list, stop: asyncio.Event):
    """
    Generates Poisson arrivals at rate_holder[0] rps, fires each as an async
    HTTP GET, and streams the result back over the WebSocket.
    rate_holder is a 1-element list so the handler can change rate mid-run.
    """
    t0 = time.perf_counter()
    seq = 0
    connector = aiohttp.TCPConnector(limit=0, force_close=False)

    async with aiohttp.ClientSession(connector=connector) as session:
        while not stop.is_set():
            rate = max(rate_holder[0], 0.1)
            gap  = random.expovariate(rate)   # Poisson inter-arrival
            await asyncio.sleep(gap)

            if stop.is_set():
                break

            seq += 1
            send_at    = time.perf_counter()
            arrival_ms = (send_at - t0) * 1000
            n          = seq

            # Fire request concurrently — don't await it, just create task
            async def do_req(n=n, send_at=send_at, arrival_ms=arrival_ms):
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        recv_at = time.perf_counter()
                        resp_ms = (recv_at - send_at) * 1000
                        # Servers can optionally return X-Service-Ms header
                        svc_hdr = resp.headers.get("X-Service-Ms")
                        payload = {
                            "type":        "req",
                            "n":           n,
                            "arrival_ms":  round(arrival_ms, 2),
                            "response_ms": round(resp_ms, 2),
                            "service_ms":  round(float(svc_hdr), 2) if svc_hdr else None,
                            "status":      resp.status,
                        }
                except asyncio.TimeoutError:
                    payload = {"type": "req", "n": n, "arrival_ms": round(arrival_ms, 2),
                               "response_ms": None, "service_ms": None, "status": 0, "error": "timeout"}
                except Exception as ex:
                    payload = {"type": "req", "n": n, "arrival_ms": round(arrival_ms, 2),
                               "response_ms": None, "service_ms": None, "status": 0, "error": str(ex)}
                try:
                    await ws.send(json.dumps(payload))
                except Exception:
                    pass  # ws closed mid-run

            asyncio.create_task(do_req())


async def handler(ws):
    stop_event  = asyncio.Event()
    rate_holder = [10.0]
    load_task   = None

    # Immediately send server catalogue to the browser
    await ws.send(json.dumps({"type": "servers", "servers": SERVERS}))

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            cmd = msg.get("cmd")

            if cmd == "start":
                # Stop any existing load task
                if load_task and not load_task.done():
                    stop_event.set()
                    try:
                        await asyncio.wait_for(load_task, timeout=1.0)
                    except asyncio.TimeoutError:
                        pass

                stop_event  = asyncio.Event()
                rate_holder = [float(msg.get("rate", 10))]
                url         = msg.get("url", "http://localhost:8080")

                load_task = asyncio.create_task(
                    load_generator(ws, url, rate_holder, stop_event)
                )
                await ws.send(json.dumps({
                    "type": "status",
                    "msg":  f"started → {url} @ {rate_holder[0]:.0f} rps",
                }))
                print(f"[start] {url} @ {rate_holder[0]:.0f} rps")

            elif cmd == "setrate":
                rate_holder[0] = float(msg.get("rate", rate_holder[0]))
                print(f"[setrate] → {rate_holder[0]:.0f} rps")

            elif cmd == "stop":
                stop_event.set()
                await ws.send(json.dumps({"type": "status", "msg": "stopped"}))
                print("[stop]")

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        stop_event.set()
        print("[disconnected]")


async def main():
    print(f"vis_server  ws://localhost:{PORT}")
    print("Open visualiser.html → Live Experiment → Connect")
    print("-" * 48)
    async with websockets.serve(handler, "localhost", PORT):
        await asyncio.Future()   # run forever


if __name__ == "__main__":
    asyncio.run(main())
