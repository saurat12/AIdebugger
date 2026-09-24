import pytest

from aidebug import credentials


@pytest.fixture(autouse=True)
def mock_system_keyring(monkeypatch):
    """Tests must never read or modify the developer's real credentials."""
    monkeypatch.setattr(credentials.keyring, "get_password", lambda *args: None)
    monkeypatch.setattr(credentials.keyring, "set_password", lambda *args: None)


@pytest.fixture
def local_project_python(tmp_path, monkeypatch):
    import os
    from aidebug import discovery

    relative = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    interpreter = tmp_path.joinpath(".venv", *relative)
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("test interpreter", encoding="utf-8")
    monkeypatch.setattr(discovery, "_usable_python", lambda path: path.is_file())
    return str(interpreter.absolute())
