"""SC Panel.

The version is surfaced in the UI and in ``/api/auth/me`` so a bug report can
say which build it came from — the panel is deployed from a moving ``:latest``
tag, and until this existed there was no way to tell what was running.

Deliberately not in the public ``/api/auth/status``: an unauthenticated visitor
has no use for it, and it would tell anyone which build to look up.

Bump it together with the git tag that publishes the image.
"""

__version__ = "2.2.0"
