"""Generate an Argon2id admin password hash without storing the password.

Usage:
    python scripts/generate_admin_password_hash.py

Copy only the printed hash into ADMIN_PASSWORD_HASH in the deployment secret
store. The plaintext is read with echo disabled and is never written to disk.
"""

from getpass import getpass

from argon2 import PasswordHasher


def main() -> None:
    password = getpass("Admin password (12+ characters): ")
    confirmation = getpass("Repeat admin password: ")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters.")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    print(PasswordHasher().hash(password))


if __name__ == "__main__":
    main()
