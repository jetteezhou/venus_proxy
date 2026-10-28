"""A local forwarder in front of Venus (http://v2.open.venus.oa.com/llmproxy) for clients that cannot set request
headers, such as CodeBuddy's custom models (~/.codebuddy/models.json sends only Content-Type and Authorization).
Point their url at http://127.0.0.1:18901/llmproxy and each request goes on to Venus with:
- Venus-Sticky-Routing: token, so one key keeps landing on one upstream and its prompt cache stays warm;
- for Claude models, Anthropic cache_control on the system prompt, the last tool and the last message (Venus caches
  Claude only when asked; same places as DeepSeek Harness' cacheControlFormat: anthropic).
Everything else (path, auth, body, streamed answer) passes through as is. Only 127.0.0.1 can reach it.

It also guards against Venus going quiet or breaking off mid-answer:
- one log line per request: status, timings, longest silence, finish_reason, [DONE], usage, and how it ended;
- one retry when Venus could not take the request at all (nothing has reached the client, so nothing is duplicated);
- after STALL seconds without a byte from Venus it gives up, before CodeBuddy's 300 s per-tool-call timer fires;
- a stream that ends without finish_reason or [DONE] gets an explicit SSE error event, so the client reports a broken
  answer (finish reason "error") instead of taking it for a finished one (finish reason "unknown");
- the bodies of the last few failed requests are kept in failed/ for replay against Venus (headers, so keys, are not).

Run: python3 venus_proxy.py (install.sh on macOS, venus_proxy.service on Linux keep it running). See README.md."""
import http.client
import http.server
import itertools
import json
import os
import socketserver
import sys
import time
import urllib.parse

UPSTREAM = os.environ.get("VENUS_PROXY_UPSTREAM", "http://v2.open.venus.oa.com")
PORT = int(os.environ.get("VENUS_PROXY_PORT", "18901"))
# Longest wait for Venus' next byte. Under CodeBuddy's 300 s tool-call timer (which starts at the tool name, when Venus
# goes quiet while it buffers the arguments), above the ~2.5 s per KB of arguments that buffering takes.
STALL = float(os.environ.get("VENUS_PROXY_STALL", "270"))
ANSWER_TIMEOUT = 900  # until Venus' response headers, and between bytes of a non-streamed answer, which come all at once
KEEP_FAILED = int(os.environ.get("VENUS_PROXY_KEEP_FAILED", "5"))  # bodies of the last N failed requests; 0 keeps none
FAILED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "failed")
RETRY_STATUS = {429, 502, 503, 504}
RETRY_WAIT = 2.0
CACHE = {"type": "ephemeral"}
# hop-by-hop or recomputed here: not passed on in either direction
SKIP = {"host", "connection", "keep-alive", "proxy-connection", "transfer-encoding", "content-length", "upgrade", "te", "trailer"}
_ids = itertools.count(1)


def _log(msg):
    sys.stderr.write(f"{time.strftime('%d/%b/%Y %H:%M:%S')} {msg}\n")
    sys.stderr.flush()


def _text_cache(message):
    """cache_control on the message's last text part (a plain string becomes one); False when it has no text."""
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return False
        message["content"] = [{"type": "text", "text": content, "cache_control": CACHE}]
        return True
    for part in reversed(content if isinstance(content, list) else []):
        if isinstance(part, dict) and part.get("type") == "text":
            part["cache_control"] = CACHE
            return True
    return False


def add_cache(body):
    """The chat completions request body with Claude's cache marks, or body itself when it is not a Claude request
    or already carries marks of its own. Venus caches a marked prefix of at least 1024 tokens."""
    try:
        req = json.loads(body)
    except ValueError:
        return body
    if not isinstance(req, dict) or "claude" not in str(req.get("model", "")).lower():
        return body
    messages = [m for m in req.get("messages") or [] if isinstance(m, dict)]
    parts = [p for m in messages if isinstance(m.get("content"), list) for p in m["content"]] + (req.get("tools") or [])
    if any(isinstance(p, dict) and "cache_control" in p for p in parts):  # the client's own marks; the word in text does not count
        return body
    next((_text_cache(m) for m in messages if m.get("role") in ("system", "developer")), None)
    next((True for m in reversed(messages) if m.get("role") in ("user", "assistant", "tool") and _text_cache(m)), None)
    tools = req.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1]["cache_control"] = CACHE
    return json.dumps(req, ensure_ascii=False).encode()


class Watch:
    """What an answer said on its way through: finish_reason, usage, [DONE], error, and the kind of its last delta."""

    def __init__(self, sse):
        self.sse, self.buf, self.size = sse, b"", 0
        self.finish = self.usage = self.error = None
        self.done, self.stage = False, "-"

    def feed(self, chunk):
        self.size += len(chunk)
        if not self.sse:
            if len(self.buf) < 1 << 20:  # ponytail: only the first 1 MB of a JSON answer is inspected
                self.buf += chunk
            return
        *lines, self.buf = (self.buf + chunk).split(b"\n")
        for line in lines:
            self._line(line.strip())

    def close(self):
        if self.sse:
            self._line(self.buf.strip())
            return
        try:
            self._take(json.loads(self.buf))
        except ValueError:
            pass

    @property
    def complete(self):
        return self.done or self.finish is not None or self.error is not None

    def _line(self, line):
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if data == b"[DONE]":
            self.done = True
            return
        try:
            self._take(json.loads(data))
        except ValueError:
            pass

    def _take(self, ev):
        if not isinstance(ev, dict):
            return
        for c in ev.get("choices") or []:
            if not isinstance(c, dict):
                continue
            delta = c.get("delta") or c.get("message") or {}
            if isinstance(delta, dict):
                self.stage = next((k for k in ("tool_calls", "reasoning_content", "content") if delta.get(k)), self.stage)
            if c.get("finish_reason"):
                self.finish = c["finish_reason"]
        if isinstance(ev.get("usage"), dict):
            self.usage = ev["usage"]
        if ev.get("error"):
            self.error = ev["error"]


def _keep_failed(rid, body):
    """Path of the saved request body (mode 0600; the last KEEP_FAILED are kept), or None."""
    if KEEP_FAILED <= 0:
        return None
    try:
        os.makedirs(FAILED_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(FAILED_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{rid:06d}.json")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            f.write(body)
        for old in sorted(os.listdir(FAILED_DIR))[:-KEEP_FAILED]:
            os.remove(os.path.join(FAILED_DIR, old))
        return path
    except OSError as e:
        _log(f"#{rid} 保存失败请求体出错：{e}")
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self):
        rid, t0 = next(_ids), time.monotonic()
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        marked = False
        if self.command == "POST" and self.path.split("?")[0].endswith("/chat/completions"):
            new = add_cache(body)
            marked, body = new is not body, new
        try:
            model = str(json.loads(body).get("model", "-"))
        except (ValueError, AttributeError):
            model = "-"
        headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP}
        if not any(k.lower() in ("venus-session-id", "venus-sticky-routing") for k in headers):
            headers["Venus-Sticky-Routing"] = "token"
        info = {"status": "-", "ttfb": None, "gap": 0.0, "end": "ok", "retried": None}
        watch = None
        try:
            conn, resp = self._open(body, headers, info)
        except (OSError, http.client.HTTPException) as e:
            info["end"] = f"no_answer:{type(e).__name__}"
            self._error(502, f"本机 Venus 转发连不上 {UPSTREAM}（重试过一次）：{e}")
        else:
            try:
                watch = self._relay(resp, info, t0)
            finally:
                resp.close()
                resp.sock.close()
                conn.close()  # also stops Venus generating for a client that has gone
        self._summary(rid, t0, model, body, marked, info, watch)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward

    def _open(self, body, headers, info):
        """Connection and response from Venus, retried once when Venus could not take the request. Nothing has been sent
        to the client yet, so the retry cannot duplicate part of an answer."""
        up = urllib.parse.urlsplit(UPSTREAM)
        cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
        for attempt in (1, 2):
            conn = cls(up.netloc, timeout=ANSWER_TIMEOUT)
            try:
                conn.request(self.command, self.path, body=body or None, headers={**headers, "Content-Length": str(len(body))})
                sock = conn.sock  # getresponse() drops conn.sock when Venus will close the connection after the answer
                resp = conn.getresponse()
                resp.sock = sock
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                if attempt == 2 or isinstance(e, TimeoutError):  # after ANSWER_TIMEOUT Venus may well be working on it
                    raise
                info["retried"] = type(e).__name__
            else:
                if attempt == 2 or resp.status not in RETRY_STATUS:
                    return conn, resp
                info["retried"] = str(resp.status)
                conn.close()
            time.sleep(RETRY_WAIT)

    def _relay(self, resp, info, t0):
        info["status"], info["ttfb"] = resp.status, time.monotonic() - t0
        sse = "text/event-stream" in (resp.getheader("Content-Type") or "")
        if sse:
            resp.sock.settimeout(STALL)
        watch = Watch(sse)
        try:
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in SKIP:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")  # streamed answers (SSE) go out as they arrive
            self.end_headers()
            last = time.monotonic()
            while True:
                try:
                    chunk = resp.read1(65536)
                except TimeoutError:
                    info["end"] = "stalled"
                    break
                except (OSError, http.client.HTTPException) as e:
                    info["end"] = f"upstream_error:{type(e).__name__}"
                    break
                now = time.monotonic()
                info["gap"], last = max(info["gap"], now - last), now
                if not chunk:
                    break
                watch.feed(chunk)
                self._chunk(chunk)
            info["gap"] = max(info["gap"], time.monotonic() - last)
            watch.close()
            if sse and resp.status == 200 and not watch.complete:
                if info["end"] == "ok":
                    info["end"] = "upstream_eof"  # Venus closed the stream with neither finish_reason nor [DONE]
                why = {"stalled": f"Venus 超过 {STALL:g} 秒没有输出"}.get(info["end"], f"Venus 中途断开（{info['end']}）")
                err = {"error": {"message": f"{why}，这次回复不完整，已由本机转发中止，请重试", "type": "upstream_stream_interrupted"}}
                # the blank line first ends any event Venus left half-written
                self._chunk(b"\n\ndata: " + json.dumps(err, ensure_ascii=False).encode() + b"\n\n")
            elif info["end"] != "ok":
                self.close_connection = True  # no chunked end: the client sees a cut-off body, not a short complete one
                return watch
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:  # BrokenPipe / ConnectionReset: the client went away mid-answer
            info["end"] = "client_gone"
            self.close_connection = True
        return watch

    def _chunk(self, data):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def _error(self, status, message):
        msg = json.dumps({"error": {"message": message, "type": "proxy_error"}}, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
        except OSError:
            self.close_connection = True

    def _summary(self, rid, t0, model, body, marked, info, watch):
        status = info["status"]
        out = [f"#{rid}", self.command, self.path, f"model={model}", f"req={len(body)}B", f"status={status}"]
        if info["retried"]:
            out.append(f"retried={info['retried']}")
        if info["ttfb"] is not None:
            out.append(f"ttfb={info['ttfb']:.1f}s")
        out += [f"dur={time.monotonic() - t0:.1f}s", f"gap={info['gap']:.1f}s"]
        if watch:
            out += [f"resp={watch.size}B", f"stage={watch.stage}", f"finish={watch.finish or '-'}", f"done={'yes' if watch.done else 'no'}"]
            if watch.usage:
                u = watch.usage
                d = u.get("prompt_tokens_details") or {}
                out.append(f"usage=in:{u.get('prompt_tokens', '-')},read:{d.get('cache_read_tokens', d.get('cached_tokens', '-'))},"
                           f"write:{d.get('cache_creation_tokens', '-')},out:{u.get('completion_tokens', '-')}")
            err = watch.error
            if err is None and isinstance(status, int) and status >= 400:
                err = watch.buf[:300].decode("utf-8", "replace")
            if err:
                out.append("err=" + " ".join(str(err.get("message", err) if isinstance(err, dict) else err).split())[:200])
        if marked:
            out.append("cache_control=added")
        out.append(f"end={info['end']}")
        if info["end"] not in ("ok", "client_gone") or (isinstance(status, int) and status >= 500):
            saved = _keep_failed(rid, body)
            if saved:
                out.append(f"saved={saved}")
        _log(" ".join(out))

    def log_request(self, code="-", size="-"):
        pass  # _summary writes the request's line once it is over

    def log_message(self, fmt, *args):
        _log(fmt % args)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    print(f"Venus 转发：http://127.0.0.1:{PORT} -> {UPSTREAM}（Venus-Sticky-Routing: token，Claude 加 cache_control，"
          f"{STALL:g} 秒无输出即中止）", flush=True)
    Server(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
