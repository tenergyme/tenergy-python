"""A real local HTTP server for the transport tests: each request answers from `Recorder.script`
and is recorded in `Recorder.seen` (method, path, lower-cased headers, raw body)."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler


class Recorder(BaseHTTPRequestHandler):
    # (status, body, extra headers) answered by the next request; set per test.
    script: list[tuple[int, bytes, dict[str, str]]] = []
    seen: list[dict[str, object]] = []

    def answer(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        Recorder.seen.append(
            {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
        )
        status, payload, extra = Recorder.script.pop(0)
        self.send_response(status)
        self.send_header("content-length", str(len(payload)))
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = answer

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


def ok(payload: object, status: int = 200, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    return status, json.dumps(payload).encode(), {"content-type": "application/json", **(headers or {})}
