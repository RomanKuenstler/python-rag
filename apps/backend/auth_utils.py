import hashlib
import os


def get_global_password_salt() -> str:
    return str(os.getenv("AUTH_PASSWORD_SALT", "xzy132"))


def hash_password_with_salt(password: str, salt: str) -> str:
    plain = str(password or "")
    normalized_salt = str(salt or "")
    return hashlib.sha256(f"{normalized_salt}{plain}".encode("utf-8")).hexdigest()


def hash_password_with_global_salt(password: str) -> str:
    return hash_password_with_salt(password, get_global_password_salt())
