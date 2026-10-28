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
import claude_native as cn

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


class Harness(unittest.TestCase):
    """A fake Venus and a proxy in front of it."""
    native = False

    def setUp(self):
        Upstream.script, Upstream.seen = [], []
        self.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.proxy = vp.Server(("127.0.0.1", 0), vp.Handler)
        for s in (self.up, self.proxy):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.failed = tempfile.TemporaryDirectory()
        self.log = io.StringIO()
        for p in (mock.patch.object(vp, "UPSTREAM", f"http://127.0.0.1:{self.up.server_port}"), mock.patch.object(vp, "RETRY_WAIT", 0),
                  mock.patch.object(vp, "FAILED_DIR", self.failed.name), mock.patch.object(vp, "CLAUDE_NATIVE", self.native),
                  mock.patch.object(vp, "THOUGHTS", cn.Thoughts()), redirect_stderr(self.log)):
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)

    def tearDown(self):
        for s in (self.up, self.proxy):
            s.shutdown()
            s.server_close()
        self.failed.cleanup()

    def post(self, model="claude-fable-5-1", headers=None, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.proxy.server_port}/llmproxy/chat/completions", method="POST",
                                     headers={"Authorization": "Bearer k", "Content-Type": "application/json", **(headers or {})},
                                     data=json.dumps(body or {"model": model, "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode())
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def line(self):
        time.sleep(0.05)  # the summary is written just after the answer's last byte
        return [l for l in self.log.getvalue().splitlines() if " POST " in l][-1]


class Forward(Harness):
    """Relaying, with Claude on Venus' OpenAI-compatible endpoint (VENUS_PROXY_CLAUDE_NATIVE=0)."""

    def test_forwards_with_sticky_routing_and_cache_marks_and_streams_back(self):
        Upstream.script = [(200, "text/event-stream", [TEXT, DONE])]
        self.assertEqual(self.post(), (200, TEXT + DONE))
        seen = Upstream.seen[0]
        self.assertEqual((seen["path"], seen["headers"]["Authorization"], seen["headers"]["Venus-Sticky-Routing"]),
                         ("/llmproxy/chat/completions", "Bearer k", "token"))
        self.assertEqual(json.loads(seen["body"])["messages"][0]["content"][0]["cache_control"], C)
        line = self.line()
        for part in ("model=claude-fable-5-1", "api=compat", "route=token", "status=200", "finish=stop", "done=yes",
                     "usage=in:9,read:7,write:0,out:2", "end=ok"):
            self.assertIn(part, line)
        self.assertNotIn("Bearer", self.log.getvalue())
        self.assertEqual(os.listdir(self.failed.name), [])

    def test_conversation_id_becomes_venus_session_id(self):
        Upstream.script = [(200, "text/event-stream", [DONE])] * 2
        self.post(model="kimi-k3.1-pro", headers={"X-Conversation-Id": "c1"})
        self.assertEqual(Upstream.seen[0]["headers"]["Venus-Session-Id"], "c1")
        self.assertNotIn("Venus-Sticky-Routing", Upstream.seen[0]["headers"])
        self.assertIn("route=session", self.line())
        with mock.patch.object(vp, "ROUTING", "token"):
            self.post(model="kimi-k3.1-pro", headers={"X-Conversation-Id": "c1"})
        self.assertEqual(Upstream.seen[1]["headers"]["Venus-Sticky-Routing"], "token")

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


def ev(kind, **kw):
    return b"event:" + kind.encode() + b"\ndata: " + json.dumps({"type": kind, **kw}).encode() + b"\n\n"


SIG = "c2ln"
NATIVE_TOOL = [  # Venus' native stream: thinking, text, a tool call, as recorded from it
    ev("message_start", message={"id": "msg_1", "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7}}),
    ev("content_block_start", index=0, content_block={"type": "thinking", "thinking": "", "signature": ""}),
    ev("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "想一想"}),
    ev("content_block_delta", index=0, delta={"type": "signature_delta", "signature": SIG}),
    ev("content_block_stop", index=0), b"event:ping\ndata: {\"type\":\"ping\"}\n\n",
    ev("content_block_start", index=1, content_block={"type": "text", "text": ""}),
    ev("content_block_delta", index=1, delta={"type": "text_delta", "text": "看一下"}),
    ev("content_block_start", index=2, content_block={"type": "tool_use", "id": "toolu_A", "name": "run", "input": {}}),
    ev("content_block_delta", index=2, delta={"type": "input_json_delta", "partial_json": ""}),
    ev("content_block_delta", index=2, delta={"type": "input_json_delta", "partial_json": "{\"cmd\":"}),
    ev("content_block_delta", index=2, delta={"type": "input_json_delta", "partial_json": "\"ls\"}"}),
    ev("content_block_stop", index=2),
    ev("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 30, "output_tokens_details": {"thinking_tokens": 20}}),
    ev("message_stop")]
TOOLS = [{"type": "function", "function": {"name": "run", "description": "d", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}}]


def chunks(sse):
    return [json.loads(l[6:]) for l in sse.split(b"\n") if l.startswith(b"data: {")]


class Translate(unittest.TestCase):
    def test_codebuddy_request_becomes_a_native_one(self):
        req = {"model": "claude-opus-5-5", "max_tokens": 128000, "reasoning_effort": "high", "stream": True, "tool_choice": "auto", "tools": TOOLS,
               "temperature": 0.2, "messages": [
                   {"role": "system", "content": "规则"},
                   {"role": "user", "content": [{"type": "text", "text": "看图", "cache_control": C},
                                                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
                   {"role": "assistant", "content": "-", "reasoning_content": "", "tool_calls": [
                       {"id": "t1", "type": "function", "function": {"name": "run", "arguments": "{\"cmd\":\"ls\"}"}},
                       {"id": "t2", "type": "function", "function": {"name": "run", "arguments": "not json"}}]},
                   {"role": "tool", "tool_call_id": "t1", "content": "{\"status\":\"success\",\"result\":{\"type\":\"r\",\"stdout\":\"a\\r\\nb\\r\\n\",\"stderr\":\"\",\"exitCode\":0}}"},
                   {"role": "tool", "tool_call_id": "t2", "content": "plain"},
                   {"role": "assistant", "content": "好的"}, {"role": "user", "content": "继续"}]}
        body, turns = cn.to_anthropic(req, cn.Thoughts())
        self.assertEqual((body["max_tokens"], body["thinking"], body["output_config"], body["tool_choice"], body["stream"]),
                         (128000, {"type": "adaptive", "display": "summarized"}, {"effort": "high"}, {"type": "auto"}, True))
        self.assertNotIn("temperature", body)  # not allowed with thinking
        self.assertEqual(body["system"], [{"type": "text", "text": "规则", "cache_control": C}])
        self.assertEqual(body["tools"], [{"name": "run", "description": "d", "input_schema": TOOLS[0]["function"]["parameters"], "cache_control": C}])
        m = body["messages"]
        self.assertEqual([x["role"] for x in m], ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(m[0]["content"], [{"type": "text", "text": "看图"},
                                           {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}])
        self.assertEqual(m[1]["content"], [{"type": "tool_use", "id": "t1", "name": "run", "input": {"cmd": "ls"}},  # "-" dropped
                                           {"type": "tool_use", "id": "t2", "name": "run", "input": {"arguments": "not json"}}])
        self.assertEqual(m[2]["content"][0], {"type": "tool_result", "tool_use_id": "t1", "content": "status: success\nresult:\n  stdout:\na\nb\n  exitCode: 0"})
        self.assertEqual(m[2]["content"][1], {"type": "tool_result", "tool_use_id": "t2", "content": "plain", "cache_control": C})
        self.assertEqual(m[4]["content"], [{"type": "text", "text": "继续", "cache_control": C}])
        self.assertEqual(json.dumps(body).count("cache_control"), 4)  # Anthropic's limit; the client's own marks are dropped
        self.assertEqual(turns, {"turns": 2, "restored": 0})

    def test_effort_none_and_override(self):
        req = {"model": "claude-x", "reasoning_effort": "none", "temperature": 0.3, "messages": [{"role": "user", "content": "hi"}]}
        body, _ = cn.to_anthropic(req, cn.Thoughts())
        self.assertNotIn("thinking", body)
        self.assertEqual((body["temperature"], body["max_tokens"]), (0.3, 32000))
        body, _ = cn.to_anthropic(req, cn.Thoughts(), effort="max")
        self.assertEqual(body["output_config"], {"effort": "max"})
        body, _ = cn.to_anthropic({**req, "reasoning_effort": None}, cn.Thoughts())
        self.assertEqual(body["output_config"], {"effort": "high"})

    def test_signed_thinking_is_carried_back_into_its_turn(self):
        th = cn.Thoughts()
        a = cn.Answer("claude-opus-5-5", True, 200, True, th)
        b"".join(a.feed(c) for c in NATIVE_TOOL)
        req = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "q"},
               {"role": "assistant", "content": "看一下", "tool_calls": [{"id": "toolu_A", "function": {"name": "run", "arguments": "{\"cmd\":\"ls\"}"}}]},
               {"role": "tool", "tool_call_id": "toolu_A", "content": "a.py"}]}
        body, turns = cn.to_anthropic(req, th)
        self.assertEqual(body["messages"][1]["content"][0], {"type": "thinking", "thinking": "想一想", "signature": SIG})
        self.assertEqual(turns, {"turns": 1, "restored": 1})
        a = cn.Answer("m", False, 200, False, th)  # a text-only answer is found again by its text
        a.feed(json.dumps({"content": [{"type": "redacted_thinking", "data": "R"}, {"type": "text", "text": "答案"}], "stop_reason": "end_turn"}).encode())
        a.close()
        body, _ = cn.to_anthropic({"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "答案"}]}, th)
        self.assertEqual(body["messages"][1]["content"][0], {"type": "redacted_thinking", "data": "R"})

    def test_thoughts_forget_the_least_recently_used(self):
        th = cn.Thoughts(2)
        th.put("a", 1), th.put("b", 2), th.get("a"), th.put("c", 3)
        self.assertEqual((th.get("a"), th.get("b"), th.get("c")), (1, None, 3))

    def test_native_stream_becomes_openai_chunks(self):
        a = cn.Answer("claude-opus-5-5", True, 200, True, cn.Thoughts())
        raw = b"".join(NATIVE_TOOL)
        out = b"".join(a.feed(raw[i:i + 9]) for i in range(0, len(raw), 9)) + a.close()  # cut anywhere
        cs = chunks(out)
        self.assertTrue(out.endswith(b"data: [DONE]\n\n"))
        deltas = [c["choices"][0]["delta"] for c in cs]
        self.assertEqual(deltas[0], {"role": "assistant", "content": ""})
        self.assertEqual("".join(d.get("reasoning_content", "") for d in deltas), "想一想")
        self.assertEqual("".join(d.get("content", "") for d in deltas), "看一下")
        tc = [t for d in deltas for t in d.get("tool_calls", [])]
        self.assertEqual((tc[0]["id"], tc[0]["function"]["name"], tc[0]["index"]), ("toolu_A", "run", 0))
        self.assertEqual("".join(t["function"]["arguments"] for t in tc), "{\"cmd\":\"ls\"}")
        self.assertEqual(cs[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(cs[-1]["usage"], {"prompt_tokens": 112, "completion_tokens": 30, "total_tokens": 142,
                                           "prompt_tokens_details": {"cached_tokens": 100, "cache_read_tokens": 100, "cache_creation_tokens": 7},
                                           "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7,
                                           "completion_tokens_details": {"reasoning_tokens": 20}})
        self.assertEqual({c["id"] for c in cs}, {"msg_1"})

    def test_json_answers_and_errors(self):
        d = {"id": "msg_2", "content": [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "hi"},
                                        {"type": "tool_use", "id": "x", "name": "run", "input": {"cmd": "ls"}}],
             "stop_reason": "max_tokens", "usage": {"input_tokens": 3, "output_tokens": 4}}
        a = cn.Answer("m", False, 200, False, cn.Thoughts())
        a.feed(json.dumps(d).encode())
        got = json.loads(a.close())
        self.assertEqual(got["choices"][0], {"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "hi", "reasoning_content": "t",
                         "tool_calls": [{"id": "x", "type": "function", "function": {"name": "run", "arguments": "{\"cmd\": \"ls\"}"}}]}})
        a = cn.Answer("m", True, 200, False, cn.Thoughts())  # stream asked, one piece came: still a stream
        a.feed(json.dumps(d).encode())
        cs = chunks(a.close())
        self.assertEqual((cs[-1]["choices"][0]["finish_reason"], cs[2]["choices"][0]["delta"]), ("length", {"content": "hi"}))
        a = cn.Answer("m", True, 400, False, cn.Thoughts())
        self.assertEqual(a.content_type, "application/json")
        a.feed(b'{"type":"error","error":{"type":"invalid_request_error","message":"bad"}}')
        self.assertEqual(json.loads(a.close()), {"error": {"message": "bad", "type": "invalid_request_error", "code": 400}})
        a = cn.Answer("m", True, 200, True, cn.Thoughts())
        self.assertEqual(chunks(a.feed(ev("error", error={"type": "overloaded_error", "message": "busy"})))[0]["error"]["message"], "busy")

    def test_tool_text_leaves_what_it_cannot_read(self):
        for s in ("plain", "[1,2]", "", "{bad"):
            self.assertEqual(cn.tool_text(s), s)


class NativeForward(Harness):
    native = True

    def body(self, **kw):
        return {"model": "claude-opus-5-5", "stream": True, "reasoning_effort": "high", "tools": TOOLS,
                "messages": [{"role": "user", "content": "hi"}], **kw}

    def test_claude_goes_native_and_comes_back_as_openai(self):
        Upstream.script = [(200, "text/event-stream", NATIVE_TOOL)]
        status, out = self.post(body=self.body(), headers={"X-Conversation-Id": "c9", "Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        seen = Upstream.seen[0]
        self.assertEqual(seen["path"], "/llmproxy/anthropic/v1/messages")
        self.assertEqual((seen["headers"]["Authorization"], seen["headers"]["Venus-Session-Id"], seen["headers"]["anthropic-version"],
                          seen["headers"]["Accept-Encoding"]), ("Bearer k", "c9", "2023-06-01", "identity"))
        self.assertEqual(json.loads(seen["body"])["thinking"]["type"], "adaptive")
        self.assertEqual(chunks(out)[-1]["choices"][0]["finish_reason"], "tool_calls")
        line = self.line()
        for part in ("api=native", "route=session", "thinking_restored=0/0", "stage=tool_calls", "finish=tool_calls", "done=yes",
                     "usage=in:112,read:100,write:7,out:30,think:20", "end=ok"):
            self.assertIn(part, line)
        Upstream.script = [(200, "text/event-stream", NATIVE_TOOL)]  # next turn: the thinking goes back with it
        self.post(body=self.body(messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "看一下", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "run", "arguments": "{\"cmd\":\"ls\"}"}}]},
            {"role": "tool", "tool_call_id": "toolu_A", "content": "a.py"}]))
        self.assertEqual(json.loads(Upstream.seen[1]["body"])["messages"][1]["content"][0]["signature"], SIG)
        self.assertIn("thinking_restored=1/1", self.line())

    def test_refused_signature_retries_without_carried_thinking_then_compat(self):
        vp.THOUGHTS.put("tool:t1", [{"type": "thinking", "thinking": "x", "signature": "bad"}])
        hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "-", "tool_calls": [{"id": "t1", "function": {"name": "run", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "t1", "content": "ok"}]
        refused = (400, "application/json", [b'{"type":"error","error":{"type":"invalid_request_error","message":"bad signature"}}'])
        Upstream.script = [refused, refused, (200, "text/event-stream", [TEXT, DONE])]
        self.assertEqual(self.post(body=self.body(messages=hist)), (200, TEXT + DONE))
        self.assertEqual([s["path"] for s in Upstream.seen], ["/llmproxy/anthropic/v1/messages"] * 2 + ["/llmproxy/chat/completions"])
        self.assertIn("signature", Upstream.seen[0]["body"].decode())
        self.assertNotIn("signature", Upstream.seen[1]["body"].decode())
        self.assertIn("cache_control", Upstream.seen[2]["body"].decode())  # the compat request still gets its cache marks
        line = self.line()
        self.assertIn("api=compat", line)
        self.assertIn("fallback=native_unrestored:400:bad_signature", line)

    def test_native_errors_reach_the_client_in_openai_shape(self):
        Upstream.script = [(429, "application/json", [b'{"type":"error","error":{"type":"rate_limit_error","message":"slow"}}'])] * 2
        status, out = self.post(body=self.body())
        self.assertEqual((status, json.loads(out)["error"]["message"]), (429, "slow"))

    def test_pings_alone_do_not_keep_a_stalled_answer_alive(self):
        ping = b"event:ping\ndata: {\"type\":\"ping\"}\n\n"
        Upstream.script = [(200, "text/event-stream", NATIVE_TOOL[:9] + [0.3, ping] * 10)]
        with mock.patch.object(vp, "STALL", 1.0):
            status, out = self.post(body=self.body())
        self.assertEqual(chunks(out)[-1]["error"]["type"], "upstream_stream_interrupted")
        self.assertIn("end=stalled", self.line())

    def test_other_models_are_not_translated(self):
        Upstream.script = [(200, "text/event-stream", [DONE])]
        self.post(body=self.body(model="kimi-k3.1-pro"))
        self.assertEqual(Upstream.seen[0]["path"], "/llmproxy/chat/completions")
        self.assertIn("api=pass", self.line())


if __name__ == "__main__":
    unittest.main()
