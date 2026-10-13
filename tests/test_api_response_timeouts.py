"""A successful status followed by a stalled body must preserve the error boundary."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import threading
from types import SimpleNamespace

import pytest

from modules import openai_compat
from modules.tts import cleaner_api
from tests._api_security_helpers import UpstreamError


@contextmanager
def stalled_body(status=200):
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def handle_request(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', '20')
            self.end_headers()
            self.wfile.flush()
            done.wait(2)

        do_GET = handle_request
        do_POST = handle_request

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}'
    finally:
        done.set()
        http.shutdown()
        http.server_close()
        thread.join(3)
        assert not thread.is_alive()


@pytest.mark.parametrize('kind', ['models', 'voice', 'cleaner-sync', 'cleaner-async'])
@pytest.mark.parametrize('status', [200, 401])
def test_stalled_provider_body_raises_controlled_error(kind, status):
    with stalled_body(status) as url:
        with pytest.raises(RuntimeError):
            if kind == 'models':
                cleaner_api.fetch_openai_model_ids(api_key='synthetic', base_url=url, timeout_seconds=.1)
            elif kind == 'voice':
                openai_compat.post_chat_completions_via_urlopen(
                    api_key='synthetic', base_url=url, payload={}, timeout_seconds=.1,
                    http_error_prefix='Audit', connection_error_prefix='Audit connection')
            else:
                kwargs = dict(config=SimpleNamespace(api_timeout_seconds=.1), base_url=url,
                              api_key='synthetic', payload={}, ssl_context=ssl.create_default_context(),
                              http_error_cls=UpstreamError)
                if kind == 'cleaner-sync':
                    cleaner_api.post_api_chat_completions_via_urlopen(**kwargs)
                else:
                    asyncio.run(cleaner_api.post_api_chat_completions_with_retry_async(**kwargs, max_tokens=20))
