#!/usr/bin/env python3
"""proxy.py: the naive agent-traffic proxy for Lab 4.

A reverse proxy that prints every request and response and forwards them
unchanged. Standard library only; nothing to install.

    python3 proxy.py                       # listens on http://localhost:8041
    ANTHROPIC_BASE_URL=http://localhost:8041 claude

Everything Claude Code sends to the API now passes through this program and is
printed to the terminal. The proxy changes nothing: headers go through untouched
(a subscription login needs its bearer token and its `anthropic-beta` header),
and the reply is streamed back byte for byte as it arrives.

It also serves a live dashboard of the same traffic at
http://localhost:8041/__dashboard and appends every exchange to traffic.jsonl.

    python3 proxy.py --port 9000
    python3 proxy.py --log /tmp/run.jsonl  # or --no-log
    python3 proxy.py --upstream https://chatgpt.com/backend-api/codex   # for Codex
"""

from __future__ import annotations

import argparse
import codecs
import http.client
import json
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# Headers that describe one hop of the connection, not the message. They are
# never forwarded. `accept-encoding` is dropped too, so the API answers in plain
# text and what we print is readable.
SKIP = {
    "host", "connection", "keep-alive", "transfer-encoding", "te", "trailers",
    "upgrade", "proxy-authorization", "proxy-authenticate", "accept-encoding",
    "content-length",
}

SECRET_HEADERS = ("authorization", "x-api-key")

UPSTREAM = "https://api.anthropic.com"
HERE = Path(__file__).resolve().parent
DASHBOARD = HERE / "dashboard.html"
HISTORY_LIMIT = 200  # exchanges the dashboard gets when it loads


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def redact(headers) -> dict[str, str]:
    return {
        name: value[:16] + "..." if name.lower() in SECRET_HEADERS else value
        for name, value in headers
    }


def parse_body(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


class StreamAssembler:
    """Rebuilds the final message from an Anthropic SSE stream, fed as text."""

    def __init__(self) -> None:
        self.buf = ""
        self.data: list[str] = []
        self.message: dict | None = None
        self.partial: dict[int, str] = {}  # block index -> tool input JSON so far

    def feed(self, text: str) -> None:
        self.buf += text
        *lines, self.buf = self.buf.split("\n")
        for line in lines:
            line = line.rstrip("\r")
            if line.startswith("data:"):
                self.data.append(line[5:].removeprefix(" "))
            elif not line and self.data:
                self.event("\n".join(self.data))
                self.data = []

    def event(self, data: str) -> None:
        try:
            self.apply(json.loads(data))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            pass  # a malformed event must never break the relay

    def apply(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "message_start":
            self.message = dict(ev["message"])
            self.message["content"] = list(self.message.get("content") or [])
            return
        if kind == "error":
            self.message = self.message or {"content": []}
            self.message["error"] = ev.get("error")
            return
        msg = self.message
        if msg is None:
            return
        if kind == "content_block_start":
            i, block = ev["index"], dict(ev["content_block"])
            while len(msg["content"]) <= i:
                msg["content"].append({})
            msg["content"][i] = block
            if "input" in block:
                self.partial[i] = ""
        elif kind == "content_block_delta":
            i, delta = ev["index"], ev["delta"]
            block = msg["content"][i]
            match delta.get("type"):
                case "text_delta":
                    block["text"] = block.get("text", "") + delta["text"]
                case "input_json_delta":
                    self.partial[i] = self.partial.get(i, "") + delta["partial_json"]
                case "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta["thinking"]
                case "signature_delta":
                    block["signature"] = delta["signature"]
                case "citations_delta":
                    block.setdefault("citations", []).append(delta["citation"])
        elif kind == "content_block_stop":
            self.close_block(ev["index"])
        elif kind == "message_delta":
            msg.update(ev.get("delta") or {})
            msg.setdefault("usage", {}).update(ev.get("usage") or {})

    def close_block(self, i: int) -> None:
        if i not in self.partial or self.message is None:
            return
        raw = self.partial.pop(i)
        try:
            self.message["content"][i]["input"] = json.loads(raw) if raw else {}
        except ValueError:
            self.message["content"][i]["input"] = raw

    def result(self) -> dict | None:
        for i in list(self.partial):
            self.close_block(i)
        return self.message


class Hub:
    """Shared state: exchange ids, recent exchanges, the log, and live subscribers.

    One lock covers everything, so a history snapshot and its event sequence
    number always agree and the dashboard can resume from exactly that point.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.log_path: Path | None = None
        self.next_id = 1
        self.seq = 0
        self.recent: deque[tuple[int, dict]] = deque(maxlen=5000)  # for reconnect catch-up
        self.clients: set[queue.Queue] = set()
        self.finished: deque[dict] = deque(maxlen=HISTORY_LIMIT)
        self.inflight: dict[int, dict] = {}

    def load(self, path: Path | None) -> None:
        self.log_path = path
        if not path or not path.exists():
            return
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    ex = json.loads(line)
                    self.next_id = max(self.next_id, int(ex["id"]) + 1)
                except (ValueError, KeyError, TypeError):
                    continue
                self.finished.append(ex)

    def _emit(self, event: dict) -> None:  # lock held
        self.seq += 1
        self.recent.append((self.seq, event))
        for q in self.clients:
            q.put((self.seq, event))

    def begin(self, request: dict) -> int:
        with self.lock:
            xid = self.next_id
            self.next_id += 1
            ex = {"id": xid, "time": datetime.now().isoformat(timespec="seconds"),
                  "request": request, "response": None}
            self.inflight[xid] = {**ex, "stream": []}
            self._emit({"type": "request", **ex})
            return xid

    def chunk(self, xid: int, text: str) -> None:
        with self.lock:
            self.inflight[xid]["stream"].append(text)
            self._emit({"type": "chunk", "id": xid, "data": text})

    def finish(self, xid: int, response: dict) -> None:
        with self.lock:
            ex = self.inflight.pop(xid)
            del ex["stream"]
            ex["response"] = response
            self.finished.append(ex)
            self._emit({"type": "response", "id": xid, "response": response})
            if self.log_path:
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    def snapshot(self) -> dict:
        with self.lock:
            live = [{**ex, "stream": "".join(ex["stream"])} for ex in self.inflight.values()]
            return {"seq": self.seq, "exchanges": sorted([*self.finished, *live], key=lambda e: e["id"])}

    def subscribe(self, since: int | None) -> queue.Queue:
        with self.lock:
            q: queue.Queue = queue.Queue()
            if since is not None:
                oldest = self.recent[0][0] if self.recent else self.seq + 1
                if since > self.seq or since < oldest - 1:
                    q.put((self.seq, {"type": "reset"}))  # proxy restarted or we fell too far behind
                else:
                    for seq, event in self.recent:
                        if seq > since:
                            q.put((seq, event))
            self.clients.add(q)
            return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.clients.discard(q)


HUB = Hub()


class Tap(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # we print our own lines

    def do_POST(self) -> None:
        self.relay()

    def do_GET(self) -> None:
        if self.path.startswith("/__") or self.path == "/favicon.ico":
            self.dashboard()
        else:
            self.relay()

    def do_HEAD(self) -> None:
        self.relay()

    # --- dashboard routes (never forwarded) ---

    def dashboard(self) -> None:
        route = urlsplit(self.path)
        if route.path == "/__dashboard":
            try:
                self.send_body(200, "text/html; charset=utf-8", DASHBOARD.read_bytes())
            except OSError:
                self.send_body(500, "text/plain", b"dashboard.html is missing next to proxy.py")
        elif route.path == "/__history":
            data = json.dumps(HUB.snapshot(), ensure_ascii=False).encode()
            self.send_body(200, "application/json", data)
        elif route.path == "/__events":
            since = self.headers.get("Last-Event-ID") or parse_qs(route.query).get("since", [None])[0]
            self.events(int(since) if since and since.isdigit() else None)
        else:
            self.send_body(404, "text/plain", b"not found")

    def send_body(self, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def events(self, since: int | None) -> None:
        q = HUB.subscribe(since)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                try:
                    seq, event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keeps the connection from idling out
                else:
                    self.wfile.write(f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
        except OSError:
            pass  # the page went away
        finally:
            HUB.unsubscribe(q)

    # --- the proxy itself ---

    def relay(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        started = time.monotonic()
        xid = HUB.begin({"method": self.command, "path": self.path,
                         "headers": redact(self.headers.items()), "body": parse_body(body)})

        def elapsed() -> int:
            return round((time.monotonic() - started) * 1000)

        # 1. Print what the agent sent.
        print(f"\n{'=' * 78}\n[{stamp()}] >>> {self.command} {self.path}", flush=True)
        for name, value in redact(self.headers.items()).items():
            print(f"    {name}: {value}")
        if body:
            try:
                print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
            except ValueError:
                print(body.decode("utf-8", "replace"))

        # 2. Forward it, headers intact.
        target = urlsplit(UPSTREAM)
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        # Connect with a short timeout (a lost SYN must not stall the agent for
        # minutes); the long timeout is for reading a slow stream.
        upstream = conn_cls(target.netloc, timeout=10)
        try:
            upstream.connect()
            upstream.sock.settimeout(600)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP}
            upstream.request(self.command, target.path.rstrip("/") + self.path, body=body or None, headers=headers)
            reply = upstream.getresponse()
        except OSError as e:
            print(f"\n[{stamp()}] !!! cannot reach {UPSTREAM}: {e}", flush=True)
            HUB.finish(xid, {"error": f"cannot reach {UPSTREAM}: {e}", "duration_ms": elapsed()})
            self.send_error(502, f"proxy: cannot reach {UPSTREAM}: {e}")
            self.close_connection = True
            return

        # 3. Stream the reply back as it arrives, printing it on the way.
        print(f"\n[{stamp()}] <<< {reply.status} {reply.reason}", flush=True)
        self.send_response(reply.status, reply.reason)
        for name, value in reply.getheaders():
            if name.lower() not in SKIP and name.lower() != "content-encoding":
                self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        response = {"status": reply.status, "reason": reply.reason,
                    "headers": redact(reply.getheaders()), "body": None}
        if self.command == "HEAD":
            HUB.finish(xid, {**response, "duration_ms": elapsed()})
            return

        assembler = StreamAssembler() if "text/event-stream" in (reply.getheader("content-type") or "") else None
        decoder = codecs.getincrementaldecoder("utf-8")("replace")  # a chunk can split a character
        raw = bytearray()
        try:
            while chunk := reply.read1(4096):  # read1: pass on each chunk as it lands, don't wait for 4096 bytes
                self.wfile.write(chunk)
                self.wfile.flush()
                text = decoder.decode(chunk)
                sys.stdout.write(text)
                sys.stdout.flush()
                if assembler:
                    assembler.feed(text)
                    HUB.chunk(xid, text)
                else:
                    raw += chunk
        except OSError as e:
            response["error"] = f"stream interrupted: {e}"
        print(f"\n[{stamp()}] --- done", flush=True)
        response["body"] = assembler.result() if assembler else parse_body(bytes(raw))
        HUB.finish(xid, {**response, "duration_ms": elapsed()})


def main() -> None:
    global UPSTREAM
    parser = argparse.ArgumentParser(description="Print and forward an agent's API traffic.")
    parser.add_argument("--port", type=int, default=8041)
    parser.add_argument("--upstream", default=UPSTREAM, help="where to forward (default: the Anthropic API)")
    parser.add_argument("--log", type=Path, default=HERE / "traffic.jsonl", help="where to append exchanges (default: traffic.jsonl)")
    parser.add_argument("--no-log", action="store_true", help="keep history in memory only")
    args = parser.parse_args()
    UPSTREAM = args.upstream.rstrip("/")
    HUB.load(None if args.no_log else args.log)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Tap)
    print(f"proxy listening on http://localhost:{args.port}  ->  {UPSTREAM}")
    print(f"run the agent with:  ANTHROPIC_BASE_URL=http://localhost:{args.port} claude")
    print(f"dashboard:           http://localhost:{args.port}/__dashboard")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
