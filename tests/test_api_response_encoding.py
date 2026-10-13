"""Malformed provider bytes must become controlled upstream failures."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from tests._api_security_helpers import _call


@pytest.mark.parametrize("kind", ["models", "voice", "cleaner-sync", "cleaner-async"])
@pytest.mark.parametrize("status", [200, 400])
def test_invalid_utf8_is_a_controlled_provider_error(kind, status):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"\xff")

        do_GET = respond
        do_POST = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        with pytest.raises(RuntimeError):
            _call(kind, f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
