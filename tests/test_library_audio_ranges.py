"""Audio range responses must remain valid HTTP multipart messages."""

from email.parser import BytesParser
from email.policy import default

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from modules.web.routes.library import FileResponse


@pytest.fixture
def audio_client(tmp_path):
    audio = tmp_path / "chapter.mp3"
    audio.write_bytes(b"0123456789")
    app = FastAPI()

    @app.api_route("/audio", methods=["GET", "HEAD"])
    def get_audio():
        return FileResponse(file=audio.open("rb"), path=audio, media_type="audio/mpeg")

    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("ranges", ["bytes=0-1,8-9", "bytes=0-1,4-5,8-9"])
def test_multiple_audio_ranges_have_parseable_parts_and_exact_length(audio_client, ranges):
    response = audio_client.get("/audio", headers={"Range": ranges})

    assert response.status_code == 206
    assert response.headers["content-type"].startswith("multipart/byteranges; boundary=")
    assert "content-range" not in response.headers
    assert int(response.headers["content-length"]) == len(response.content)
    message = BytesParser(policy=default).parsebytes(
        f'Content-Type: {response.headers["content-type"]}\r\n\r\n'.encode("ascii")
        + response.content
    )
    expected = [("bytes 0-1/10", b"01"), ("bytes 8-9/10", b"89")]
    if "4-5" in ranges:
        expected.insert(1, ("bytes 4-5/10", b"45"))
    assert message.is_multipart()
    parts = list(message.iter_parts())
    assert [(part["content-range"], part.get_payload(decode=True)) for part in parts] == expected
    assert all(part.get_content_type() == "audio/mpeg" for part in parts)


def test_multiple_audio_ranges_head_matches_get_headers_without_a_body(audio_client):
    headers = {"Range": "bytes=0-1,4-5,8-9"}
    response = audio_client.head("/audio", headers=headers)
    full_response = audio_client.get("/audio", headers=headers)

    assert response.status_code == 206
    assert response.headers["content-type"].startswith("multipart/byteranges; boundary=")
    assert "content-range" not in response.headers
    assert int(response.headers["content-length"]) == len(full_response.content)
    assert response.content == b""
