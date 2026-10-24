"""Tests for loading the deployed secret into the environment.

Deployed, a function is given only APP_SECRETS_ARN; the database URL and the JWT
key reach Settings through load_deployed_secrets. Three properties matter: it
does nothing at all without the ARN (so tests and local runs never touch AWS),
it runs before Settings is built, and a broken secret stops startup without
ever printing a value.
"""

from __future__ import annotations

from typing import Any

import pytest

from cbpupsis_core import config
from cbpupsis_core.config import APP_SECRETS_ARN, load_deployed_secrets

ARN = (
    "arn:aws:secretsmanager:ap-southeast-1:123456789012"
    ":secret:cbpupsis/staging/app-AbCdEf"
)


class FakeSecretsClient:
    """Answers get_secret_value with fixed text and records what was asked for."""

    def __init__(self, secret_string: str) -> None:
        self.secret_string = secret_string
        self.requested: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        self.requested.append(SecretId)
        return {"SecretString": self.secret_string}


class UnreachableClient:
    """Fails the test if the loader calls AWS when it should not."""

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        raise AssertionError(
            "The loader called Secrets Manager without APP_SECRETS_ARN."
        )


class TestWithoutTheArn:
    def test_does_nothing_locally(self) -> None:
        """No ARN is how tests and local runs stay free of AWS credentials."""
        environ: dict[str, str] = {"DATABASE_URL": "postgresql://local"}

        assert load_deployed_secrets(environ, UnreachableClient()) == []
        assert environ == {"DATABASE_URL": "postgresql://local"}

    def test_blank_arn_counts_as_unset(self) -> None:
        environ = {APP_SECRETS_ARN: ""}

        assert load_deployed_secrets(environ, UnreachableClient()) == []


class TestWithTheArn:
    def test_copies_every_key_into_the_environment(self) -> None:
        client = FakeSecretsClient(
            '{"DATABASE_URL": "postgresql://deployed", "JWT_SECRET": "k"}'
        )
        environ = {APP_SECRETS_ARN: ARN}

        loaded = load_deployed_secrets(environ, client)

        assert client.requested == [ARN]
        assert environ["DATABASE_URL"] == "postgresql://deployed"
        assert environ["JWT_SECRET"] == "k"
        assert sorted(loaded) == ["DATABASE_URL", "JWT_SECRET"]

    def test_returns_names_never_values(self) -> None:
        """The return value may be logged, so it must not carry a secret."""
        client = FakeSecretsClient('{"JWT_SECRET": "do-not-log-me"}')

        loaded = load_deployed_secrets({APP_SECRETS_ARN: ARN}, client)

        assert "do-not-log-me" not in repr(loaded)

    def test_an_explicit_variable_wins(self) -> None:
        client = FakeSecretsClient('{"LOG_LEVEL": "DEBUG", "JWT_SECRET": "k"}')
        environ = {APP_SECRETS_ARN: ARN, "LOG_LEVEL": "ERROR"}

        loaded = load_deployed_secrets(environ, client)

        assert environ["LOG_LEVEL"] == "ERROR"
        assert loaded == ["JWT_SECRET"]


class TestABrokenSecretStopsStartup:
    def test_text_that_is_not_json(self) -> None:
        client = FakeSecretsClient("postgresql://user:hunter2@host/db")

        with pytest.raises(ValueError, match="not JSON") as raised:
            load_deployed_secrets({APP_SECRETS_ARN: ARN}, client)

        assert "hunter2" not in str(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__

    @pytest.mark.parametrize(
        "secret_string",
        [
            '["DATABASE_URL"]',
            '"just a string"',
            '{"DB_POOL_SIZE": 5}',
            '{"NESTED": {"a": "b"}}',
        ],
    )
    def test_anything_but_an_object_of_strings(self, secret_string: str) -> None:
        with pytest.raises(ValueError, match="JSON object of string values"):
            load_deployed_secrets(
                {APP_SECRETS_ARN: ARN}, FakeSecretsClient(secret_string)
            )

    def test_an_aws_error_is_not_swallowed(self) -> None:
        """Running without the secret would only fail later, less clearly."""

        class FailingClient:
            def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
                raise RuntimeError("AccessDeniedException")

        with pytest.raises(RuntimeError, match="AccessDenied"):
            load_deployed_secrets({APP_SECRETS_ARN: ARN}, FailingClient())


class TestOrder:
    def test_settings_are_built_after_the_secret_is_loaded(self, monkeypatch) -> None:
        """Whatever the loader puts in the environment, Settings must see."""

        def fake_loader() -> list[str]:
            monkeypatch.setenv("APP_NAME", "Loaded From The Secret")
            return ["APP_NAME"]

        monkeypatch.setattr(config, "load_deployed_secrets", fake_loader)

        built = config.get_settings.__wrapped__()

        assert built.app_name == "Loaded From The Secret"
