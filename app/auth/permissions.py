"""Panel permissions, stored as a bitwise integer on ``jf_user.permissions``.

The flags are deliberately **independent**: there is no ADMIN bit that implies
the others. Seerr short-circuits every check when a user has ADMIN, which would
make it impossible to have an administrator who manages settings and users but
never sees the request queue — which is a requirement here.
"""

from enum import IntFlag


class Permission(IntFlag):
    NONE = 0
    DOWNLOAD = 1 << 0          # start a download directly, bypassing the request queue
    REQUEST = 1 << 1           # create requests
    MANAGE_REQUESTS = 1 << 2   # see the queue, approve, deny, fix
    MANAGE_USERS = 1 << 3      # import Jellyfin users, assign permissions, enable/disable
    MANAGE_SETTINGS = 1 << 4   # source domain, libraries, performance, Jellyfin connection
    MANAGE_FILES = 1 << 5      # move, rename, delete in the file manager
    VIEW_LIBRARY = 1 << 6      # browse and stream the library


ALL_PERMISSIONS = (
    Permission.DOWNLOAD
    | Permission.REQUEST
    | Permission.MANAGE_REQUESTS
    | Permission.MANAGE_USERS
    | Permission.MANAGE_SETTINGS
    | Permission.MANAGE_FILES
    | Permission.VIEW_LIBRARY
)

# What a freshly imported user gets when no explicit set is given: they can ask
# for content and browse what is already there, nothing else.
DEFAULT_PERMISSIONS = Permission.REQUEST | Permission.VIEW_LIBRARY


def has(value: int, *required: Permission, mode: str = "and") -> bool:
    """Bitwise check of ``required`` against a stored permissions integer."""
    if not required:
        return True
    checks = [bool(value & int(p)) for p in required]
    return all(checks) if mode == "and" else any(checks)


def to_names(value: int) -> list[str]:
    """Permission names held by ``value``, for the API and the frontend."""
    return [p.name for p in Permission if p is not Permission.NONE and value & int(p)]


def from_names(names: list[str]) -> int:
    """Build a permissions integer from names, rejecting anything unknown."""
    total = 0
    for name in names:
        try:
            total |= int(Permission[name])
        except KeyError:
            raise ValueError(f"Unknown permission: {name!r}")
    return total


def sanitize(value: int) -> int:
    """Drop any bit that does not map to a defined permission."""
    return int(value) & int(ALL_PERMISSIONS)
