"""OpenAI chat completions <-> Anthropic Messages, so venus_proxy can send CodeBuddy's Claude requests to Venus' native
endpoint (/llmproxy/anthropic/v1/messages). Through Venus' OpenAI-compatible endpoint Claude hardly thinks whatever
reasoning_effort says; through the native one adaptive thinking is on, and its signed blocks can be carried back in
later requests (CodeBuddy drops them), as Cursor does. See README.md."""
import collections
import hashlib
import json
import threading
import time

CACHE = {"type": "ephemeral"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls",
          "pause_turn": "stop", "refusal": "content_filter"}


class Thoughts:
    """Signed thinking blocks of recent answers by turn, put back into the history of later requests. Every request of a
    conversation must carry the same blocks, or its cached prefix changes: so entries live as long as they are used.
    ponytail: in memory only; a restart costs each running conversation one cache miss. Save to a file if that bites."""

    def __init__(self, size=5000):
        self.size, self.d, self.lock = size, collections.OrderedDict(), threading.Lock()

    def put(self, key, blocks):
        with self.lock:
            self.d[key] = blocks
            self.d.move_to_end(key)
            while len(self.d) > self.size:
                self.d.popitem(last=False)

    def get(self, key):
        with self.lock:
            if key in self.d:
                self.d.move_to_end(key)
                return self.d[key]
        return None


def turn_key(tool_ids, text):
    """An assistant turn is known by its first tool call id, or by its text when it called no tool."""
    if tool_ids and tool_ids[0]:
        return "tool:" + tool_ids[0]
    return "text:" + hashlib.sha256(text.strip().encode()).hexdigest()


def remember(thoughts, blocks):
    """Keeps the signed thinking of an answer (Anthropic content blocks) for its turn."""
    think = [{k: b[k] for k in ("type", "thinking", "signature", "data") if k in b} for b in blocks
             if (b.get("type") == "thinking" and b.get("signature")) or (b.get("type") == "redacted_thinking" and b.get("data"))]
    if think:
        ids = [b.get("id", "") for b in blocks if b.get("type") == "tool_use"]
        thoughts.put(turn_key(ids, "".join(b.get("text", "") for b in blocks if b.get("type") == "text")), think)


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


def _parts(content):
    """OpenAI message content (a string, or text and image_url parts) as Anthropic blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            out.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image_url":
            url = p["image_url"].get("url", "") if isinstance(p.get("image_url"), dict) else str(p.get("image_url") or "")
            if url.startswith("data:") and ";base64," in url:
                media, data = url[5:].split(";base64,", 1)
                out.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
            elif url:
                out.append({"type": "image", "source": {"type": "url", "url": url}})
    return out


def _lines(d, indent=""):
    for k, v in d.items():
        if v is None or v == "" or v == [] or v == {} or (indent and k == "type"):  # result.type just repeats the tool name
            continue
        if isinstance(v, dict):
            yield f"{indent}{k}:"
            yield from _lines(v, indent + "  ")
        elif isinstance(v, str):
            yield f"{indent}{k}:\n{v.rstrip()}" if "\n" in v else f"{indent}{k}: {v}"
        else:
            yield f"{indent}{k}: {json.dumps(v, ensure_ascii=False)}"


def tool_text(s):
    """CodeBuddy wraps tool results in JSON ({"status":..,"result":{"stdout":"..\\r\\n.."}}), every newline escaped. The
    model reads them better, and in fewer tokens, as plain key: value lines. Anything that is not a JSON object passes."""
    try:
        v = json.loads(s)
    except ValueError:
        return s
    if not isinstance(v, dict):
        return s
    return "\n".join(_lines(v)).replace("\r\n", "\n") or s


def _args(s):
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s or "{}")
    except ValueError:
        return {"arguments": s}
    return v if isinstance(v, dict) else {"arguments": v}


def to_anthropic(req, thoughts, effort=None):
    """(Anthropic Messages request body, {"turns": assistant turns, "restored": those given back their thinking})."""
    msgs, system, turns, restored = [], [], 0, 0

    def add(role, blocks):
        if msgs and msgs[-1]["role"] == role:  # Anthropic wants turns to alternate; tool results join the user turn
            msgs[-1]["content"] = msgs[-1]["content"] + blocks
        elif blocks:
            msgs.append({"role": role, "content": blocks})

    for m in req.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("system", "developer"):  # ponytail: system text anywhere in the history goes to the top
            if _text(content):
                system.append({"type": "text", "text": _text(content)})
        elif role == "user":
            add("user", _parts(content))
        elif role == "tool":
            result = tool_text(content) if isinstance(content, str) else (_parts(content) or "")
            add("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": result}])
        elif role == "assistant":
            calls = [c for c in m.get("tool_calls") or [] if isinstance(c, dict)]
            text = _text(content)
            blocks = [{"type": "text", "text": text}] if text and not (calls and text.strip() == "-") else []  # "-": CodeBuddy's filler
            for c in calls:
                f = c.get("function") or {}
                blocks.append({"type": "tool_use", "id": c.get("id", ""), "name": f.get("name", ""), "input": _args(f.get("arguments"))})
            turns += 1
            think = thoughts.get(turn_key([c.get("id", "") for c in calls], text))
            if think:
                restored += 1
                blocks = think + blocks
            add("assistant", blocks or [{"type": "text", "text": "-"}])

    body = {"model": req.get("model"), "max_tokens": int(req.get("max_completion_tokens") or req.get("max_tokens") or 32000), "messages": msgs}
    if system:
        body["system"] = system
    tools = [{"name": f.get("name", ""), "description": f.get("description") or "",
              "input_schema": f.get("parameters") or {"type": "object", "properties": {}}}
             for t in req.get("tools") or [] if isinstance(t, dict) and isinstance(f := t.get("function"), dict)]
    if tools:
        body["tools"] = tools
        tc = req.get("tool_choice")
        choice = {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "none"}}.get(tc) if isinstance(tc, str) else \
            {"type": "tool", "name": tc["function"].get("name", "")} if isinstance(tc, dict) and isinstance(tc.get("function"), dict) else None
        if req.get("parallel_tool_calls") is False:
            choice = {**(choice or {"type": "auto"}), "disable_parallel_tool_use": True}
        if choice:
            body["tool_choice"] = choice
    level = effort or req.get("reasoning_effort") or "high"
    level = {"minimal": "low"}.get(level, level)
    if level in EFFORTS:
        body["thinking"] = {"type": "adaptive", "display": "summarized"}  # summarized: the thinking text comes back too
        body["output_config"] = {"effort": level}
    else:  # "none": no thinking, and sampling settings are allowed again
        body.update({k: req[k] for k in ("temperature", "top_p") if k in req})
    stop = req.get("stop")
    if stop:
        body["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    if req.get("stream"):
        body["stream"] = True
    _mark(body)
    return body, {"turns": turns, "restored": restored}


def _mark(body):
    """Anthropic takes at most 4 cache breakpoints (CodeBuddy sends ~20; the client's own are dropped): the tools, the
    system prompt, and the ends of the last two user turns, so this request reads what the previous one wrote."""
    if body.get("tools"):
        body["tools"][-1]["cache_control"] = CACHE
    if body.get("system"):
        body["system"][-1]["cache_control"] = CACHE
    for m in [m for m in body["messages"] if m["role"] == "user"][-2:]:
        m["content"][-1] = {**m["content"][-1], "cache_control": CACHE}


def usage(u):
    """Anthropic usage as OpenAI's, as Venus' compatible endpoint reports it for Claude (prompt_tokens includes the cache)."""
    read, write = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
    prompt, out = (u.get("input_tokens") or 0) + read + write, u.get("output_tokens") or 0
    res = {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out,
           "prompt_tokens_details": {"cached_tokens": read, "cache_read_tokens": read, "cache_creation_tokens": write},
           "cache_read_input_tokens": read, "cache_creation_input_tokens": write}
    think = (u.get("output_tokens_details") or {}).get("thinking_tokens")
    if think is not None:
        res["completion_tokens_details"] = {"reasoning_tokens": think}
    return res


def _error(raw, status):
    try:
        e = json.loads(raw).get("error") or {}
    except (ValueError, AttributeError):
        e = {}
    if not isinstance(e, dict):
        e = {"message": str(e)}
    msg = e.get("message") or raw.decode("utf-8", "replace")[:500] or f"Venus 返回 {status}"
    return {"error": {"message": msg, "type": e.get("type") or "upstream_error", "code": status}}


class Answer:
    """Venus' native answer turned, as it arrives, into what an OpenAI chat completions client expects: SSE chunks for a
    stream, one JSON body otherwise, OpenAI's error shape for errors. Signed thinking goes to thoughts on the way."""

    def __init__(self, model, stream, status, upstream_sse, thoughts):
        self.model, self.status, self.thoughts = model, status, thoughts
        self.want_sse = bool(stream) and status == 200  # what the client gets
        self.sse = self.want_sse and upstream_sse  # what Venus sends
        self.content_type = "text/event-stream" if self.want_sse else "application/json"
        self.buf, self.id, self.created = b"", "chatcmpl-venus", int(time.time())
        self.use, self.blocks, self.calls, self.finish = {}, {}, 0, None

    def feed(self, chunk):
        if not self.sse:
            self.buf += chunk
            return b""
        *lines, self.buf = (self.buf + chunk).split(b"\n")
        return b"".join(self._line(line.strip()) for line in lines)

    def close(self):
        if self.sse:
            out, self.buf = self._line(self.buf.strip()), b""
            return out
        raw, self.buf = self.buf, b""
        if self.status != 200:
            return json.dumps(_error(raw, self.status), ensure_ascii=False).encode()
        try:
            d = json.loads(raw)
        except ValueError:
            return b"" if self.want_sse else raw  # cut off: the relay reports the stream as broken
        if not isinstance(d, dict) or d.get("type") == "error":
            return json.dumps(_error(raw, self.status), ensure_ascii=False).encode()
        content = [b for b in d.get("content") or [] if isinstance(b, dict)]
        remember(self.thoughts, content)
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        think = "".join(b.get("thinking", "") for b in content if b.get("type") == "thinking")
        calls = [{"id": b.get("id"), "type": "function", "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                 for b in content if b.get("type") == "tool_use"]
        finish, use = FINISH.get(d.get("stop_reason"), "stop"), usage(d.get("usage") or {})
        self.id = d.get("id") or self.id
        if self.want_sse:  # asked for a stream, Venus answered in one piece
            out = self._chunk({"role": "assistant", "content": ""})
            out += self._chunk({"reasoning_content": think}) if think else b""
            out += self._chunk({"content": text}) if text else b""
            out += self._chunk({"tool_calls": [{**c, "index": i} for i, c in enumerate(calls)]}) if calls else b""
            return out + self._chunk({}, finish, use) + b"data: [DONE]\n\n"
        msg = {"role": "assistant", "content": text or None}
        if think:
            msg["reasoning_content"] = think
        if calls:
            msg["tool_calls"] = calls
        return json.dumps({"id": self.id, "object": "chat.completion", "created": self.created, "model": self.model,
                           "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": use}, ensure_ascii=False).encode()

    def _chunk(self, delta, finish=None, use=None):
        c = {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if use:
            c["usage"] = use
        return b"data: " + json.dumps(c, ensure_ascii=False).encode() + b"\n\n"

    def _line(self, line):
        """One SSE line of Venus' answer; b"" for pings and bookkeeping events."""
        if not line.startswith(b"data:"):
            return b""
        try:
            ev = json.loads(line[5:])
        except ValueError:
            return b""
        if not isinstance(ev, dict):
            return b""
        kind = ev.get("type")
        if kind == "message_start":
            m = ev.get("message") or {}
            self.id = m.get("id") or self.id
            self.use.update(m.get("usage") or {})
            return self._chunk({"role": "assistant", "content": ""})
        if kind == "content_block_start":
            b = dict(ev.get("content_block") or {})
            self.blocks[ev.get("index")] = b
            if b.get("type") == "tool_use":
                b["_n"], self.calls = self.calls, self.calls + 1
                return self._chunk({"tool_calls": [{"index": b["_n"], "id": b.get("id"), "type": "function",
                                                    "function": {"name": b.get("name"), "arguments": ""}}]})
            if b.get("type") == "text" and b.get("text"):
                return self._chunk({"content": b["text"]})
            return b""
        if kind == "content_block_delta":
            b, d = self.blocks.setdefault(ev.get("index"), {}), ev.get("delta") or {}
            if d.get("type") == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
                return self._chunk({"content": d.get("text", "")})
            if d.get("type") == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
                return self._chunk({"reasoning_content": d.get("thinking", "")})
            if d.get("type") == "signature_delta":
                b["signature"] = b.get("signature", "") + d.get("signature", "")
            elif d.get("type") == "input_json_delta" and "_n" in b and d.get("partial_json"):
                return self._chunk({"tool_calls": [{"index": b["_n"], "function": {"arguments": d["partial_json"]}}]})
            return b""
        if kind == "message_delta":
            self.use.update({k: v for k, v in (ev.get("usage") or {}).items() if v is not None})
            self.finish = FINISH.get((ev.get("delta") or {}).get("stop_reason"), "stop")
            return b""
        if kind == "message_stop":
            remember(self.thoughts, [self.blocks[i] for i in sorted(self.blocks, key=lambda i: i if isinstance(i, int) else -1)])
            return self._chunk({}, self.finish or "stop", usage(self.use)) + b"data: [DONE]\n\n"
        if kind == "error":
            return b"data: " + json.dumps(_error(json.dumps(ev).encode(), self.status), ensure_ascii=False).encode() + b"\n\n"
        return b""  # ping
