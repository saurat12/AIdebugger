from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug import cli, credentials
from aidebug.openai_agent import OpenAIAgent


def test_environment_takes_precedence(monkeypatch):
    lookup = Mock(side_effect=AssertionError("Must not access keyring"))
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setattr(credentials.keyring, "get_password", lookup)
    assert credentials.resolve_api_key() == "environment-key"
    lookup.assert_not_called()


@pytest.mark.parametrize("environment", [None, ""])
def test_keyring_fallback(monkeypatch, environment):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if environment is not None:
        monkeypatch.setenv("OPENAI_API_KEY", environment)
    lookup = Mock(return_value="stored-key")
    monkeypatch.setattr(credentials.keyring, "get_password", lookup)
    assert credentials.resolve_api_key() == "stored-key"
    lookup.assert_called_once_with(credentials.SERVICE_NAME, credentials.KEY_NAME)


def test_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert credentials.resolve_api_key() is None


def test_configure_from_unrelated_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["aidebug", "configure"])
    prompt = Mock(return_value="secret-key")
    save = Mock()
    monkeypatch.setattr(cli.getpass, "getpass", prompt)
    monkeypatch.setattr(credentials.keyring, "set_password", save)
    monkeypatch.setattr(cli, "discover_repository", Mock(side_effect=AssertionError("No discovery")))
    assert cli.main() == 0
    prompt.assert_called_once()
    save.assert_called_once_with(credentials.SERVICE_NAME, credentials.KEY_NAME, "secret-key")
    captured = capsys.readouterr()
    assert captured.out == "Configuration saved.\n"
    assert captured.err == ""
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_key_is_not_saved(monkeypatch, value):
    save = Mock()
    monkeypatch.setattr(credentials.keyring, "set_password", save)
    with pytest.raises(credentials.CredentialError, match="empty"):
        credentials.save_api_key(value)
    save.assert_not_called()


def test_configuration_failure_does_not_leak_secret(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["aidebug", "configure"])
    monkeypatch.setattr(cli.getpass, "getpass", Mock(return_value="secret-key"))
    monkeypatch.setattr(credentials.keyring, "set_password", Mock(side_effect=RuntimeError("secret-key")))
    with pytest.raises(SystemExit) as error:
        cli.main()
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "secret-key" not in captured.out + captured.err
    assert "Configuration saved." not in captured.out


def test_keyring_read_failure_is_sanitized(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(credentials.keyring, "get_password", Mock(side_effect=RuntimeError("secret-key")))
    with pytest.raises(credentials.CredentialError) as error:
        credentials.resolve_api_key()
    assert "secret-key" not in str(error.value)


@pytest.mark.parametrize("failure", [EOFError(), KeyboardInterrupt(), cli.getpass.GetPassWarning("unsafe")])
def test_configure_cancelled(monkeypatch, failure, capsys):
    monkeypatch.setattr("sys.argv", ["aidebug", "configure"])
    monkeypatch.setattr(cli.getpass, "getpass", Mock(side_effect=failure))
    save = Mock()
    monkeypatch.setattr(credentials.keyring, "set_password", save)
    with pytest.raises(SystemExit):
        cli.main()
    save.assert_not_called()
    assert "Configuration saved." not in capsys.readouterr().out


def test_agent_loads_saved_key(tmp_path, monkeypatch):
    import openai

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(credentials.keyring, "get_password", Mock(return_value="stored-key"))
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="done")
    factory = Mock(return_value=client)
    monkeypatch.setattr(openai, "OpenAI", factory)
    assert OpenAIAgent()._complete("system", "evidence", tmp_path) == "done"
    factory.assert_called_once_with(api_key="stored-key")
