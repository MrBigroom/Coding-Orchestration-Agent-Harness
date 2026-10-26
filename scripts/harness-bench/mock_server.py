"""Logging mock LLM server: OpenAI chat, OpenAI Responses, Anthropic Messages.

Every POST body is saved to $LOG_DIR/<seq>_<path>.json. Always answers "OK" with no tool calls.
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_DIR = os.environ.get("LOG_DIR", "logs")
SEQ = [0]
LOCK = threading.Lock()


def sse(handler, chunks, named=False):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    for c in chunks:
        if named:
            handler.wfile.write(f"event: {c['type']}\n".encode())
        handler.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
    if not named:
        handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            ids = ["qwen3.8-flash", "qwen3.8-max", "glm-5.3", "deepseek-v4.1-flash"]
            return self._json({"object": "list", "data": [{"id": i, "object": "model", "created": 0, "owned_by": "x"} for i in ids]})
        self._json({})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw)
        except Exception:
            body = {"_raw": raw.decode("utf-8", "replace")}
        with LOCK:
            SEQ[0] += 1
            seq = SEQ[0]
        os.makedirs(LOG_DIR, exist_ok=True)
        safe = self.path.split("?")[0].strip("/").replace("/", "_")
        with open(os.path.join(LOG_DIR, f"{seq:03d}_{safe}.json"), "w") as f:
            json.dump({"path": self.path, "body": body}, f)
        model = body.get("model", "m")
        stream = body.get("stream", False)
        p = self.path.split("?")[0]
        if p.endswith("/count_tokens"):
            return self._json({"input_tokens": 10})
        if p.endswith("/messages"):
            return self.anthropic(model, stream)
        if p.endswith("/responses"):
            return self.responses_api(model)
        return self.chat(model, stream)

    def chat(self, model, stream):
        cid, now = "chatcmpl-1", int(time.time())
        usage = {"prompt_tokens": 1000, "completion_tokens": 7, "total_tokens": 1007, "prompt_tokens_details": {"cached_tokens": 600}}
        if not stream:
            return self._json({"id": cid, "object": "chat.completion", "created": now, "model": model,
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                               "usage": usage})
        base = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": model}
        sse(self, [
            {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"}, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**base, "choices": [], "usage": usage},
        ])

    def anthropic(self, model, stream):
        msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 400, "output_tokens": 1, "cache_read_input_tokens": 600, "cache_creation_input_tokens": 0}}
        if not stream:
            msg.update(content=[{"type": "text", "text": "OK"}], stop_reason="end_turn")
            return self._json(msg)
        sse(self, [
            {"type": "message_start", "message": msg},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 7}},
            {"type": "message_stop"},
        ], named=True)

    def responses_api(self, model):
        item = {"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "OK", "annotations": []}]}
        resp = {"id": "resp_1", "object": "response", "created_at": int(time.time()), "model": model,
                "status": "completed", "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11,
                          "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}
        sse(self, [
            {"type": "response.created", "response": {**resp, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
            {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": "OK"},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": resp},
        ], named=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
