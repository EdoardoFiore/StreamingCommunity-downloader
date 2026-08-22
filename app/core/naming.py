"""Configurable file and folder names.

The layout used to be fixed. Anyone whose library follows a different convention
had to rename by hand in the file manager after every download, which also broke
the request system's library check — it looks for the name the downloader would
have written.

Two constructs, both deliberately small:

**Named tokens**, from a closed set: ``{title}``, ``{year}``, ``{season}``,
``{season2}``, ``{episode}``, ``{episode2}``. The zero-padded variants are names
of their own rather than a format-spec mini-language, which makes "unknown
token" a clean error instead of a mystery.

**Conditional groups**: ``[ ({year})]`` renders only when every token inside
resolves to something, and otherwise disappears whole — brackets, parentheses,
leading space and all. That is how "omit the year when there isn't one" becomes
a property of the template rather than a special case in the code, and it is the
convention Sonarr and Jellyfin users already know.

``str.format`` is never used on any of this. ``{title.__class__}`` leaks
attributes, ``{0}`` indexes, ``{x:{y}}`` nests, and a stray ``{`` raises
ValueError — which, on this code path, means raising in the middle of a
download after the bytes have already been fetched.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Every slot the layout is made of, with the tokens that mean anything in it.
# A film has no season, so offering {season} there would only produce templates
# that render empty.
_COMMON = ("title", "year")
_EPISODE = _COMMON + ("season", "season2", "episode", "episode2")

SLOT_TOKENS: dict[str, tuple[str, ...]] = {
    "film_folder": _COMMON,
    "film_file": _COMMON,
    "series_folder": _COMMON,
    "season_folder": _COMMON + ("season", "season2"),
    "episode_file": _EPISODE,
    "anime_folder": _COMMON,
    "anime_season_folder": _COMMON + ("season", "season2"),
    "anime_episode_file": _COMMON + ("episode", "episode2"),
    "anime_movie_file": _COMMON,
}

# The layout as it has always been. Also the fallback for any slot whose
# template renders to nothing, and — as LEGACY_TEMPLATES — what the library
# check falls back to so files written before a change still count as present.
DEFAULT_TEMPLATES: dict[str, str] = {
    "film_folder": "{title}[ ({year})]",
    "film_file": "{title}[ ({year})]",
    "series_folder": "{title}[ ({year})]",
    "season_folder": "Season {season2}",
    "episode_file": "{title} S{season2}E{episode2}",
    "anime_folder": "{title}[ ({year})]",
    "anime_season_folder": "Season 01",
    "anime_episode_file": "{title} S01E{episode2}",
    "anime_movie_file": "{title}",
}

# Frozen: DEFAULT_TEMPLATES is handed out as a dict and a caller could mutate it.
LEGACY_TEMPLATES: dict[str, str] = dict(DEFAULT_TEMPLATES)

_TOKEN_RE = re.compile(r"\{([a-z0-9_]+)\}")
_GROUP_RE = re.compile(r"\[([^\[\]]*)\]")

# Characters that cannot appear in a name on some platform we support, plus the
# path separators. Same set as headers.sanitize_filename, minus its 180-char cap
# — the cap belongs on the *title*, which the caller has already truncated, and
# applying it again here would silently drop the year off a very long title.
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def templates() -> dict[str, str]:
    """The configured templates, with every missing slot defaulted.

    Merged per key rather than taken wholesale: get_settings() does a shallow
    merge, so a stored naming_templates holding three of the nine slots would
    otherwise replace the whole default dict and leave six of them missing.
    """
    from app.config import get_settings

    stored = get_settings().get("naming_templates") or {}
    if not isinstance(stored, dict):
        logger.warning("naming_templates is not an object; using the defaults")
        return dict(DEFAULT_TEMPLATES)
    return {**DEFAULT_TEMPLATES, **{k: v for k, v in stored.items() if isinstance(v, str)}}


def _scrub(name: str) -> str:
    """Strip what cannot be in a filename, without truncating."""
    cleaned = _FORBIDDEN.sub("", name).strip().strip(".")
    return cleaned


def render(slot: str, template: str | None, values: dict) -> str:
    """Fill a template in. Never raises.

    This runs inside a download, so every failure mode has to have an answer
    that is a string: an unknown token renders empty and logs, a template that
    ends up empty falls back to the slot's built-in default, and a template that
    is still empty after that falls back to the title.
    """
    text = template if template is not None else DEFAULT_TEMPLATES.get(slot, "{title}")
    rendered = _render_raw(text, values)

    if not rendered and text != DEFAULT_TEMPLATES.get(slot):
        logger.warning("Naming template for %r rendered empty; using the default", slot)
        rendered = _render_raw(DEFAULT_TEMPLATES.get(slot, "{title}"), values)

    # A folder slot rendering empty would make the output directory the library
    # root itself, which m3u8.py's empty-directory cleanup then points at.
    return rendered or _scrub(str(values.get("title") or "")) or "senza-nome"


def _render_raw(template: str, values: dict) -> str:
    def resolve_group(match: re.Match) -> str:
        inner = match.group(1)
        tokens = _TOKEN_RE.findall(inner)
        if tokens and not all(str(values.get(t) or "") for t in tokens):
            return ""
        return inner

    text = _GROUP_RE.sub(resolve_group, template)

    def resolve_token(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            logger.warning("Unknown naming token %r; rendering it empty", name)
            return ""
        return str(values.get(name) or "")

    return _scrub(_TOKEN_RE.sub(resolve_token, text))


# ── Validation ────────────────────────────────────────────────────────────────

# Values used to prove a template produces something usable before it is saved.
_PROBE = {
    "title": "Titolo",
    "year": "2024",
    "season": "1",
    "season2": "01",
    "episode": "7",
    "episode2": "07",
}


def validate(slot: str, template: str) -> str:
    """Check a template and return it, or raise ValueError explaining why not.

    The point of doing this at save time is that render() cannot afford to
    complain: it runs mid-download. Everything that would be silently mangled or
    quietly defaulted there is refused here, while there is still a person
    looking at it.
    """
    if slot not in SLOT_TOKENS:
        raise ValueError(f"Sezione sconosciuta: {slot}")

    text = (template or "").strip()
    if not text:
        raise ValueError("Il modello non può essere vuoto")

    if "/" in text or "\\" in text:
        raise ValueError("Il modello non può contenere / o \\: è un solo nome, non un percorso")
    if ".." in text:
        raise ValueError("Il modello non può contenere ..")
    if text.startswith(".") or text.endswith("."):
        raise ValueError("Il modello non può iniziare o finire con un punto")

    if text.count("[") != text.count("]"):
        raise ValueError("Parentesi quadre non bilanciate")
    if re.search(r"\[[^\]]*\[", text):
        raise ValueError("I gruppi opzionali non possono essere annidati")

    allowed = SLOT_TOKENS[slot]
    unknown = [t for t in _TOKEN_RE.findall(text) if t not in allowed]
    if unknown:
        raise ValueError(
            f"Segnaposto non validi qui: {', '.join('{' + u + '}' for u in unknown)}. "
            f"Disponibili: {', '.join('{' + a + '}' for a in allowed)}"
        )

    rendered = _render_raw(text, _PROBE)
    if not rendered:
        raise ValueError("Con valori di esempio il modello produce un nome vuoto")
    if rendered != _scrub(rendered):
        raise ValueError("Il modello contiene caratteri non ammessi in un nome di file")

    return text


def preview(slot: str, template: str, values: dict | None = None) -> str:
    """What a template would produce, for the settings UI."""
    return _render_raw(template or "", {**_PROBE, **(values or {})})
