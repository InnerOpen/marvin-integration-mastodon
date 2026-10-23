"""The shared engagement capabilities: social.comments (cursor, kinds, HTML), reply (private and public), like, repost,
message, delete_comment, their refusals, dry runs and declarations."""

from urllib.parse import parse_qs, urlsplit

import pytest
from fakes import SERVER, FakeMastodon, make_ctx, reply

from marvin_integration_mastodon import MastodonProvider
from marvin_integration_mastodon.provider import MastodonError
from marvin_integration_mastodon.text import html_to_text

ME = {"id": "1", "acct": "me", "username": "me"}
ALICE = {"id": "7", "acct": "alice@remote.example", "username": "alice", "display_name": "Alice A"}
BOB = {"id": "8", "acct": "bob", "username": "bob", "display_name": ""}


def run(key, args, http, **ctx_kwargs):
    return MastodonProvider().run_action(key, args, make_ctx(http, **ctx_kwargs))


def note(nid, *, sid=None, account=ALICE, content="<p>hi</p>", parent=None, parent_account=None, when="2026-10-19T08:30:00.000Z"):
    """A mention notification; ``parent`` is the status it replies to, ``parent_account`` that status's author id."""
    return {
        "id": str(nid),
        "type": "mention",
        "created_at": when,
        "account": account,
        "status": {
            "id": str(sid or 5000 + int(nid)),
            "url": f"https://remote.example/@x/{sid or 5000 + int(nid)}",
            "content": content,
            "created_at": when,
            "account": account,
            "in_reply_to_id": parent,
            "in_reply_to_account_id": parent_account,
        },
    }


def refused(key, args, http=None, code="refused"):
    http = http or FakeMastodon()
    with pytest.raises(MastodonError) as caught:
        run(key, args, http)
    assert caught.value.code == code
    return caught.value


def asked(http):
    """The query strings of every notifications request."""
    return [parse_qs(urlsplit(r.url).query) for r in http.to("GET", "/api/v1/notifications")]


# ---- comments: shape, kinds, text ------------------------------------------------------------------------------------


def test_comments_maps_a_mention_notification_to_the_contract_item():
    http = FakeMastodon(notifications=[note(10, sid=555, content="<p>hello</p>", parent="321", parent_account="1", when="2026-10-19T08:30:00.123Z")])
    out = run("comments", {}, http)
    assert out["cursor"] == "10"
    assert out["items"] == [
        {
            "id": "555",
            "kind": "reply",
            "post_id": "321",
            "post_url": f"{SERVER}/@me/321",
            "author": {"id": "7", "handle": "alice@remote.example", "name": "Alice A"},
            "text": "hello",
            "created_at": "2026-10-19T08:30:00.123000+00:00",
            "url": "https://remote.example/@x/555",
            "network": "mastodon",
        }
    ]


def test_comments_asks_for_mentions_only_with_the_token():
    http = FakeMastodon(notifications=[note(1)])
    run("comments", {}, http)
    query = asked(http)[0]
    assert query["types[]"] == ["mention"] and "since_id" not in query
    assert http.to("GET", "/api/v1/notifications")[0].headers["Authorization"] == "Bearer fake-token"


def test_a_mention_replying_to_our_status_is_a_reply_and_any_other_is_a_mention():
    http = FakeMastodon(
        notifications=[
            note(1, parent="321", parent_account="1"),  # answers us
            note(2, parent="900", parent_account="8"),  # answers someone else, mentioning us
            note(3),  # not a reply at all
        ]
    )
    items = run("comments", {}, http)["items"]
    assert [i["kind"] for i in items] == ["reply", "mention", "mention"]
    assert [i["post_id"] for i in items] == ["321", "900", None]
    assert [i["post_url"] for i in items] == [f"{SERVER}/@me/321", None, None]


def test_kinds_filters_but_the_cursor_still_moves_past_what_was_skipped():
    http = FakeMastodon(notifications=[note(1, parent="3", parent_account="1"), note(2)])
    out = run("comments", {"kinds": ["reply"]}, http)
    assert [i["kind"] for i in out["items"]] == ["reply"] and out["cursor"] == "2"
    assert run("comments", {"kinds": ["comment"]}, http)["items"] == []  # Mastodon has no separate comments


def test_a_mention_on_a_direct_status_is_a_message_kept_apart_from_public_mentions():
    dm = note(3, parent="3", parent_account="1")
    dm["status"]["visibility"] = "direct"
    http = FakeMastodon(notifications=[note(1), dm])

    kinds = [i["kind"] for i in run("comments", {}, http)["items"]]
    (message,) = run("comments", {"kinds": ["message"]}, http)["items"]

    assert kinds == ["mention", "message"]
    assert (message["post_id"], message["post_url"]) == (None, None)  # a DM has no post to point at
    assert [i["kind"] for i in run("comments", {"kinds": ["comment", "reply", "mention"]}, http)["items"]] == ["mention"]


def test_html_is_stripped_to_plain_text():
    content = (
        '<p><span class="h-card"><a href="https://mastodon.example/@me" class="u-url mention">@<span>me</span></a></span> thanks &amp; '
        'see <a href="https://e.com/long/path"><span class="invisible">https://</span><span class="ellipsis">e.com/lo</span>'
        '<span class="invisible">ng/path</span></a><br>bye</p><p>2nd &lt;b&gt; <a href="https://x/tags/fun" class="hashtag">#<span>fun</span></a></p>'
    )
    assert html_to_text(content) == "@me thanks & see https://e.com/long/path\nbye\n\n2nd <b> #fun"
    assert html_to_text(None) == ""
    http = FakeMastodon(notifications=[note(1, content=content)])
    assert run("comments", {}, http)["items"][0]["text"].startswith("@me thanks & see https://e.com/long/path")


def test_a_handle_without_a_domain_and_a_missing_display_name_fall_back_to_the_username():
    out = run("comments", {}, FakeMastodon(notifications=[note(1, account=BOB)]))
    assert out["items"][0]["author"] == {"id": "8", "handle": "bob", "name": "bob"}


def test_a_mention_whose_status_is_gone_is_skipped():
    gone = note(1)
    gone["status"] = None
    out = run("comments", {}, FakeMastodon(notifications=[gone, note(2)]))
    assert [i["id"] for i in out["items"]] == ["5002"] and out["cursor"] == "2"


# ---- comments: cursor, order, limit, paging --------------------------------------------------------------------------


def test_items_come_back_oldest_first_and_the_cursor_is_the_newest_notification():
    http = FakeMastodon(notifications=[note(30), note(10), note(20)])
    out = run("comments", {}, http)
    assert [i["id"] for i in out["items"]] == ["5010", "5020", "5030"] and out["cursor"] == "30"


def test_the_cursor_is_sent_back_as_since_id_and_only_newer_items_return():
    http = FakeMastodon(notifications=[note(10), note(20), note(30)])
    out = run("comments", {"since": "20"}, http)
    assert [i["id"] for i in out["items"]] == ["5030"] and out["cursor"] == "30"
    assert asked(http)[0]["since_id"] == ["20"]


def test_polling_with_nothing_new_returns_no_items_and_the_same_cursor():
    http = FakeMastodon(notifications=[note(10)])
    out = run("comments", {"since": "10"}, http)
    assert out == {"items": [], "cursor": "10"}
    assert http.to("GET", "/api/v1/accounts/verify_credentials") == []  # nothing to classify, no lookup


def test_two_polls_in_a_row_see_every_mention_once():
    http = FakeMastodon(notifications=[note(1), note(2)])
    first = run("comments", {}, http)
    http.notifications.append(note(3))
    second = run("comments", {"since": first["cursor"]}, http)
    assert [i["id"] for i in first["items"]] == ["5001", "5002"] and [i["id"] for i in second["items"]] == ["5003"]


def test_it_pages_back_through_max_id_until_nothing_is_left():
    http = FakeMastodon(notifications=[note(n) for n in range(101, 201)])  # 100 new, page size 40
    out = run("comments", {"since": "100", "limit": 200}, http)
    assert [i["id"] for i in out["items"]] == [str(5000 + n) for n in range(101, 201)] and out["cursor"] == "200"
    queries = asked(http)
    assert [q.get("max_id") for q in queries] == [None, ["161"], ["121"], ["101"]]
    assert all(q["since_id"] == ["100"] for q in queries)


def test_limit_hands_over_the_oldest_and_the_cursor_stops_there_so_nothing_is_lost():
    http = FakeMastodon(notifications=[note(n) for n in range(1, 11)])
    out = run("comments", {"since": "0", "limit": 3}, http)
    assert [i["id"] for i in out["items"]] == ["5001", "5002", "5003"] and out["cursor"] == "3"
    rest = run("comments", {"since": out["cursor"], "limit": 100}, http)
    assert [i["id"] for i in rest["items"]] == [str(5000 + n) for n in range(4, 11)]


def test_a_first_run_without_a_cursor_returns_only_the_newest_limit():
    http = FakeMastodon(notifications=[note(n) for n in range(1, 11)])
    out = run("comments", {"limit": 3}, http)
    assert [i["id"] for i in out["items"]] == ["5008", "5009", "5010"] and out["cursor"] == "10"
    assert len(asked(http)) == 1  # it did not walk the whole history


def test_the_default_limit_is_50():
    http = FakeMastodon(notifications=[note(n) for n in range(1, 61)])
    out = run("comments", {"since": "0"}, http)
    assert len(out["items"]) == 50 and out["cursor"] == "50"


@pytest.mark.parametrize(
    "args", [{"limit": 0}, {"limit": 1000}, {"limit": "many"}, {"kinds": ["spam"]}, {"kinds": "reply"}, {"since": "../x"}, {"since": 5}]
)
def test_bad_comment_arguments_are_refused_before_any_request(args):
    http = FakeMastodon()
    refused("comments", args, http)
    assert http.requests == []


def test_a_server_that_ignores_max_id_does_not_loop_forever():
    http = FakeMastodon(notifications=[note(1), note(2)])
    plain = http.get
    http.get = lambda url, **kw: reply(200, [note(1), note(2)]) if "/notifications" in url else plain(url, **kw)
    assert [i["id"] for i in run("comments", {"since": "0"}, http)["items"]] == ["5001", "5002"]


# ---- reply -----------------------------------------------------------------------------------------------------------


def status_of(sid, account=ALICE):
    return {"id": sid, "url": f"{SERVER}/@x/{sid}", "account": account}


def test_a_private_reply_is_a_direct_status_to_the_commenter_with_the_handle_in_front():
    http = FakeMastodon(statuses={"555": status_of("555")})
    out = run("reply", {"comment_id": "555", "text": "Thanks!"}, http)
    assert out == {"replied": True, "dry_run": False, "reply_id": "110001", "private": True, "url": f"{SERVER}/@me/110001", "network": "mastodon"}
    [req] = http.status_posts
    assert req.form() == {"status": ["@alice@remote.example Thanks!"], "visibility": ["direct"], "in_reply_to_id": ["555"]}
    assert req.headers["Idempotency-Key"].startswith("marvin-")


def test_private_is_the_default_and_a_string_false_means_public():
    http = FakeMastodon(statuses={"555": status_of("555")})
    run("reply", {"comment_id": "555", "text": "a"}, http)
    run("reply", {"comment_id": "555", "text": "b", "private": "false"}, http)
    assert [r.form()["visibility"] for r in http.status_posts] == [["direct"], ["public"]]


def test_a_public_reply_is_public_or_unlisted_when_the_connection_is_unlisted():
    http = FakeMastodon(statuses={"555": status_of("555")})
    out = run("reply", {"comment_id": "555", "text": "Hi", "private": False}, http)
    assert out["private"] is False and http.status_posts[0].form()["visibility"] == ["public"]
    http = FakeMastodon(statuses={"555": status_of("555")})
    run("reply", {"comment_id": "555", "text": "Hi", "private": False}, http, config={"visibility": "unlisted"})
    assert http.status_posts[0].form()["visibility"] == ["unlisted"]
    http = FakeMastodon(statuses={"555": status_of("555")})
    run("reply", {"comment_id": "555", "text": "Hi", "private": False}, http, config={"visibility": "private"})
    assert http.status_posts[0].form()["visibility"] == ["public"]  # followers-only is not a reply mode


def test_the_configured_language_goes_with_the_reply():
    http = FakeMastodon(statuses={"555": status_of("555")})
    run("reply", {"comment_id": "555", "text": "Hi"}, http, config={"language": "en"})
    assert http.status_posts[0].form()["language"] == ["en"]


@pytest.mark.parametrize(
    ("account", "text", "expected"),
    [
        (ALICE, "@alice@remote.example thanks", "@alice@remote.example thanks"),
        (ALICE, "thanks @Alice@Remote.example!", "thanks @Alice@Remote.example!"),
        (ALICE, "thanks @alice", "@alice@remote.example thanks @alice"),  # a bare @alice could be a local account
        (BOB, "hey @bob, thanks", "hey @bob, thanks"),
        (BOB, "@bobby thanks", "@bob @bobby thanks"),  # a different handle that starts the same
        (BOB, "@bob@other.example hi", "@bob @bob@other.example hi"),  # another account on another server
        (BOB, "thanks", "@bob thanks"),
    ],
)
def test_the_handle_is_prepended_unless_it_is_already_mentioned(account, text, expected):
    http = FakeMastodon(statuses={"9": status_of("9", account)})
    run("reply", {"comment_id": "9", "text": text}, http)
    assert http.status_posts[0].form()["status"] == [expected]


def test_the_reply_is_checked_against_the_character_limit_with_the_handle_counted():
    http = FakeMastodon(statuses={"9": status_of("9", BOB)})
    error = refused("reply", {"comment_id": "9", "text": "x" * 497}, http)  # "@bob " is 5, so 502 > 500
    assert "502" in str(error) and "500" in str(error)
    assert http.status_posts == []
    run("reply", {"comment_id": "9", "text": "x" * 495}, http)  # exactly 500
    assert len(http.status_posts) == 1


def test_a_url_in_a_reply_counts_as_23_characters():
    http = FakeMastodon(statuses={"9": status_of("9", BOB)})
    run("reply", {"comment_id": "9", "text": "see https://example.org/" + "a" * 600}, http)
    assert len(http.status_posts) == 1


def test_the_same_reply_sent_twice_carries_the_same_idempotency_key_and_a_different_one_for_other_content():
    http = FakeMastodon(statuses={"9": status_of("9", BOB), "10": status_of("10", BOB)})
    for cid, text in (("9", "a"), ("9", "a"), ("10", "a"), ("9", "b")):
        run("reply", {"comment_id": cid, "text": text}, http)
    keys = [r.headers["Idempotency-Key"] for r in http.status_posts]
    assert keys[0] == keys[1] and len({*keys}) == 3


def test_reply_dry_run_posts_nothing_and_reports_what_would_go_out():
    http = FakeMastodon(statuses={"555": status_of("555")})
    out = run("reply", {"comment_id": "555", "text": "Thanks!", "dry_run": True}, http)
    assert out["replied"] is False and out["dry_run"] is True and out["reply_id"] is None and out["private"] is True
    would = out["would_reply"]
    assert would["text"] == "@alice@remote.example Thanks!" and would["visibility"] == "direct" and would["in_reply_to_id"] == "555"
    assert would["to"] == "alice@remote.example" and would["limit"] == 500
    assert http.status_posts == []


def test_reply_dry_run_still_applies_the_limit_and_works_when_the_instance_info_is_down():
    http = FakeMastodon(statuses={"9": status_of("9", BOB)}, instance_status=500)
    out = run("reply", {"comment_id": "9", "text": "ok", "dry_run": True}, http)
    assert out["would_reply"]["limits_from"] == "default"
    refused("reply", {"comment_id": "9", "text": "x" * 600, "dry_run": True}, http)


def test_a_real_reply_needs_the_instance_info():
    http = FakeMastodon(statuses={"9": status_of("9", BOB)}, instance_status=500)
    refused("reply", {"comment_id": "9", "text": "ok"}, http, code="unavailable")
    assert http.status_posts == []


@pytest.mark.parametrize("args", [{"text": "hi"}, {"comment_id": "9"}, {"comment_id": "9", "text": "  "}, {"comment_id": "../x", "text": "hi"}])
def test_reply_refuses_missing_arguments(args):
    http = FakeMastodon()
    refused("reply", args, http)
    assert http.requests == []


def test_replying_to_a_status_that_is_gone_surfaces_mastodons_reason():
    with pytest.raises(MastodonError, match="Record not found"):
        run("reply", {"comment_id": "404", "text": "hi"}, FakeMastodon())


def test_a_reply_mastodon_refuses_is_a_refusal():
    http = FakeMastodon(statuses={"9": status_of("9", BOB)}, status_reply=reply(422, {"error": "Validation failed: Text can't be blank"}))
    refused("reply", {"comment_id": "9", "text": "hi"}, http)


# ---- like / repost ---------------------------------------------------------------------------------------------------


def test_like_favourites_the_status():
    http = FakeMastodon()
    assert run("like", {"comment_id": "555"}, http) == {"liked": True, "network": "mastodon"}
    [req] = http.requests
    assert (req.method, req.url) == ("POST", f"{SERVER}/api/v1/statuses/555/favourite")
    assert req.headers["Authorization"] == "Bearer fake-token"


def test_repost_boosts_and_returns_the_boosted_posts_url():
    http = FakeMastodon()
    assert run("repost", {"post_id": "321"}, http) == {"reposted": True, "url": f"{SERVER}/@them/321", "network": "mastodon"}
    [req] = http.requests
    assert (req.method, req.url) == ("POST", f"{SERVER}/api/v1/statuses/321/reblog")


@pytest.mark.parametrize(("key", "args"), [("like", {}), ("like", {"comment_id": "1/2"}), ("repost", {}), ("repost", {"post_id": "?x"})])
def test_like_and_repost_refuse_a_missing_or_malformed_id(key, args):
    http = FakeMastodon()
    refused(key, args, http)
    assert http.requests == []


def test_like_surfaces_a_missing_scope_and_a_missing_status():
    http = FakeMastodon()
    http.post = lambda url, **kw: reply(403, {"error": "This action is outside the authorized scopes"})
    with pytest.raises(MastodonError, match="write:favourites") as caught:
        run("like", {"comment_id": "1"}, http)
    assert caught.value.code == "auth"
    http.post = lambda url, **kw: reply(404, {"error": "Record not found"})
    with pytest.raises(MastodonError, match="Record not found"):
        run("repost", {"post_id": "1"}, http)


# ---- message ---------------------------------------------------------------------------------------------------------


def test_message_by_handle_is_a_direct_status_mentioning_them():
    http = FakeMastodon()
    out = run("message", {"to": {"handle": "@carol@elsewhere.example"}, "text": "Hello"}, http)
    assert out == {"sent": True, "dry_run": False, "message_id": "110001", "network": "mastodon"}
    [req] = http.status_posts
    assert req.form() == {"status": ["@carol@elsewhere.example Hello"], "visibility": ["direct"]}
    assert http.to("GET", "/api/v1/accounts/") == []  # the handle was given: no lookup


def test_message_by_id_looks_up_the_handle():
    http = FakeMastodon(accounts={"7": ALICE})
    out = run("message", {"to": {"id": "7"}, "text": "Hello"}, http)
    assert out["sent"] is True
    assert [r.path for r in http.to("GET", "/api/v1/accounts/")] == ["/api/v1/accounts/7"]
    assert http.status_posts[0].form()["status"] == ["@alice@remote.example Hello"]


def test_message_prefers_the_handle_when_both_are_given_and_does_not_double_the_mention():
    http = FakeMastodon(accounts={"7": ALICE})
    run("message", {"to": {"id": "7", "handle": "carol"}, "text": "@carol hi"}, http)
    assert http.to("GET", "/api/v1/accounts/") == []
    assert http.status_posts[0].form()["status"] == ["@carol hi"]


def test_message_dry_run_sends_nothing():
    http = FakeMastodon(accounts={"7": ALICE})
    out = run("message", {"to": {"id": "7"}, "text": "Hello", "dry_run": True}, http)
    assert out["sent"] is False and out["dry_run"] is True and out["message_id"] is None
    assert out["would_send"]["to"] == "alice@remote.example" and out["would_send"]["visibility"] == "direct"
    assert http.status_posts == []


def test_message_is_checked_against_the_character_limit():
    http = FakeMastodon()
    error = refused("message", {"to": {"handle": "carol"}, "text": "x" * 600}, http)
    assert "500" in str(error) and http.status_posts == []


@pytest.mark.parametrize(
    "args",
    [
        {"text": "hi"},
        {"to": "carol", "text": "hi"},
        {"to": {}, "text": "hi"},
        {"to": {"handle": "ca rol"}, "text": "hi"},
        {"to": {"handle": "carol"}, "text": ""},
        {"to": {"id": "../x"}, "text": "hi"},
    ],
)
def test_message_refuses_bad_arguments(args):
    http = FakeMastodon()
    refused("message", args, http)
    assert http.requests == []


def test_message_to_an_unknown_account_id_surfaces_mastodons_reason():
    with pytest.raises(MastodonError, match="Record not found"):
        run("message", {"to": {"id": "99"}, "text": "hi"}, FakeMastodon())


# ---- delete_comment --------------------------------------------------------------------------------------------------


def test_delete_comment_deletes_one_of_our_own_statuses():
    http = FakeMastodon(statuses={"600": status_of("600", ME)})
    assert run("delete_comment", {"comment_id": "600"}, http) == {"deleted": True, "network": "mastodon"}
    assert [(r.method, r.path) for r in http.requests if r.method == "DELETE"] == [("DELETE", "/api/v1/statuses/600")]


def test_delete_comment_refuses_someone_elses_status_as_unsupported_and_deletes_nothing():
    http = FakeMastodon(statuses={"555": status_of("555", ALICE)})
    error = refused("delete_comment", {"comment_id": "555"}, http, code="unsupported")
    assert "only your own replies" in str(error)
    assert http.to("DELETE", "/") == []


def test_delete_comment_needs_a_status_id_and_surfaces_a_missing_status():
    http = FakeMastodon()
    refused("delete_comment", {}, http)
    assert http.requests == []
    with pytest.raises(MastodonError, match="Record not found"):
        run("delete_comment", {"comment_id": "404"}, http)


# ---- failures common to all of them ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "args"),
    [
        ("comments", {}),
        ("reply", {"comment_id": "1", "text": "hi"}),
        ("like", {"comment_id": "1"}),
        ("repost", {"post_id": "1"}),
        ("message", {"to": {"handle": "a"}, "text": "hi"}),
        ("delete_comment", {"comment_id": "1"}),
    ],
)
def test_no_token_is_an_auth_error_before_any_request(key, args):
    http = FakeMastodon()
    with pytest.raises(MastodonError) as caught:
        run(key, args, http, secret="")
    assert caught.value.code == "auth" and http.requests == []


def test_a_rejected_token_and_a_rate_limit_while_reading_comments():
    http = FakeMastodon()
    http.get = lambda url, **kw: reply(401, {"error": "The access token is invalid"})
    with pytest.raises(MastodonError) as caught:
        run("comments", {}, http)
    assert caught.value.code == "auth" and "fake-token" not in str(caught.value)
    http.get = lambda url, **kw: reply(429, {"error": "Too many requests"}, {"Retry-After": "30"})
    with pytest.raises(MastodonError) as caught:
        run("comments", {}, http)
    assert caught.value.code == "rate_limited" and caught.value.retry_after == 30


def test_a_missing_read_scope_is_named():
    http = FakeMastodon()
    http.get = lambda url, **kw: reply(403, {"error": "This action is outside the authorized scopes"})
    with pytest.raises(MastodonError, match="read:notifications") as caught:
        run("comments", {}, http)
    assert caught.value.code == "auth"


def test_an_unreachable_server_is_unavailable():
    http = FakeMastodon(raises={"GET /api/v1/notifications": TimeoutError("timed out")})
    with pytest.raises(MastodonError) as caught:
        run("comments", {}, http)
    assert caught.value.code == "unavailable"


def test_a_notifications_body_that_is_not_a_list_is_an_error():
    http = FakeMastodon()
    http.get = lambda url, **kw: reply(200, {"oops": 1})
    with pytest.raises(MastodonError, match="unexpected"):
        run("comments", {}, http)


# ---- what the plugin declares ----------------------------------------------------------------------------------------

DECLARED = {
    "post_status": ("social.post", True),
    "comments": ("social.comments", False),
    "reply": ("social.reply", True),
    "like": ("social.like", False),
    "repost": ("social.repost", True),
    "message": ("social.message", True),
    "delete_comment": ("social.delete_comment", True),
}


@pytest.mark.parametrize(("key", "expected"), DECLARED.items())
def test_each_action_declares_its_capability_and_approval(key, expected):
    action = MastodonProvider().get_action(key)
    assert (action.capability, action.requires_approval) == expected


def test_capabilities_are_unique_and_there_is_no_hide():
    provider = MastodonProvider()
    capabilities = [a.capability for a in provider.actions if a.capability]
    assert len(capabilities) == len(set(capabilities)) and "social.hide" not in capabilities
    assert {a.key for a in provider.actions} == {*DECLARED, "delete_status"}


def test_delete_status_is_unchanged_and_declares_no_capability():
    action = MastodonProvider().get_action("delete_status")
    assert action.capability is None and action.requires_approval is True


def test_the_input_schemas_follow_the_contract():
    get = MastodonProvider().get_action
    assert set(get("comments").input_schema["properties"]) == {"since", "limit", "kinds"}
    assert get("comments").input_schema["properties"]["limit"]["default"] == 50
    assert set(get("reply").input_schema["properties"]) == {"comment_id", "text", "private", "dry_run"}
    assert get("reply").input_schema["properties"]["private"]["default"] is True
    assert get("reply").input_schema["required"] == ["comment_id", "text"]
    assert set(get("message").input_schema["properties"]) == {"to", "text", "dry_run"}
    assert get("repost").input_schema["required"] == ["post_id"]
    assert get("like").input_schema["required"] == get("delete_comment").input_schema["required"] == ["comment_id"]
