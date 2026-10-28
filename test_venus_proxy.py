"""Tests for venus_proxy.py. Run: python3 -m unittest -v test_venus_proxy (in ~/.venus_proxy)."""
import http.client
import http.server
import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from unittest import mock

import venus_proxy as vp

C = {"type": "ephemeral"}
DONE = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":2,' \
       b'"prompt_tokens_details":{"cache_read_tokens":7,"cache_creation_tokens":0}}}\n\ndata: [DONE]\n\n'
TEXT = b'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
TOOL = b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"t1","function":{"name":"run","arguments":""}}]}}]}\n\n'


class AddCache(unittest.TestCase):
    def test_claude_requests_get_cache_marks_others_pass_as_is(self):
        req = {"model": "claude-opus-5-5", "tools": [{"type": "function"}, {"type": "function"}],
               "messages": [{"role": "system", "content": "规则"}, {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image_url"}]},
                            {"role": "assistant", "content": None, "tool_calls": []}, {"role": "tool", "content": ""}]}
        got = json.loads(vp.add_cache(json.dumps(req).encode()))
        self.assertEqual(got["messages"][0]["content"], [{"type": "text", "text": "规则", "cache_control": C}])
        self.assertEqual(got["messages"][1]["content"][0], {"type": "text", "text": "a", "cache_control": C})  # last message with text
        self.assertEqual(got["tools"], [{"type": "function"}, {"type": "function", "cache_control": C}])
        for body in (json.dumps({**req, "model": "kimi-k3.1-pro"}).encode(), json.dumps(got).encode(), b"not json"):
            self.assertIs(vp.add_cache(body), body)  # not Claude, already marked, or not JSON: untouched

    def test_the_word_in_the_conversation_is_not_a_mark(self):
        talk = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "为啥 cache_control 没生效"}]}
        self.assertEqual(json.loads(vp.add_cache(json.dumps(talk).encode()))["messages"][0]["content"][0]["cache_control"], C)


class WatchTest(unittest.TestCase):
    def test_reads_finish_usage_done_and_stage_across_split_chunks_and_crlf(self):
        w = vp.Watch(sse=True)
        raw = (TOOL + DONE).replace(b"\n", b"\r\n")
        for i in range(0, len(raw), 7):  # events cut at arbitrary points
            w.feed(raw[i:i + 7])
        w.close()
        self.assertEqual((w.finish, w.done, w.stage, w.usage["prompt_tokens"], w.size), ("stop", True, "tool_calls", 9, len(raw)))

    def test_cut_off_stream_is_incomplete_and_an_error_event_counts_as_an_end(self):
        w = vp.Watch(sse=True)
        w.feed(TEXT + TOOL)
        w.close()
        self.assertFalse(w.complete)
        w.feed(b'data: {"error":{"message":"busy"}}\n\n')
        self.assertTrue(w.complete)

    def test_json_answer(self):
        w = vp.Watch(sse=False)
        w.feed(b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3}}')
        w.close()
        self.assertEqual((w.finish, w.stage, w.usage), ("stop", "content", {"prompt_tokens": 3}))


class KeepFailed(unittest.TestCase):
    def test_keeps_the_last_n_bodies_readable_only_by_the_owner(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(vp, "FAILED_DIR", d), mock.patch.object(vp, "KEEP_FAILED", 2):
            paths = [vp._keep_failed(i, b"body%d" % i) for i in range(1, 5)]
            self.assertEqual(sorted(os.listdir(d)), sorted(os.path.basename(p) for p in paths[-2:]))
            self.assertEqual(stat.S_IMODE(os.stat(paths[-1]).st_mode), 0o600)
            with open(paths[-1], "rb") as f:
                self.assertEqual(f.read(), b"body4")
        with mock.patch.object(vp, "KEEP_FAILED", 0):
            self.assertIsNone(vp._keep_failed(9, b"x"))


class Upstream(http.server.BaseHTTPRequestHandler):
    """A fake Venus. Each request takes the next entry of `script`: (status, content_type, [bytes or seconds to sleep])."""
    protocol_version = "HTTP/1.0"  # the body ends when the connection closes, so a stream can be cut off at will
    script, seen = [], []

    def do_POST(self):
        self.seen.append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(int(self.headers["Content-Length"]))})
        status, ctype, parts = self.script.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        for p in parts:
            if isinstance(p, (int, float)):
                time.sleep(p)
            else:
                try:
                    self.wfile.write(p)
                    self.wfile.flush()
                except OSError:
                    self.seen[-1]["cut"] = True  # the proxy let go of us
                    return
        self.seen[-1]["cut"] = False

    def log_message(self, *a):
        pass


class Forward(unittest.TestCase):
    def setUp(self):
        Upstream.script, Upstream.seen = [], []
        self.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.proxy = vp.Server(("127.0.0.1", 0), vp.Handler)
        for s in (self.up, self.proxy):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.failed = tempfile.TemporaryDirectory()
        self.log = io.StringIO()
        for p in (mock.patch.object(vp, "UPSTREAM", f"http://127.0.0.1:{self.up.server_port}"), mock.patch.object(vp, "RETRY_WAIT", 0),
                  mock.patch.object(vp, "FAILED_DIR", self.failed.name), redirect_stderr(self.log)):
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)

    def tearDown(self):
        for s in (self.up, self.proxy):
            s.shutdown()
            s.server_close()
        self.failed.cleanup()

    def post(self, model="claude-fable-5-1", headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.proxy.server_port}/llmproxy/chat/completions", method="POST",
                                     headers={"Authorization": "Bearer k", "Content-Type": "application/json", **(headers or {})},
                                     data=json.dumps({"model": model, "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode())
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def line(self):
        time.sleep(0.05)  # the summary is written just after the answer's last byte
        return [l for l in self.log.getvalue().splitlines() if " POST " in l][-1]

    def test_forwards_with_sticky_routing_and_cache_marks_and_streams_back(self):
        Upstream.script = [(200, "text/event-stream", [TEXT, DONE])]
        self.assertEqual(self.post(), (200, TEXT + DONE))
        seen = Upstream.seen[0]
        self.assertEqual((seen["path"], seen["headers"]["Authorization"], seen["headers"]["Venus-Sticky-Routing"]),
                         ("/llmproxy/chat/completions", "Bearer k", "token"))
        self.assertEqual(json.loads(seen["body"])["messages"][0]["content"][0]["cache_control"], C)
        line = self.line()
        for part in ("model=claude-fable-5-1", "status=200", "finish=stop", "done=yes", "usage=in:9,read:7,write:0,out:2",
                     "cache_control=added", "end=ok"):
            self.assertIn(part, line)
        self.assertNotIn("Bearer", self.log.getvalue())
        self.assertEqual(os.listdir(self.failed.name), [])

    def test_own_routing_header_is_kept(self):
        Upstream.script = [(200, "text/event-stream", [DONE])]
        self.post(model="kimi-k3.1-pro", headers={"Venus-Session-Id": "s1"})
        self.assertNotIn("Venus-Sticky-Routing", Upstream.seen[0]["headers"])
        self.assertNotIn("cache_control", Upstream.seen[0]["body"].decode())

    def test_stream_cut_off_by_venus_ends_with_an_error_event_and_keeps_the_body(self):
        Upstream.script = [(200, "text/event-stream", [TEXT, TOOL])]
        status, body = self.post()
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(TEXT + TOOL))
        err = json.loads(body.split(b"data: ")[-1])["error"]
        self.assertEqual(err["type"], "upstream_stream_interrupted")
        line = self.line()
        self.assertIn("stage=tool_calls", line)
        self.assertIn("end=upstream_eof", line)
        saved = line.split("saved=")[1]
        with open(saved, "rb") as f:
            self.assertEqual(json.loads(f.read())["model"], "claude-fable-5-1")

    def test_silent_venus_is_given_up_on_with_an_error_event(self):
        Upstream.script = [(200, "text/event-stream", [TEXT, TOOL, 2])]
        with mock.patch.object(vp, "STALL", 0.5):
            t0 = time.monotonic()
            status, body = self.post()
        self.assertLess(time.monotonic() - t0, 1.8)
        self.assertIn("超过 0.5 秒没有输出", json.loads(body.split(b"data: ")[-1])["error"]["message"])
        self.assertIn("end=stalled", self.line())

    def test_venus_own_error_event_is_not_doubled(self):
        own = b'data: {"error":{"message":"busy","type":"x"}}\n\n'
        Upstream.script = [(200, "text/event-stream", [own])]
        self.assertEqual(self.post(), (200, own))
        self.assertIn("err=busy", self.line())

    def test_busy_venus_is_retried_once_before_anything_is_sent(self):
        Upstream.script = [(503, "application/json", [b'{"error":{"message":"busy"}}']), (200, "text/event-stream", [DONE])]
        self.assertEqual(self.post(), (200, DONE))
        self.assertEqual(len(Upstream.seen), 2)
        self.assertIn("retried=503", self.line())

    def test_second_failure_is_passed_on_as_is(self):
        Upstream.script = [(429, "application/json", [b'{"error":{"message":"slow down"}}'])] * 2
        status, body = self.post()
        self.assertEqual((status, json.loads(body)["error"]["message"]), (429, "slow down"))
        self.assertEqual(len(Upstream.seen), 2)
        self.assertIn("err=slow down", self.line())

    def test_client_errors_are_not_retried(self):
        Upstream.script = [(400, "application/json", [b'{"error":{"message":"bad"}}'])]
        self.assertEqual(self.post()[0], 400)
        self.assertEqual(len(Upstream.seen), 1)

    def test_unreachable_venus_gives_502_after_one_retry(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]  # bound but never listening: connection refused
            with mock.patch.object(vp, "UPSTREAM", f"http://127.0.0.1:{dead}"):
                status, body = self.post()
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["type"], "proxy_error")
        line = self.line()
        self.assertIn("retried=ConnectionRefusedError", line)
        self.assertIn("end=no_answer:ConnectionRefusedError", line)

    def test_json_answer_passes_through(self):
        answer = b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'
        Upstream.script = [(200, "application/json", [answer])]
        self.assertEqual(self.post(), (200, answer))
        self.assertIn("finish=stop", self.line())

    def test_client_leaving_mid_answer_is_logged_and_venus_is_let_go(self):
        Upstream.script = [(200, "text/event-stream", [TEXT] + [0.1, TEXT] * 30)]
        c = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=10)
        c.request("POST", "/llmproxy/chat/completions", body=b'{"model":"m"}', headers={"Content-Type": "application/json"})
        r = c.getresponse()
        r.read1(10)
        c.sock.shutdown(socket.SHUT_RDWR)
        c.close()
        for _ in range(60):
            if "end=client_gone" in self.log.getvalue():
                break
            time.sleep(0.1)
        self.assertIn("end=client_gone", self.line())
        self.assertEqual(os.listdir(self.failed.name), [])
        for _ in range(60):
            if "cut" in Upstream.seen[0]:
                break
            time.sleep(0.1)
        self.assertTrue(Upstream.seen[0]["cut"])  # Venus stopped sending, well before its 3 s answer would have ended


if __name__ == "__main__":
    unittest.main()
