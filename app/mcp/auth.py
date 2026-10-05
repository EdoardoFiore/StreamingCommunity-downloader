"""Who an MCP client is, and whether it may be one.

The panel holds a single MCP token. It is stored as a SHA-256 hash, like a
session token, so it is shown once — when it is generated — and never read
back. Whoever generates it becomes its **owner**, and the agent acts as that
user with their *live* permissions: a token outliving its owner's DOWNLOAD
permission no longer downloads, and one whose owner is disabled or deleted
stops working altogether. There is no identity of its own to grant anything,
which is what keeps "the agent" from becoming a back door around the
permission model — and it is the only honest answer to "who asked for this?"
when a request lands in the queue.

In open mode there are no accounts, so the agent is the same implicit user as
a browser. A token minted in open mode does not survive a switch to Jellyfin:
it has no owner there, and is refused until someone regenerates it.
"""

import hashlib
import hmac
import secrets

from app import db
from app.auth import models
from app.auth.deps import OPEN_MODE_USER

SETTING_TOKEN_HASH = "mcp_token_sha256"
SETTING_TOKEN_OWNER = "mcp_token_owner"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token(owner: models.User) -> str:
    """Replace the token, returning the only copy of the new one."""
    token = secrets.token_urlsafe(32)
    owner_id = "" if owner is OPEN_MODE_USER else str(owner.id)
    with db.tx() as conn:
        models.set_setting(SETTING_TOKEN_HASH, _hash(token), conn=conn)
        models.set_setting(SETTING_TOKEN_OWNER, owner_id, conn=conn)
    return token


def revoke_token() -> None:
    with db.tx() as conn:
        models.set_setting(SETTING_TOKEN_HASH, "", conn=conn)
        models.set_setting(SETTING_TOKEN_OWNER, "", conn=conn)


def has_token() -> bool:
    return bool(models.get_setting(SETTING_TOKEN_HASH))


def token_owner() -> models.User | None:
    """The user the agent acts as, or None when nobody valid holds the token."""
    if models.runtime_open_mode():
        return OPEN_MODE_USER
    owner_id = models.get_setting(SETTING_TOKEN_OWNER) or ""
    if not owner_id.isdigit():
        return None
    user = models.get_user(int(owner_id))
    return user if user is not None and user.enabled else None


def authenticate(presented: str) -> models.User | None:
    """The acting user for a presented token, or None. Fails closed: no stored
    token means no access, never open access."""
    stored = models.get_setting(SETTING_TOKEN_HASH) or ""
    if not stored or not presented:
        return None
    if not hmac.compare_digest(stored, _hash(presented)):
        return None
    return token_owner()
