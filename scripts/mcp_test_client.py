"""Keep a real stdio MCP connection alive for staged, operator-driven checks.

Read JSONL requests from a workspace file and append full JSONL responses. Each
request is {id, tool, arguments}; {id, stop: true} closes the client and server.
This helper does not choose hardware or generate commands itself.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=float, default=300)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable,
                                  args=["-X", "utf8", str(root / "launch.py"), "--workspace", str(root)],
                                  cwd=str(root))
    args.responses.parent.mkdir(parents=True, exist_ok=True)

    def emit(value):
        line = json.dumps(value, ensure_ascii=False)
        with args.responses.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        print(line, flush=True)

    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
            hello = await client.initialize()
            emit({"id": "initialize", "result": hello.model_dump(mode="json")})
            offset, idle_since = 0, time.monotonic()
            while time.monotonic() - idle_since < args.idle_seconds:
                if not args.requests.exists():
                    await asyncio.sleep(0.2)
                    continue
                with args.requests.open("rb") as stream:
                    stream.seek(offset)
                    line = stream.readline()
                    if not line.endswith(b"\n"):
                        line = b""
                    else:
                        offset = stream.tell()
                if not line:
                    await asyncio.sleep(0.2)
                    continue
                idle_since = time.monotonic()
                request = json.loads(line)
                if request.get("stop"):
                    emit({"id": request["id"], "status": "stopping"})
                    return
                try:
                    result = await client.call_tool(request["tool"], request.get("arguments", {}))
                    payload = result.structuredContent
                    if payload is None:
                        payload = json.loads("".join(item.text for item in result.content if item.type == "text"))
                    emit({"id": request["id"], "tool": request["tool"], "is_error": result.isError, "result": payload})
                except Exception as exc:
                    emit({"id": request["id"], "error": f"{type(exc).__name__}: {exc}"})
            emit({"id": "idle_timeout", "status": "closing"})


if __name__ == "__main__":
    asyncio.run(main())
