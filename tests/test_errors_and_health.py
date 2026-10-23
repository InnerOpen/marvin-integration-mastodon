"""Auth failure and the other error codes, configuration problems, the health check, delete_status and registration."""

import pytest
from fakes import SERVER, FakeMastodon, make_ctx, reply
from marvin_integration_sdk import INTEGRATION_REGISTRY, get_provider, resolve_policy

import marvin_integration_mastodon
from marvin_integration_mastodon import MastodonProvider
from marvin_integration_mastodon.provider import CAPABILITY, MastodonError


def run(key, args, http, **ctx_kwargs):
    return MastodonProvider().run_action(key, args, make_ctx(http, **ctx_kwargs))


def failing_status(status, body=None, headers=None):
    return FakeMastodon(status_reply=reply(status, body if body is not None else {"error": "boom"}, headers))


# ---- failures from Mastodon ----------------------------------------------------------------------------------------


def test_a_rejected_token_is_an_auth_error_that_says_how_to_fix_it():
    http = failing_status(401, {"error": "The access token is invalid"})
    with pytest.raises(MastodonError) as caught:
        run("post_status", {"text": "hi"}, http)
    assert caught.value.code == "auth"
    assert "The access token is invalid" in str(caught.value) and "Preferences" in str(caught.value)
    assert "fake-token" not in str(caught.value)


def test_a_403_names_the_scopes():
    with pytest.raises(MastodonError, match="write:statuses") as caught:
        run("post_status", {"text": "hi"}, failing_status(403, {"error": "This action is outside the authorized scopes"}))
    assert caught.value.code == "auth"


def test_a_422_is_a_refusal_with_mastodons_reason():
    with pytest.raises(MastodonError, match="Validation failed") as caught:
        run("post_status", {"text": "hi"}, failing_status(422, {"error": "Validation failed: Text character limit of 500 exceeded"}))
    assert caught.value.code == "refused"


def test_a_429_carries_the_retry_after_hint():
    with pytest.raises(MastodonError) as caught:
        run("post_status", {"text": "hi"}, failing_status(429, {"error": "Too many requests"}, {"Retry-After": "120"}))
    assert caught.value.code == "rate_limited" and caught.value.retry_after == 120


def test_a_429_without_a_hint_has_no_retry_after():
    with pytest.raises(MastodonError) as caught:
        run("post_status", {"text": "hi"}, failing_status(429))
    assert caught.value.retry_after is None


def test_a_5xx_is_unavailable_and_a_non_json_body_is_still_readable():
    with pytest.raises(MastodonError, match="Bad Gateway") as caught:
        run("post_status", {"text": "hi"}, failing_status(502, b"<html>Bad Gateway</html>"))
    assert caught.value.code == "unavailable"


def test_a_network_failure_is_unavailable():
    http = FakeMastodon(raises={"POST /api/v1/statuses": TimeoutError("read timed out")})
    with pytest.raises(MastodonError, match="read timed out") as caught:
        run("post_status", {"text": "hi"}, http)
    assert caught.value.code == "unavailable"


def test_every_failure_is_an_integration_error_so_older_cores_fail_the_step_with_the_message():
    with pytest.raises(ValueError, match="boom"):
        run("post_status", {"text": "hi"}, failing_status(500))


def test_the_error_policy_retries_transient_codes_and_lets_the_rest_fail():
    provider = MastodonProvider()
    assert resolve_policy(provider, "post_status", "unavailable").retry is not None
    assert resolve_policy(provider, "post_status", "rate_limited").retry is not None
    assert resolve_policy(provider, "post_status", "auth").notify is True
    refusal = resolve_policy(provider, "post_status", "refused")
    assert refusal.retry is None and not refusal.review


# ---- configuration -------------------------------------------------------------------------------------------------


def test_no_token_is_refused_before_any_request():
    http = FakeMastodon()
    with pytest.raises(MastodonError) as caught:
        run("post_status", {"text": "hi"}, http, secret=None)
    assert caught.value.code == "auth" and http.requests == []


@pytest.mark.parametrize(
    "server", ["", "http://mastodon.example", "https://mastodon.example/some/path", "https://user:pw@mastodon.example", "ftp://x"]
)
def test_an_unusable_server_is_a_config_error(server):
    http = FakeMastodon()
    with pytest.raises(MastodonError) as caught:
        MastodonProvider().run_action("post_status", {"text": "hi"}, make_ctx(http, config={"server": server}))
    assert caught.value.code == "config" and http.requests == []


@pytest.mark.parametrize("server", ["mastodon.example", "https://Mastodon.Example/", "  https://mastodon.example  "])
def test_a_bare_host_or_trailing_slash_is_normalised(server):
    http = FakeMastodon()
    MastodonProvider().run_action("post_status", {"text": "hi"}, make_ctx(http, config={"server": server}))
    assert http.status_posts[0].url == f"{SERVER}/api/v1/statuses"


def test_unknown_actions_are_not_implemented():
    with pytest.raises(NotImplementedError):
        run("tweet", {}, FakeMastodon())


# ---- health check --------------------------------------------------------------------------------------------------


def check(http, **kwargs):
    return MastodonProvider().check(make_ctx(http, **kwargs))


def test_health_check_calls_verify_credentials_with_the_token():
    http = FakeMastodon()
    assert check(http) == ("ok", None)
    [req] = http.requests
    assert req.url == f"{SERVER}/api/v1/accounts/verify_credentials" and req.headers["Authorization"] == "Bearer fake-token"


def test_health_check_reports_a_rejected_token():
    http = FakeMastodon()
    http.verify = reply(401, {"error": "The access token is invalid"})
    status, error = check(http)
    assert status == "error" and "The access token is invalid" in error and "fake-token" not in error


def test_a_write_only_token_is_ok_because_a_403_proves_it_authenticated():
    http = FakeMastodon()
    http.verify = reply(403, {"error": "This action is outside the authorized scopes"})
    assert check(http) == ("ok", None)


def test_health_check_reports_a_server_error_and_an_unreachable_server():
    http = FakeMastodon()
    http.verify = reply(503, {"error": "down"})
    assert check(http)[0] == "error"
    assert check(FakeMastodon(raises={"GET /api/v1/accounts": OSError("no route")}))[0] == "error"


def test_health_check_needs_a_token_and_a_server():
    assert check(FakeMastodon(), secret=None) == ("unconfigured", "Missing access token.")
    assert check(FakeMastodon(), config={"server": ""}) == ("unconfigured", "Missing server.")


def test_health_check_flags_an_insecure_server():
    status, error = check(FakeMastodon(), config={"server": "http://mastodon.example"})
    assert status == "error" and "https" in error


# ---- delete_status -------------------------------------------------------------------------------------------------


def test_delete_status_deletes_by_id():
    http = FakeMastodon()
    assert run("delete_status", {"id": "110001"}, http) == {"deleted": True, "id": "110001", "network": "mastodon"}
    [req] = http.requests
    assert (req.method, req.url) == ("DELETE", f"{SERVER}/api/v1/statuses/110001")


@pytest.mark.parametrize("bad", ["", "../accounts/verify_credentials", "1/2", "1?x=y", None])
def test_delete_status_refuses_an_id_that_is_not_an_id(bad):
    http = FakeMastodon()
    with pytest.raises(MastodonError):
        run("delete_status", {"id": bad}, http)
    assert http.requests == []


def test_delete_status_surfaces_a_missing_status():
    http = FakeMastodon()
    http.delete = lambda url, **kw: reply(404, {"error": "Record not found"})
    with pytest.raises(MastodonError, match="Record not found"):
        run("delete_status", {"id": "1"}, http)


# ---- what the plugin declares ----------------------------------------------------------------------------------------


def test_post_status_declares_the_social_post_contract():
    action = MastodonProvider().get_action("post_status")
    assert action.capability == CAPABILITY == "social.post"
    assert action.requires_approval is True
    props = action.input_schema["properties"]
    assert set(props) == {"text", "images", "alt", "link", "dry_run"}
    assert action.input_schema["required"] == ["text"]
    assert props["images"]["maxItems"] == 10
    assert set(action.output_schema["properties"]) >= {"posted", "post_id", "url", "network", "would_post"}


def test_delete_status_is_approval_gated_and_declares_no_capability():
    action = MastodonProvider().get_action("delete_status")
    assert action.requires_approval is True and action.capability is None


def test_the_plugin_imports_registers_and_has_its_version():
    assert marvin_integration_mastodon.__version__ == "0.3.1"
    assert isinstance(INTEGRATION_REGISTRY["mastodon"], MastodonProvider)
    assert get_provider("mastodon").name == "Mastodon"


def test_the_token_is_declared_as_a_credential_not_config():
    provider = MastodonProvider()
    assert [c.key for c in provider.credentials] == ["access_token"]
    assert set(provider.config_schema["properties"]) == {"server", "visibility", "language"}
    assert provider.config_schema["required"] == ["server"]
    assert provider.config_schema["properties"]["visibility"]["default"] == "public"


def test_the_entry_point_resolves_to_the_provider():
    from importlib.metadata import entry_points

    found = [e for e in entry_points(group="marvin.integrations") if e.name == "mastodon"]
    assert found and found[0].load() is MastodonProvider


def test_a_rate_limit_reset_time_also_becomes_a_retry_after():
    from datetime import UTC, datetime, timedelta

    reset = (datetime.now(UTC) + timedelta(seconds=90)).isoformat().replace("+00:00", "Z")
    with pytest.raises(MastodonError) as caught:
        run("post_status", {"text": "hi"}, failing_status(429, {"error": "Too many requests"}, {"X-RateLimit-Reset": reset}))
    assert 80 <= caught.value.retry_after <= 90
