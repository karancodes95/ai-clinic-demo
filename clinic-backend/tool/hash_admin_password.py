#!/usr/bin/env python3
"""Generate an argon2id hash of the admin password.

Usage (from repo root, with venv activated):
    .venv/bin/python tool/hash_admin_password.py

Reads the password from a hidden prompt (twice, for confirmation), prints
the hash to stdout. Paste the hash into .env as:

    ADMIN_PASSWORD_HASH=$argon2id$v=19$m=65536,t=3,p=4$...

The hash itself is safe to commit (it's a one-way KDF), but .env is
gitignored anyway. Generate ADMIN_JWT_SECRET separately:

    python -c "import secrets; print(secrets.token_hex(32))"
"""

from __future__ import annotations

import getpass
import sys

from argon2 import PasswordHasher


def main() -> int:
    pw1 = getpass.getpass("Admin password: ")
    if not pw1:
        print("aborted: empty password", file=sys.stderr)
        return 1
    pw2 = getpass.getpass("Confirm password: ")
    if pw1 != pw2:
        print("aborted: passwords do not match", file=sys.stderr)
        return 1

    ph = PasswordHasher()
    print(ph.hash(pw1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
