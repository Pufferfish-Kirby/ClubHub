"""Capability tokens for POST /events/{id}/confirm.

Confirming publishes an event, so it must not be open to anyone who can guess an id. The upload
response hands the uploader a token for each pending event; only a request that echoes it back may
confirm that event. No accounts: the token is HMAC(secret, event_id), so there is nothing to store
and the checked-in database is untouched.

The secret is `CONFIRM_TOKEN_SECRET` when set. Otherwise a random per-process one: tokens then die
on restart, which is acceptable because the whole review happens in the same upload flow (and
Render's free tier loses runtime-written events on restart anyway).
"""

import hashlib
import hmac
import secrets

from app.config import settings

_process_secret = secrets.token_bytes(32)


def _key() -> bytes:
    return settings.confirm_token_secret.encode() if settings.confirm_token_secret else _process_secret


def make_confirm_token(event_id: str) -> str:
    return hmac.new(_key(), event_id.encode(), hashlib.sha256).hexdigest()


def valid_confirm_token(event_id: str, token: str) -> bool:
    # compare_digest takes the same time wherever the strings differ, so response timing
    # can't be used to guess a token one character at a time.
    return hmac.compare_digest(make_confirm_token(event_id), token)
