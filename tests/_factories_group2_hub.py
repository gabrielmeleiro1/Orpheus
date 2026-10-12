"""Temporary Hub environment and imported credential isolation."""


def isolate_hub_environment(monkeypatch, home):
    root = home / "hub"
    for name in (
        "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_OIDC_RESOURCE", "HF_OIDC_ID_TOKEN",
        "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_SCOPES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_TOKEN_PATH", str(home / "token"))
    monkeypatch.setenv("HF_STORED_TOKENS_PATH", str(home / "stored_tokens"))
    monkeypatch.setenv("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    monkeypatch.setenv("HF_HOME", str(home))
    monkeypatch.setenv("HF_HUB_CACHE", str(root))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(root))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


def isolate_hub_credentials(monkeypatch, home):
    from huggingface_hub import constants
    from huggingface_hub.utils import _auth

    # Hub may already be imported, so environment overrides alone are insufficient.
    monkeypatch.setattr(constants, "HF_TOKEN_PATH", str(home / "token"))
    monkeypatch.setattr(constants, "HF_STORED_TOKENS_PATH", str(home / "stored_tokens"))
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_IMPLICIT_TOKEN", True)
    for name in ("OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_SCOPES"):
        if hasattr(constants, name):
            monkeypatch.setattr(constants, name, None)
    for name, value in (
        ("_OIDC_TOKEN_CACHE", None), ("_OAUTH_REFRESH_CACHE", None),
        ("_GOOGLE_COLAB_SECRET", None), ("_IS_GOOGLE_COLAB_CHECKED", True),
    ):
        if hasattr(_auth, name):
            monkeypatch.setattr(_auth, name, value)
