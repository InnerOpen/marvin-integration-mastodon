"""post_status: text, the appended link, the character limit, images with alt text, media processing, the image limit,
visibility, language, the dry run and the idempotency key."""

import pytest
from fakes import JPEG, PNG, SERVER, FakeMastodon, make_ctx

from marvin_integration_mastodon import MastodonProvider
from marvin_integration_mastodon.provider import MEDIA_POLL_ATTEMPTS, MEDIA_POLL_SECONDS, MastodonError

IMG1, IMG2 = "https://assets.example.test/a/hero.png", "https://assets.example.test/b/second.jpg"
PICS = {IMG1: (PNG, "image/png"), IMG2: (JPEG, "image/jpeg")}


def post(http, args, **ctx_kwargs):
    return MastodonProvider().run_action("post_status", args, make_ctx(http, **ctx_kwargs))


def refused(http, args, **ctx_kwargs) -> MastodonError:
    with pytest.raises(MastodonError) as caught:
        post(http, args, **ctx_kwargs)
    assert caught.value.code == "refused"
    return caught.value


# ---- text ------------------------------------------------------------------------------------


def test_text_only_post_sends_the_status_and_returns_the_contract_output():
    http = FakeMastodon()
    out = post(http, {"text": "Hello, fediverse"})
    assert out["posted"] is True
    assert out["post_id"] == "110001"
    assert out["url"] == f"{SERVER}/@me/110001"
    assert out["network"] == "mastodon"
    assert "would_post" not in out
    [req] = http.status_posts
    assert req.url == f"{SERVER}/api/v1/statuses"
    assert req.headers["Authorization"] == "Bearer fake-token"
    assert req.form()["status"] == ["Hello, fediverse"]
    assert http.uploads == []


def test_the_post_id_is_a_string_even_when_mastodon_sends_a_number():
    from fakes import reply

    http = FakeMastodon(status_reply=reply(200, {"id": 42, "url": None}))
    out = post(http, {"text": "hi"})
    assert out["post_id"] == "42"
    assert out["url"] is None


@pytest.mark.parametrize("args", [{}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": None}])
def test_text_is_required(args):
    http = FakeMastodon()
    refused(http, args)
    assert http.status_posts == []


def test_a_missing_status_id_in_the_answer_is_an_error():
    from fakes import reply

    with pytest.raises(MastodonError, match="no status id"):
        post(FakeMastodon(status_reply=reply(200, {"url": "x"})), {"text": "hi"})


# ---- link ----------------------------------------------------------------------------------------


def test_link_is_appended_on_a_new_line():
    http = FakeMastodon()
    post(http, {"text": "New post is up", "link": "https://example.org/blog/new"})
    assert http.status_posts[0].form()["status"] == ["New post is up\nhttps://example.org/blog/new"]


def test_link_already_in_the_text_is_not_appended_again():
    http = FakeMastodon()
    post(http, {"text": "Read https://example.org/blog/new today", "link": "https://example.org/blog/new"})
    assert http.status_posts[0].form()["status"] == ["Read https://example.org/blog/new today"]


@pytest.mark.parametrize("link", ["example.org", "javascript:alert(1)", "ftp://example.org/x", "https://a b"])
def test_a_link_that_is_not_http_is_refused(link):
    refused(FakeMastodon(), {"text": "hi", "link": link})


def test_blank_link_is_ignored():
    http = FakeMastodon()
    post(http, {"text": "hi", "link": "  "})
    assert http.status_posts[0].form()["status"] == ["hi"]


# ---- character limit (URLs count as 23) ---------------------------------------------------------


def test_exactly_the_limit_posts_and_one_over_is_refused_with_a_per_network_hint():
    http = FakeMastodon()
    post(http, {"text": "a" * 500})
    err = refused(http, {"text": "a" * 501})
    assert "501" in str(err) and "500" in str(err)
    assert "per_network" in str(err)
    assert len(http.status_posts) == 1


def test_a_url_counts_as_23_characters_however_long_it_is():
    long_url = "https://example.org/" + "x" * 300
    http = FakeMastodon()
    post(http, {"text": f"{'a' * 476} {long_url}"})  # 476 + space + 23 = 500
    assert len(http.status_posts) == 1
    refused(http, {"text": f"{'a' * 477} {long_url}"})  # 501


def test_the_appended_link_counts_too_and_the_error_says_so():
    err = refused(FakeMastodon(), {"text": "a" * 477, "link": "https://example.org/x"})  # 477 + newline + 23 = 501
    assert "link was appended" in str(err)


def test_a_mention_counts_only_its_local_part():
    http = FakeMastodon()
    post(http, {"text": "@someone@a-very-long-instance-name.example.social " + "a" * 480})  # 12 + 1 + 480 = 493
    assert len(http.status_posts) == 1


def test_the_limit_comes_from_the_instance():
    instance = {"configuration": {"statuses": {"max_characters": 5000, "max_media_attachments": 4, "characters_reserved_per_url": 23}}}
    http = FakeMastodon(instance=instance)
    post(http, {"text": "a" * 5000})
    assert http.to("GET", "/api/v2/instance")[0].url == f"{SERVER}/api/v2/instance"
    refused(http, {"text": "a" * 5001})


def test_a_smaller_instance_limit_is_enforced():
    instance = {"configuration": {"statuses": {"max_characters": 100}}}
    refused(FakeMastodon(instance=instance), {"text": "a" * 101})


def test_the_reserved_url_length_comes_from_the_instance_too():
    instance = {"configuration": {"statuses": {"max_characters": 50, "characters_reserved_per_url": 10}}}
    post(FakeMastodon(instance=instance), {"text": "https://example.org/" + "x" * 100 + " " + "a" * 39})  # 10 + 1 + 39 = 50


@pytest.mark.parametrize("instance", [{}, {"configuration": {}}, {"configuration": {"statuses": {"max_characters": "oops"}}}])
def test_500_is_the_default_when_the_instance_does_not_say(instance):
    http = FakeMastodon(instance=instance)
    post(http, {"text": "a" * 500})
    refused(http, {"text": "a" * 501})


def test_a_server_without_the_v2_endpoint_uses_the_defaults():
    http = FakeMastodon(instance_status=404)
    post(http, {"text": "hello"})
    assert len(http.status_posts) == 1


def test_a_real_post_does_not_guess_when_the_instance_cannot_be_read():
    http = FakeMastodon(instance_status=503)
    with pytest.raises(MastodonError) as caught:
        post(http, {"text": "hello"})
    assert caught.value.code == "unavailable"
    assert http.status_posts == []


# ---- images and alt text ---------------------------------------------------------------------------


def test_images_are_downloaded_uploaded_with_alt_text_and_attached_in_order():
    http = FakeMastodon(images=PICS)
    out = post(http, {"text": "Two pictures", "images": [IMG1, IMG2], "alt": ["A hero shot", "The second"]})
    assert out["images"] == 2
    first, second = http.uploads
    parts = first.multipart()
    assert parts["file"]["value"] == PNG
    assert parts["file"]["content_type"] == "image/png"
    assert parts["file"]["filename"] == "hero.png"
    assert parts["description"]["value"] == b"A hero shot"
    assert second.multipart()["file"]["value"] == JPEG
    assert second.multipart()["description"]["value"] == b"The second"
    assert http.status_posts[0].form()["media_ids[]"] == ["m1", "m2"]
    assert first.url == f"{SERVER}/api/v2/media"
    assert first.headers["Content-Type"].startswith("multipart/form-data; boundary=")


def test_the_download_goes_through_the_http_helper_without_the_token():
    http = FakeMastodon(images=PICS)
    post(http, {"text": "pic", "images": [IMG1]})
    [download] = [r for r in http.requests if r.url == IMG1]
    assert download.method == "GET"
    assert "Authorization" not in download.headers  # the token is for the Mastodon server only
    assert "fake-token" not in repr(download)


def test_an_image_without_alt_uploads_with_no_description():
    http = FakeMastodon(images=PICS)
    post(http, {"text": "pic", "images": [IMG1, IMG2], "alt": ["Only the first"]})
    assert "description" in http.uploads[0].multipart()
    assert "description" not in http.uploads[1].multipart()


def test_alt_longer_than_the_images_is_refused_before_anything_is_uploaded():
    http = FakeMastodon(images=PICS)
    refused(http, {"text": "pic", "images": [IMG1], "alt": ["one", "two"]})
    assert http.requests == []


def test_alt_over_the_instance_limit_is_refused():
    instance = {"configuration": {"statuses": {}, "media_attachments": {"description_limit": 10}}}
    http = FakeMastodon(images=PICS, instance=instance)
    refused(http, {"text": "pic", "images": [IMG1], "alt": ["x" * 11]})
    assert http.uploads == []


def test_an_image_served_as_octet_stream_is_recognised_by_its_bytes():
    http = FakeMastodon(images={"https://assets.example.test/photo": (PNG, "application/octet-stream")})
    post(http, {"text": "pic", "images": ["https://assets.example.test/photo"]})
    part = http.uploads[0].multipart()["file"]
    assert part["content_type"] == "image/png"
    assert part["filename"] == "photo.png"


def test_a_url_that_is_not_an_image_is_refused():
    http = FakeMastodon(images={IMG1: (b"<html>nope</html>", "text/html")})
    refused(http, {"text": "pic", "images": [IMG1]})
    assert http.uploads == [] and http.status_posts == []


def test_a_failed_download_names_the_url_and_posts_nothing():
    http = FakeMastodon(images={})  # the fake answers 404 for an unknown URL
    with pytest.raises(MastodonError, match="hero.png"):
        post(http, {"text": "pic", "images": [IMG1]})
    assert http.status_posts == []


def test_a_blocked_download_is_a_readable_error():
    http = FakeMastodon(images=PICS, raises={f"GET {IMG1}": RuntimeError("blocked: private address")})
    with pytest.raises(MastodonError, match="blocked: private address"):
        post(http, {"text": "pic", "images": [IMG1]})


def test_an_image_over_the_instance_size_limit_is_refused():
    instance = {"configuration": {"statuses": {}, "media_attachments": {"image_size_limit": 5}}}
    refused(FakeMastodon(images=PICS, instance=instance), {"text": "pic", "images": [IMG1]})


@pytest.mark.parametrize("url", ["http://assets.example.test/a.png", "/local/a.png", "ftp://x/a.png"])
def test_only_https_image_urls_are_accepted(url):
    refused(FakeMastodon(), {"text": "pic", "images": [url]})


def test_images_must_be_a_list_of_strings():
    refused(FakeMastodon(), {"text": "pic", "images": IMG1})
    refused(FakeMastodon(), {"text": "pic", "images": [IMG1, 3]})


# ---- media processing (202 -> poll) ---------------------------------------------------------------------


def test_a_202_upload_is_polled_until_processing_finishes(sleeps):
    http = FakeMastodon(images=PICS, upload_status=202, polls=[206, 206, 200])
    out = post(http, {"text": "pic", "images": [IMG1]})
    assert out["posted"] is True
    polls = http.to("GET", "/api/v1/media/")
    assert [p.path for p in polls] == ["/api/v1/media/m1"] * 3
    assert sleeps == [MEDIA_POLL_SECONDS, MEDIA_POLL_SECONDS]
    assert http.status_posts[0].form()["media_ids[]"] == ["m1"]
    assert http.requests.index(polls[-1]) < http.requests.index(http.status_posts[0])  # attached only once ready


def test_a_200_upload_needs_no_polling(sleeps):
    http = FakeMastodon(images=PICS, upload_status=200)
    post(http, {"text": "pic", "images": [IMG1]})
    assert http.to("GET", "/api/v1/media/") == [] and sleeps == []


def test_media_that_never_finishes_fails_without_posting(sleeps):
    http = FakeMastodon(images=PICS, upload_status=202, polls=[206])
    with pytest.raises(MastodonError, match="still processing") as caught:
        post(http, {"text": "pic", "images": [IMG1]})
    assert caught.value.code == "unavailable"
    assert len(http.to("GET", "/api/v1/media/")) == MEDIA_POLL_ATTEMPTS
    assert len(sleeps) == MEDIA_POLL_ATTEMPTS - 1
    assert http.status_posts == []


def test_a_media_poll_error_stops_the_post():
    http = FakeMastodon(images=PICS, upload_status=202, polls=[404])
    with pytest.raises(MastodonError, match="HTTP 404"):
        post(http, {"text": "pic", "images": [IMG1]})
    assert http.status_posts == []


def test_an_upload_the_server_rejects_is_a_refusal():
    from fakes import reply

    http = FakeMastodon(images=PICS)
    original = http.post

    def rejecting(url, **kw):
        if url.endswith("/api/v2/media"):
            http._record("POST", url, kw.get("headers"), kw.get("data"))
            return reply(422, {"error": "Validation failed: File content type is invalid"})
        return original(url, **kw)

    http.post = rejecting
    err = refused(http, {"text": "pic", "images": [IMG1]})
    assert "content type is invalid" in str(err)


# ---- image count limit -----------------------------------------------------------------------------------


def test_more_than_four_images_is_refused_by_default_before_any_download():
    urls = [f"https://assets.example.test/{i}.png" for i in range(5)]
    http = FakeMastodon(images={u: (PNG, "image/png") for u in urls})
    err = refused(http, {"text": "pics", "images": urls})
    assert "4 images" in str(err) and "per_network" in str(err)
    assert not [r for r in http.requests if r.url in urls] and http.uploads == []


def test_exactly_four_images_post():
    urls = [f"https://assets.example.test/{i}.png" for i in range(4)]
    http = FakeMastodon(images={u: (PNG, "image/png") for u in urls})
    assert post(http, {"text": "pics", "images": urls})["images"] == 4
    assert len(http.status_posts[0].form()["media_ids[]"]) == 4


def test_the_image_limit_comes_from_the_instance():
    urls = [f"https://assets.example.test/{i}.png" for i in range(5)]
    instance = {"configuration": {"statuses": {"max_media_attachments": 6}}}
    http = FakeMastodon(images={u: (PNG, "image/png") for u in urls}, instance=instance)
    assert post(http, {"text": "pics", "images": urls})["images"] == 5
    refused(FakeMastodon(instance={"configuration": {"statuses": {"max_media_attachments": 2}}}), {"text": "x", "images": urls[:3]})


def test_more_than_ten_images_is_over_the_contract_whatever_the_instance_says():
    urls = [f"https://assets.example.test/{i}.png" for i in range(11)]
    refused(FakeMastodon(instance={"configuration": {"statuses": {"max_media_attachments": 20}}}), {"text": "x", "images": urls})


# ---- visibility and language -------------------------------------------------------------------------------


def test_visibility_defaults_to_public():
    http = FakeMastodon()
    post(http, {"text": "hi"})
    assert http.status_posts[0].form()["visibility"] == ["public"]


@pytest.mark.parametrize("visibility", ["public", "unlisted", "private"])
def test_visibility_from_the_config_is_sent(visibility):
    http = FakeMastodon()
    post(http, {"text": "hi"}, config={"visibility": visibility})
    assert http.status_posts[0].form()["visibility"] == [visibility]


@pytest.mark.parametrize("visibility", ["direct", "everyone", "followers"])
def test_other_visibilities_are_refused_before_anything_is_sent(visibility):
    http = FakeMastodon()
    with pytest.raises(MastodonError) as caught:
        post(http, {"text": "hi"}, config={"visibility": visibility})
    assert caught.value.code == "config"
    assert http.status_posts == []


def test_blank_visibility_means_public():
    http = FakeMastodon()
    post(http, {"text": "hi"}, config={"visibility": "  "})
    assert http.status_posts[0].form()["visibility"] == ["public"]


def test_language_is_sent_when_configured_and_omitted_otherwise():
    http = FakeMastodon()
    post(http, {"text": "hi"}, config={"language": "en"})
    post(http, {"text": "hi"})
    assert http.status_posts[0].form()["language"] == ["en"]
    assert "language" not in http.status_posts[1].form()


def test_a_malformed_language_is_a_config_error():
    with pytest.raises(MastodonError) as caught:
        post(FakeMastodon(), {"text": "hi"}, config={"language": "english please"})
    assert caught.value.code == "config"


# ---- dry run --------------------------------------------------------------------------------------------------


def test_dry_run_reports_the_plan_and_uploads_and_posts_nothing():
    http = FakeMastodon(images=PICS)
    out = post(
        http,
        {"text": "Plan me", "images": [IMG1, IMG2], "alt": ["one"], "link": "https://example.org/x", "dry_run": True},
        config={"visibility": "unlisted", "language": "en"},
    )
    assert out["posted"] is False and out["post_id"] is None and out["url"] is None and out["network"] == "mastodon"
    assert out["would_post"] == {
        "server": SERVER,
        "text": "Plan me\nhttps://example.org/x",
        "characters": len("Plan me\n") + 23,
        "limit": 500,
        "limits_from": "instance",
        "visibility": "unlisted",
        "language": "en",
        "media": [{"url": IMG1, "alt": "one"}, {"url": IMG2, "alt": None}],
    }
    assert http.uploads == [] and http.status_posts == []
    assert not [r for r in http.requests if r.url in (IMG1, IMG2)]  # not even downloaded
    assert {r.method for r in http.requests} == {"GET"}


def test_dry_run_still_applies_the_limits():
    refused(FakeMastodon(), {"text": "a" * 501, "dry_run": True})
    refused(FakeMastodon(), {"text": "x", "images": [f"https://a.example/{i}.png" for i in range(5)], "dry_run": True})


def test_dry_run_uses_the_defaults_when_the_instance_cannot_be_reached():
    http = FakeMastodon(raises={"GET /api/v2/instance": TimeoutError("timed out")})
    out = post(http, {"text": "hello", "dry_run": True})
    assert out["would_post"]["limit"] == 500 and out["would_post"]["limits_from"] == "default"
    assert http.status_posts == []


def test_dry_run_accepts_the_string_a_template_may_hand_over():
    http = FakeMastodon()
    assert post(http, {"text": "hi", "dry_run": "true"})["posted"] is False
    assert post(http, {"text": "hi", "dry_run": "false"})["posted"] is True


def test_posting_is_the_default_because_core_only_runs_it_after_an_approval():
    assert post(FakeMastodon(), {"text": "hi"})["posted"] is True


# ---- idempotency ----------------------------------------------------------------------------------------------------


def key_of(http, index=0):
    return http.status_posts[index].headers["Idempotency-Key"]


def test_the_status_carries_an_idempotency_key():
    http = FakeMastodon()
    post(http, {"text": "hi"})
    assert key_of(http)
    assert "Idempotency-Key" not in http.requests[0].headers  # only the status needs one


def test_a_retried_call_sends_the_same_key():
    http = FakeMastodon(images=PICS)
    args = {"text": "Same post", "images": [IMG1], "alt": ["alt"]}
    post(http, args)
    post(http, dict(args))
    assert key_of(http, 0) == key_of(http, 1)


@pytest.mark.parametrize(
    "other",
    [
        {"text": "Different"},
        {"text": "Same", "images": [IMG1]},
        {"text": "Same", "images": [IMG1], "alt": ["alt"]},
        {"text": "Same", "link": "https://e.org/x"},
    ],
)
def test_different_content_sends_a_different_key(other):
    http = FakeMastodon(images=PICS)
    post(http, {"text": "Same"})
    post(http, other)
    assert key_of(http, 0) != key_of(http, 1)


def test_the_key_depends_on_visibility_and_the_retry_chain_seed():
    http = FakeMastodon()
    post(http, {"text": "hi"})
    post(http, {"text": "hi"}, config={"visibility": "unlisted"})
    post(http, {"text": "hi"}, seed="run-1")
    post(http, {"text": "hi"}, seed="run-1")
    post(http, {"text": "hi"}, seed="run-2")
    keys = [key_of(http, i) for i in range(5)]
    assert keys[0] != keys[1] != keys[2]
    assert keys[2] == keys[3] != keys[4]


def test_the_key_never_contains_the_token():
    http = FakeMastodon()
    post(http, {"text": "hi"}, secret="super-secret-token")
    assert "super-secret-token" not in key_of(http)
