"""Store credentials in the system keyring, with environment overrides."""

import os

import keyring

SERVICE_NAME = "aidebug"
KEY_NAME = "OPENAI_API_KEY"


class CredentialError(RuntimeError):
    """A credential operation failed; messages never include backend details."""


def save_api_key(api_key: str) -> None:
    if not api_key.strip():
        raise CredentialError("API key cannot be empty.")
    try:
        keyring.set_password(SERVICE_NAME, KEY_NAME, api_key)
    except Exception:
        raise CredentialError("Could not save the API key to the system keyring.") from None


def resolve_api_key() -> str | None:
    """Prefer OPENAI_API_KEY over the credential saved by aidebug configure."""
    api_key = os.environ.get(KEY_NAME)
    if api_key:
        return api_key
    try:
        return keyring.get_password(SERVICE_NAME, KEY_NAME)
    except Exception:
        raise CredentialError(
            "Could not read the system keyring. Set OPENAI_API_KEY or use --no-ai."
        ) from None
