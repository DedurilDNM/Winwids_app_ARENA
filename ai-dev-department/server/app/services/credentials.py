"""Login credentials of a service account for auto-login.

The password is stored Fernet-encrypted and only decrypted right before it is
typed into the account's own browser. Never log or return it."""

from __future__ import annotations

import re

from app.security import decrypt_bytes

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def login_email(account) -> str | None:
    """The account's login e-mail: the explicit field, else the first address in its note."""
    explicit = (getattr(account, "login_email", None) or "").strip()
    if explicit:
        return explicit
    m = _EMAIL.search(getattr(account, "service_note", "") or "")
    return m.group(0) if m else None


def login_credentials(account) -> tuple[str, str] | None:
    """(email, password) or None when auto-login is not configured."""
    blob = getattr(account, "login_password_encrypted", None)
    email = login_email(account)
    if not blob or not email:
        return None
    try:
        return email, decrypt_bytes(blob).decode()
    except Exception:
        return None
