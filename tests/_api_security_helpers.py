"""Loopback-only provider test helpers; no real credentials or external calls."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
import threading
from types import SimpleNamespace

from modules import openai_compat
from modules.tts import cleaner_api

_CREDENTIAL = "synthetic-audit-credential-marker"
_SUCCESS = json.dumps({"choices": [{"message": {"content": "clean sample"}}], "data": [{"id": "audit-model"}]}).encode()

def _contains_no_credential(value):
    return _CREDENTIAL not in str(value)


class UpstreamError(RuntimeError):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


@contextmanager
def _server(*, status=200, redirect=None):
    observations = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def handle_request(self):
            size = int(self.headers.get("Content-Length", "0"))
            if size:
                self.rfile.read(size)
            observations.append({"method": self.command, "path": self.path,
                                 "authenticated": self.headers.get("Authorization") == f"Bearer {_CREDENTIAL}"})
            target = redirect(self.server.server_port) if callable(redirect) else redirect
            if target and self.path != "/final":
                self.send_response(status)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = ("rejected credential: " + _CREDENTIAL).encode() if status >= 400 else _SUCCESS
            self.send_response(status if status >= 400 else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observations
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive(), "audit server leaked a thread"


def _call(kind, url, *, api_key=None):
    credential = _CREDENTIAL if api_key is None else api_key
    if kind == "models":
        return cleaner_api.fetch_openai_model_ids(api_key=credential, base_url=url, timeout_seconds=3)
    if kind == "voice":
        return openai_compat.post_chat_completions_via_urlopen(
            api_key=credential, base_url=url, payload={"messages": []}, timeout_seconds=3,
            http_error_prefix="Audit", connection_error_prefix="Audit connection")
    if kind == "cleaner-sync":
        return cleaner_api.post_api_chat_completions_via_urlopen(
            config=SimpleNamespace(api_timeout_seconds=3), api_key=credential, base_url=url,
            payload={"messages": []}, ssl_context=ssl.create_default_context(), http_error_cls=UpstreamError)
    return asyncio.run(cleaner_api.post_api_chat_completions_with_retry_async(
        config=SimpleNamespace(api_timeout_seconds=3), api_key=credential, base_url=url,
        payload={"messages": []}, ssl_context=ssl.create_default_context(), http_error_cls=UpstreamError,
        max_tokens=20))
