"""secrets（keyring → 環境変数 → config.yaml）のテスト。"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from transcribe import secrets
from transcribe.config import NotionConfig, WebConfig


class FakeKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise KeyError(name)
        del self.store[(service, name)]


@pytest.fixture
def fake_kr(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: kr)
    return kr


def test_keyring_takes_precedence(fake_kr, monkeypatch):
    fake_kr.set_password("transcribe", "notion_token", "kr")
    monkeypatch.setenv("NOTION_TOKEN", "env")
    assert NotionConfig(token="cfg").resolve_token() == "kr"
    assert secrets.secret_source("notion_token", "cfg") == "keyring"


def test_env_then_config(fake_kr, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "env")
    assert NotionConfig(token="cfg").resolve_token() == "env"
    monkeypatch.delenv("NOTION_TOKEN")
    assert NotionConfig(token="cfg").resolve_token() == "cfg"
    assert secrets.secret_source("notion_token", "cfg") == "config"
    assert NotionConfig(token="").resolve_token() == ""
    assert secrets.secret_source("notion_token", "") == ""


def test_web_token_from_keyring(fake_kr):
    secrets.set_secret("web_token", "w")
    assert WebConfig(token="").resolve_token() == "w"
    assert secrets.delete_secret("web_token") is True
    assert secrets.delete_secret("web_token") is False


def test_no_keyring_falls_back(monkeypatch):
    # conftest で TRANSCRIBE_NO_KEYRING=1
    monkeypatch.setenv("TRANSCRIBE_WEB_TOKEN", "env")
    assert secrets.get_secret("web_token", "cfg") == "env"
    with pytest.raises(RuntimeError):
        secrets.set_secret("web_token", "x")


def test_keyring_error_is_ignored(monkeypatch):
    class Broken:
        def get_password(self, *a):
            raise RuntimeError("no backend")

    monkeypatch.setattr(secrets, "_keyring", lambda: Broken())
    assert secrets.get_secret("gemini_api_key", "cfg") == "cfg"


def test_unknown_name():
    with pytest.raises(KeyError):
        secrets.get_secret("nope")


def test_cli_secrets_set_and_list(fake_kr):
    from transcribe.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["secrets", "set", "gemini_api_key"], input="abc\nabc\n")
    assert result.exit_code == 0, result.output
    assert fake_kr.get_password("transcribe", "gemini_api_key") == "abc"

    result = runner.invoke(app, ["secrets", "set", "bogus"], input="x\nx\n")
    assert result.exit_code == 1

    result = runner.invoke(app, ["secrets", "delete", "gemini_api_key"])
    assert result.exit_code == 0
    assert fake_kr.get_password("transcribe", "gemini_api_key") is None
