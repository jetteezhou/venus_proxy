"""OpenAI chat completions <-> OpenAI Responses, so venus_proxy can send CodeBuddy's GPT-5+ requests to Venus'
/llmproxy/v1/responses. On /chat/completions these models refuse function tools together with reasoning_effort
("use /v1/responses"); CodeBuddy always sends both. Reasoning items (encrypted, store=false) are carried back into later
turns as Codex does, and prompt_cache_key keeps a conversation on one cache. See README.md."""
import json
import time

from claude_native import _error, turn_key


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


def _parts(content):
    """User content as Responses input: a string stays one, text and image_url parts become input_text / input_image."""
    if isinstance(content, str):
        return content
    out = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            out.append({"type": "input_text", "text": p["text"]})
        elif p.get("type") == "image_url":
            url = p["image_url"].get("url", "") if isinstance(p.get("image_url"), dict) else str(p.get("image_url") or "")
            if url:
                out.append({"type": "input_image", "image_url": url})
    return out


def to_responses(req, thoughts, cache_key=None, effort=None):
    """(Responses request body, {"turns": assistant turns, "restored": those given back their reasoning})."""
    items, turns, restored = [], 0, 0
    for m in req.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("system", "developer"):
            if _text(content):
                items.append({"role": role, "content": _text(content)})
        elif role == "user":
            parts = _parts(content)
            if parts:
                items.append({"role": "user", "content": parts})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id", ""),
                          "output": content if isinstance(content, str) else _text(content)})
        elif role == "assistant":
            calls = [c for c in m.get("tool_calls") or [] if isinstance(c, dict)]
            text = _text(content)
            turns += 1
            think = thoughts.get(turn_key([c.get("id", "") for c in calls], text))
            if think:
                restored += 1
                items += think
            if text and not (calls and text.strip() == "-"):  # "-": CodeBuddy's filler before a tool call
                items.append({"role": "assistant", "content": text})
            for c in calls:
                f = c.get("function") or {}
                args = f.get("arguments")
                items.append({"type": "function_call", "call_id": c.get("id", ""), "name": f.get("name", ""),
                              "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)})

    body = {"model": req.get("model"), "input": items, "store": False, "include": ["reasoning.encrypted_content"]}
    limit = req.get("max_completion_tokens") or req.get("max_tokens")
    if limit:
        body["max_output_tokens"] = int(limit)
    tools = [{"type": "function", "name": f.get("name", ""), "description": f.get("description") or "", "strict": False,
              "parameters": f.get("parameters") or {"type": "object", "properties": {}}}
             for t in req.get("tools") or [] if isinstance(t, dict) and isinstance(f := t.get("function"), dict)]
    if tools:
        body["tools"] = tools
        tc = req.get("tool_choice")
        if isinstance(tc, str):
            body["tool_choice"] = tc
        elif isinstance(tc, dict) and isinstance(tc.get("function"), dict):
            body["tool_choice"] = {"type": "function", "name": tc["function"].get("name", "")}
        if isinstance(req.get("parallel_tool_calls"), bool):
            body["parallel_tool_calls"] = req["parallel_tool_calls"]
    level = effort or req.get("reasoning_effort") or "high"
    level = {"max": "xhigh"}.get(level, level)
    body["reasoning"] = {"effort": level, "summary": "auto"} if level != "none" else {"effort": "none"}
    if level == "none":  # sampling settings are only taken without reasoning
        body.update({k: req[k] for k in ("temperature", "top_p") if k in req})
    if cache_key:
        body["prompt_cache_key"] = cache_key
    if req.get("stream"):
        body["stream"] = True
    return body, {"turns": turns, "restored": restored}


def remember(thoughts, output):
    """Keeps the encrypted reasoning items of an answer (Responses output items) for its turn."""
    think = [{"type": "reasoning", "summary": o.get("summary") or [], "encrypted_content": o["encrypted_content"]}
             for o in output if o.get("type") == "reasoning" and o.get("encrypted_content")]
    if think:
        ids = [o.get("call_id", "") for o in output if o.get("type") == "function_call"]
        text = "".join(c.get("text", "") for o in output if o.get("type") == "message"
                       for c in o.get("content") or [] if isinstance(c, dict) and c.get("type") == "output_text")
        thoughts.put(turn_key(ids, text), think)


def usage(u):
    """Responses usage as chat completions' (input_tokens already includes the cached ones)."""
    read = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
    prompt, out = u.get("input_tokens") or 0, u.get("output_tokens") or 0
    res = {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out,
           "prompt_tokens_details": {"cached_tokens": read, "cache_read_tokens": read}}
    think = (u.get("output_tokens_details") or {}).get("reasoning_tokens")
    if think is not None:
        res["completion_tokens_details"] = {"reasoning_tokens": think}
    return res


def _finish(resp):
    if any(o.get("type") == "function_call" for o in resp.get("output") or [] if isinstance(o, dict)):
        return "tool_calls"
    why = (resp.get("incomplete_details") or {}).get("reason")
    return {"max_output_tokens": "length", "content_filter": "content_filter"}.get(why, "stop")


class Answer:
    """Venus' Responses answer turned, as it arrives, into chat completions: SSE chunks for a stream, one JSON body
    otherwise, OpenAI's error shape for errors. Encrypted reasoning goes to thoughts on the way. Same interface as
    claude_native.Answer."""

    def __init__(self, model, stream, status, upstream_sse, thoughts):
        self.model, self.status, self.thoughts = model, status, thoughts
        self.want_sse = bool(stream) and status == 200
        self.sse = self.want_sse and upstream_sse
        self.content_type = "text/event-stream" if self.want_sse else "application/json"
        self.buf, self.id, self.created = b"", "chatcmpl-venus", int(time.time())
        self.calls = {}  # output_index -> tool_calls index

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
        if not isinstance(d, dict) or d.get("error"):
            return json.dumps(_error(raw, self.status), ensure_ascii=False).encode()
        output = [o for o in d.get("output") or [] if isinstance(o, dict)]
        remember(self.thoughts, output)
        text = "".join(c.get("text", "") for o in output if o.get("type") == "message"
                       for c in o.get("content") or [] if isinstance(c, dict) and c.get("type") == "output_text")
        think = "\n\n".join(s.get("text", "") for o in output if o.get("type") == "reasoning" for s in o.get("summary") or [])
        calls = [{"id": o.get("call_id"), "type": "function", "function": {"name": o.get("name"), "arguments": o.get("arguments") or ""}}
                 for o in output if o.get("type") == "function_call"]
        finish, use = _finish(d), usage(d.get("usage") or {})
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
        """One SSE line of Venus' answer; b"" for the events chat completions has no counterpart for."""
        if not line.startswith(b"data:"):
            return b""
        try:
            ev = json.loads(line[5:])
        except ValueError:
            return b""
        if not isinstance(ev, dict):
            return b""
        kind = ev.get("type")
        if kind == "response.created":
            self.id = (ev.get("response") or {}).get("id") or self.id
            return self._chunk({"role": "assistant", "content": ""})
        if kind == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                n = self.calls[ev.get("output_index")] = len(self.calls)
                return self._chunk({"tool_calls": [{"index": n, "id": item.get("call_id"), "type": "function",
                                                    "function": {"name": item.get("name"), "arguments": ""}}]})
            return b""
        if kind == "response.function_call_arguments.delta" and ev.get("output_index") in self.calls and ev.get("delta"):
            return self._chunk({"tool_calls": [{"index": self.calls[ev["output_index"]], "function": {"arguments": ev["delta"]}}]})
        if kind == "response.output_text.delta" and ev.get("delta"):
            return self._chunk({"content": ev["delta"]})
        if kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta") and ev.get("delta"):
            return self._chunk({"reasoning_content": ev["delta"]})
        if kind == "response.reasoning_summary_part.added" and ev.get("summary_index"):
            return self._chunk({"reasoning_content": "\n\n"})
        if kind in ("response.completed", "response.incomplete"):
            resp = ev.get("response") or {}
            remember(self.thoughts, [o for o in resp.get("output") or [] if isinstance(o, dict)])
            return self._chunk({}, _finish(resp), usage(resp.get("usage") or {})) + b"data: [DONE]\n\n"
        if kind in ("response.failed", "error"):
            err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
            return b"data: " + json.dumps(_error(json.dumps({"error": err}).encode(), self.status), ensure_ascii=False).encode() + b"\n\n"
        return b""
