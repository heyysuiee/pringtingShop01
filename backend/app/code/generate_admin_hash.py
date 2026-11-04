from __future__ import annotations

from getpass import getpass

from backend.app.code.security import hash_password


def main() -> None:
    password = getpass("New administrator password (minimum 12 characters): ")
    confirmation = getpass("Confirm administrator password: ")
    if len(password) < 12:
        raise SystemExit("Administrator passwords must be at least 12 characters.")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    print("PRINTSHOP_ADMIN_PASSWORD_HASH=" + hash_password(password))


if __name__ == "__main__":
    main()
