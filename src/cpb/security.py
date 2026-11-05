import hashlib
import hmac
import secrets

from cpb.config import get_settings


def generate_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    secret = get_settings().api_secret.encode()
    return hmac.new(secret, token.encode(), hashlib.sha256).hexdigest()


def token_matches(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), expected_hash)
