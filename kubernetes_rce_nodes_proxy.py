#!/usr/bin/env python3
import asyncio
import ssl
import json
import sys
import os
import websockets

NODE_IP    = "10.129.143.154"
NS         = "monitoring"
POD        = "prometheus-prometheus-node-exporter-nmntq"
CONTAINER  = "node-exporter"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"

TOKEN = open(TOKEN_FILE).read().strip()

def build_url(cmd, tty=False, stdin=False):
    import urllib.parse
    params = [("output", "1"), ("error", "1")]
    if tty:   params.append(("tty", "1"))
    if stdin: params.append(("stdin", "1"))
    for c in cmd:
        params.append(("command", c))
    qs = urllib.parse.urlencode(params)
    return f"wss://{NODE_IP}:10250/exec/{NS}/{POD}/{CONTAINER}?{qs}"

async def run(cmd, tty=False, stdin=False):
    url = build_url(cmd, tty=tty, stdin=stdin)
    ctx = ssl._create_unverified_context()

    try:
        async with websockets.connect(
            url,
            ssl=ctx,
            subprotocols=["v4.channel.k8s.io"],
            additional_headers={"Authorization": f"Bearer {TOKEN}"},
            open_timeout=10,
            close_timeout=5,
            max_size=None,
        ) as ws:

            # If we want an interactive shell, pump stdin -> channel 0
            async def stdin_pump():
                loop = asyncio.get_event_loop()
                while True:
                    data = await loop.run_in_executor(None, sys.stdin.buffer.readline)
                    if not data:
                        # send EOF on channel 0
                        await ws.send(b"\x00")
                        return
                    # channel 0 = stdin
                    await ws.send(b"\x00" + data)

            pump_task = None
            if stdin:
                pump_task = asyncio.create_task(stdin_pump())

            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=None if stdin else 5)
                    except asyncio.TimeoutError:
                        break
                    if not isinstance(msg, bytes) or len(msg) < 1:
                        continue
                    ch = msg[0]
                    data = msg[1:]
                    if ch == 1:
                        sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
                    elif ch == 2:
                        sys.stderr.buffer.write(data); sys.stderr.buffer.flush()
                    elif ch == 3:
                        try:
                            status = json.loads(data)
                            if status.get("status") != "Success":
                                sys.stderr.write(f"\n[status] {status}\n")
                        except Exception:
                            sys.stderr.write(f"\n[ch3] {data!r}\n")
            finally:
                if pump_task:
                    pump_task.cancel()
    except websockets.exceptions.ConnectionClosedOK:
        pass
    except Exception as e:
        print(f"\n[err] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    # argparse-lite: if args given, run them as a one-shot command
    if len(sys.argv) > 1:
        # detect interactive marker
        if sys.argv[1] == "-i":
            cmd = sys.argv[2:] or ["/bin/sh"]
            asyncio.run(run(cmd, tty=True, stdin=True))
        else:
            asyncio.run(run(sys.argv[1:]))
    else:
        # default: interactive /bin/sh
        asyncio.run(run(["/bin/sh"], tty=True, stdin=True))
