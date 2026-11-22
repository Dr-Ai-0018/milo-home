"""Explicit private credential delivery without environment mutation or fallback."""

import os

from .codex_config import private_read


def load_credential(config, path=None):
    if path is None:
        credential = os.environ.get(config.env_key, "")
    else:
        try:
            if not path.is_absolute() or path.resolve() != path:
                raise ValueError("Noncanonical credential file")
            credential = private_read(path, limit=8193).decode("utf-8")
            if credential.endswith("\n"):
                credential = credential[:-1]
        except (OSError, UnicodeError, ValueError):
            raise ValueError("Invalid private credential file") from None
    if not credential or len(credential) > 8192 or any(c in credential for c in "\r\n\x00"):
        raise ValueError("Missing or malformed provider credential")
    return credential
