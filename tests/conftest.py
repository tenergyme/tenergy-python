"""Shared fixtures. `server` starts tests/local_server.py's recorder on a free local port."""
from __future__ import annotations

import threading
from http.server import HTTPServer
from typing import Iterator

import pytest
from local_server import Recorder


@pytest.fixture()
def server() -> Iterator[str]:
    Recorder.script, Recorder.seen = [], []
    httpd = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()
    httpd.server_close()
