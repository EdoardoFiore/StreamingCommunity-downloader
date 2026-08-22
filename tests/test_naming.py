"""The naming template engine.

The anchor test is the first one: the defaults must render byte-for-byte what
the panel wrote before templates existed. Everything else in this feature rests
on that — the library check falls back to the legacy layout to keep recognising
files already on disk, and if "legacy" and "default" ever drift apart, that
fallback is checking a path nothing ever wrote.

The rest is mostly about render() never raising. It runs inside a download,
after the bytes have been fetched, so there is no failure mode there that can be
allowed to be an exception.
"""

import pytest

from app.core import naming


# ── The anchor ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "slot, values, expected",
    [
        ("film_folder", {"title": "Blade Runner", "year": "1982"}, "Blade Runner (1982)"),
        ("film_file", {"title": "Blade Runner", "year": "1982"}, "Blade Runner (1982)"),
        ("film_folder", {"title": "Blade Runner", "year": None}, "Blade Runner"),
        ("series_folder", {"title": "Dark", "year": "2017"}, "Dark (2017)"),
        ("season_folder", {"season": "1", "season2": "01"}, "Season 01"),
        ("season_folder", {"season": "12", "season2": "12"}, "Season 12"),
        ("episode_file",
         {"title": "Dark", "season2": "01", "episode2": "07"}, "Dark S01E07"),
        ("episode_file",
         {"title": "Dark", "season2": "02", "episode2": "07.5"}, "Dark S02E07.5"),
        ("anime_folder", {"title": "Naruto", "year": "2002"}, "Naruto (2002)"),
        ("anime_season_folder", {}, "Season 01"),
        ("anime_episode_file", {"title": "Naruto", "episode2": "12"}, "Naruto S01E12"),
        ("anime_movie_file", {"title": "Akira", "year": "1988"}, "Akira"),
    ],
)
def test_the_defaults_render_the_layout_the_panel_already_had(slot, values, expected):
    assert naming.render(slot, naming.DEFAULT_TEMPLATES[slot], values) == expected


def test_legacy_and_default_are_the_same_thing():
    """They will diverge the day someone edits one; the library check needs both."""
    assert naming.LEGACY_TEMPLATES == naming.DEFAULT_TEMPLATES


def test_legacy_templates_cannot_be_mutated_through_the_default_dict():
    naming.DEFAULT_TEMPLATES["film_file"] = "broken"
    try:
        assert naming.LEGACY_TEMPLATES["film_file"] == "{title}[ ({year})]"
    finally:
        naming.DEFAULT_TEMPLATES["film_file"] = "{title}[ ({year})]"


# ── Conditional groups ────────────────────────────────────────────────────────

@pytest.mark.parametrize("year", [None, "", 0])
def test_a_group_disappears_whole_when_its_token_is_empty(year):
    """Brackets, parentheses and the leading space all go, not just the value."""
    assert naming.render("film_folder", "{title}[ ({year})]",
                         {"title": "Akira", "year": year}) == "Akira"


def test_a_group_survives_when_every_token_resolves():
    assert naming.render("film_folder", "{title}[ ({year})]",
                         {"title": "Akira", "year": "1988"}) == "Akira (1988)"


def test_a_group_needing_two_tokens_needs_both():
    values = {"title": "X", "season2": "01", "episode2": ""}
    assert naming.render("episode_file", "{title}[ S{season2}E{episode2}]", values) == "X"


def test_literal_text_in_a_group_with_no_tokens_is_kept():
    assert naming.render("film_file", "{title}[ - Film]", {"title": "X"}) == "X - Film"


# ── render never raises ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "template",
    ["{title} {", "}{title}", "{title} %s", "{title} {0}", "{title} {nonesuch}",
     "{title} {title.__class__}", "{{title}}"],
)
def test_render_survives_anything_a_template_can_contain(template):
    """Every one of these is a ValueError or an information leak under str.format."""
    result = naming.render("film_file", template, {"title": "Akira", "year": "1988"})
    assert isinstance(result, str) and result


def test_a_title_containing_a_token_is_not_re_expanded():
    result = naming.render("film_file", "{title}", {"title": "{year} the movie", "year": "1999"})
    assert result == "{year} the movie"


def test_a_template_rendering_empty_falls_back_to_the_default():
    assert naming.render("film_file", "[({year})]", {"title": "Akira", "year": None}) == "Akira"


def test_a_slot_with_no_title_at_all_still_produces_a_name():
    assert naming.render("film_file", "{title}", {"title": ""}) == "senza-nome"


def test_path_separators_in_a_value_are_scrubbed():
    """The template is validated on save; a *title* is whatever the source said."""
    result = naming.render("film_file", "{title}", {"title": "../../etc/passwd"})
    assert "/" not in result and ".." not in result


# ── Validation ────────────────────────────────────────────────────────────────

def test_a_good_template_is_accepted():
    assert naming.validate("episode_file", "{title} - S{season2}E{episode2}")


@pytest.mark.parametrize(
    "template, expected",
    [
        ("", "vuoto"),
        ("   ", "vuoto"),
        ("{title}/{year}", "percorso"),
        ("{title}\\{year}", "percorso"),
        ("../{title}", "percorso"),
        (".{title}", "punto"),
        ("{title}.", "punto"),
        ("{title}[ ({year})", "bilanciate"),
        ("{title}[[ ({year})]]", "annidati"),
        ("...", "contenere \\.\\."),
    ],
)
def test_bad_templates_are_refused(template, expected):
    with pytest.raises(ValueError, match=expected):
        naming.validate("episode_file", template)


def test_a_token_that_does_not_exist_is_refused():
    with pytest.raises(ValueError, match="quality"):
        naming.validate("film_file", "{title} {quality}")


def test_a_token_from_another_slot_is_refused():
    """A film has no season; offering it would only produce empty names."""
    with pytest.raises(ValueError, match="season"):
        naming.validate("film_file", "{title} S{season2}")

    assert naming.validate("episode_file", "{title} S{season2}")


def test_the_error_lists_what_is_available():
    with pytest.raises(ValueError, match=r"\{title\}"):
        naming.validate("film_file", "{nope}")


def test_a_template_of_only_forbidden_characters_is_refused():
    """Would survive every structural check and then render to nothing."""
    with pytest.raises(ValueError, match="vuoto"):
        naming.validate("film_file", ":::")


def test_an_unknown_slot_is_refused():
    with pytest.raises(ValueError, match="Sezione sconosciuta"):
        naming.validate("not_a_slot", "{title}")


# ── Stored templates ──────────────────────────────────────────────────────────

def test_a_partial_stored_dict_keeps_the_other_slots(client):
    """get_settings() merges shallowly: three stored slots must not lose six."""
    from app import config

    config.save_settings({
        **config.get_settings(),
        "naming_templates": {"episode_file": "{title} {season2}x{episode2}"},
    })

    resolved = naming.templates()
    assert resolved["episode_file"] == "{title} {season2}x{episode2}"
    assert len(resolved) == len(naming.DEFAULT_TEMPLATES)
    assert resolved["film_folder"] == naming.DEFAULT_TEMPLATES["film_folder"]


def test_nonsense_in_the_settings_file_falls_back_to_the_defaults(client):
    from app import config

    config.save_settings({**config.get_settings(), "naming_templates": "not a dict"})
    assert naming.templates() == naming.DEFAULT_TEMPLATES


def test_a_non_string_template_is_ignored(client):
    from app import config

    config.save_settings({
        **config.get_settings(),
        "naming_templates": {"film_file": 42},
    })
    assert naming.templates()["film_file"] == naming.DEFAULT_TEMPLATES["film_file"]
