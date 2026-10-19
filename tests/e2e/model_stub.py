"""A deterministic, keyless OpenAI-compatible model for the feature proof (ADR-0127 D1, #2607).

The engine has no built-in mock model (``CAIRN_BACKEND=mock`` is the telemetry backend), and
ask-the-docs makes a real model call through ``build_chat_model`` and embeds through the
``openai`` embedder. The harness starts this stub once per session, in-process, on a free
loopback port, and points every cell's server at it with ``OPENAI_BASE_URL``: the calls
leave the engine exactly as they would for a hosted provider, and nothing needs a key.

- ``POST /v1/chat/completions`` answers from the retrieved passages in the prompt (it names
  the ``*.txt`` file it saw) — a fixed sentence, so a run's answer is reproducible. A
  ``stream: true`` request gets the same answer as one SSE chunk.
- ``POST /v1/embeddings`` returns a hashed bag-of-words vector (``dimensions`` or 1536),
  L2-normalised, so the same text always embeds the same and shared words score close.
- ``GET /v1/models`` lists the one model it pretends to be.

``ModelStub.chat_delay`` holds every chat answer for that many seconds (the DBOS crash cell
kills the server while a template run waits on its model call); ``ModelStub.calls`` counts
the requests per path. Stdlib only, so the public tree needs nothing extra to run it.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DIM = 1536
ANSWER = ("A data gate is answered by data: its predicate reads a leg's output and passes or stops "
          "the pipeline when that leg finishes. A person gate pauses the pipeline until someone "
          "approves or rejects it.")


def embed(text: str, dim: int = DIM) -> list[float]:
    v = [0.0] * dim
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _text(content: Any) -> str:
    if isinstance(content, list):   # the multi-part message form
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def answer_for(messages: list[dict]) -> str:
    seen = " ".join(_text(m.get("content")) for m in messages)
    src = re.findall(r"rag/docs/([a-z_-]+\.txt)", seen) or re.findall(r"\b([a-z_-]+\.txt)\b", seen)
    return f"{ANSWER} (source: {src[0]})" if src else ANSWER


class ModelStub:
    """The stub server: ``start()`` binds a free loopback port; ``base_url`` ends in ``/v1``."""

    def __init__(self) -> None:
        self.chat_delay = 0.0
        self.calls: Counter[str] = Counter()
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:   # quiet: the server log is the record
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 — the http.server spelling
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, json.dumps({"object": "list", "data": [
                        {"id": "gpt-4o-mini", "object": "model", "owned_by": "cairn-proof"}]}).encode())
                else:
                    self._send(404, b'{"error": "not found"}')

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                path = self.path.split("?", 1)[0].rstrip("/")
                with stub._lock:
                    stub.calls[path.rsplit("/v1", 1)[-1]] += 1
                try:
                    if path.endswith("/embeddings"):
                        self._embeddings(body)
                    elif path.endswith("/chat/completions"):
                        self._chat(body)
                    else:
                        self._send(404, b'{"error": "not found"}')
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the caller died mid-call (the crash cells kill it on purpose)

            def _embeddings(self, body: dict) -> None:
                items = body.get("input")
                items = items if isinstance(items, list) else [items]
                dim = int(body.get("dimensions") or DIM)
                data = [{"object": "embedding", "index": i, "embedding": embed(str(t), dim)}
                        for i, t in enumerate(items)]
                self._send(200, json.dumps({"object": "list", "model": body.get("model"), "data": data,
                                            "usage": {"prompt_tokens": len(items), "total_tokens": len(items)}}).encode())

            def _chat(self, body: dict) -> None:
                if stub.chat_delay:
                    time.sleep(stub.chat_delay)
                text = answer_for(body.get("messages") or [])
                usage = {"prompt_tokens": 50, "completion_tokens": len(text.split()), "total_tokens": 50 + len(text.split())}
                base = {"id": "chatcmpl-proof", "created": int(time.time()), "model": body.get("model") or "gpt-4o-mini"}
                if body.get("stream"):
                    chunks = [
                        {**base, "object": "chat.completion.chunk", "choices": [
                            {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                        {**base, "object": "chat.completion.chunk", "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
                    ]
                    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                    self._send(200, sse.encode(), "text/event-stream")
                    return
                self._send(200, json.dumps({**base, "object": "chat.completion", "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                    "usage": usage}).encode())

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, name="model-stub", daemon=True)

    def start(self) -> "ModelStub":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
