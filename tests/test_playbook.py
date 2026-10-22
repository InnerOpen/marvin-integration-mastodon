"""The Playbook this integration ships (0.3.0, SDK 0.15.0): well formed, packaged, and true to how Mastodon counts."""

from importlib import resources

from marvin_integration_sdk import check_playbook

from marvin_integration_mastodon import MastodonProvider
from marvin_integration_mastodon.text import URL_PLACEHOLDER


def _playbook():
    (blueprint,) = [c for c in MastodonProvider.content if c.kind == "playbook"]
    return blueprint


def test_the_provider_ships_the_mastodon_post_playbook():
    blueprint = _playbook()
    assert (blueprint.slug, blueprint.required) == ("mastodon-post", False)
    assert list(blueprint.payload["files"]) == ["references/examples.md"]
    assert check_playbook(blueprint, MastodonProvider()) == []


def test_it_tells_the_agent_to_write_the_text_itself_and_not_to_post():
    body = _playbook().payload["skill_md"]
    assert "`create_draft`" in body and "`update_draft`" in body
    assert "`compose_entry`" in body and "`revise_entry`" in body  # named as the ones not to use
    assert "Nothing you write is posted by you" in body and "Approval step" in body


def test_it_gives_the_length_the_way_mastodon_counts_it():
    body = _playbook().payload["skill_md"]
    assert "500 characters" in body and f"{URL_PLACEHOLDER} characters" in body
    assert "CamelCase" in body


def test_it_does_not_promise_a_content_warning_the_connection_cannot_set():
    (post,) = [a for a in MastodonProvider.actions if a.key == "post_status"]
    assert not {"spoiler_text", "content_warning"} & set(post.input_schema["properties"])
    assert "can't set one" in _playbook().payload["skill_md"]


def test_the_playbook_files_are_package_data():
    root = resources.files("marvin_integration_mastodon").joinpath("playbooks", "mastodon-post")
    assert root.joinpath("SKILL.md").is_file() and root.joinpath("references", "examples.md").is_file()
