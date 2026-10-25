"""Mastodon provider: post a status (text, a link, up to a few images with alt text) through ``social.post``, and take
part in the conversation around it through the shared social engagement capabilities.

Marvin's shared ``social.post`` capability gives every connected network one argument shape; ``post_status`` maps it
onto Mastodon's REST API: the instance's limits are read from ``GET /api/v2/instance``, each image is downloaded and
uploaded with ``POST /api/v2/media`` (waiting for Mastodon to finish processing it when it answers 202), then the
status goes out with ``POST /api/v1/statuses`` under an ``Idempotency-Key`` so a retried call can't double-post.

The provider is pure with respect to Marvin: it gets ``config``, the resolved ``secret`` (the access token), a ``logger``
and a safe ``http`` client via ``ctx`` and returns dicts. It never touches the database; whether a post may go out is
Marvin's approval step, not the provider's business (the action just declares ``requires_approval``).

Engagement (0.2): ``social.comments`` reads mention notifications (``GET /api/v1/notifications``) as normalised comments;
``social.reply`` answers one with a direct or public status; ``social.like`` favourites, ``social.repost`` boosts,
``social.message`` sends a direct status and ``social.delete_comment`` deletes one of the account's own statuses.
Mastodon can't hide other people's replies, so there is no ``social.hide``.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import ClassVar
from urllib.parse import quote, urlencode, urlsplit

from marvin_integration_sdk import (
    CATEGORY_DESTINATION,
    CredentialField,
    ErrorPolicy,
    Handle,
    IntegrationContext,
    IntegrationError,
    IntegrationProvider,
    ProviderAction,
    Response,
    Retry,
    WorkflowRecipe,
    playbook_blueprint,
    register_provider,
)

from .multipart import encode_multipart
from .text import URL_PLACEHOLDER, count_characters, html_to_text

NETWORK = "mastodon"
CAPABILITY = "social.post"
CAP_COMMENTS = "social.comments"
CAP_REPLY = "social.reply"
CAP_LIKE = "social.like"
CAP_REPOST = "social.repost"
CAP_MESSAGE = "social.message"
CAP_DELETE_COMMENT = "social.delete_comment"

# Mastodon's own defaults, used when the instance doesn't say (or, on a dry run, can't be reached).
DEFAULT_MAX_CHARACTERS = 500
DEFAULT_MAX_MEDIA = 4
DEFAULT_DESCRIPTION_LIMIT = 1500
DEFAULT_IMAGE_SIZE_LIMIT = 16 * 1024 * 1024
CONTRACT_MAX_IMAGES = 10  # what social.post allows at all, before any network's own limit
VISIBILITIES = ("public", "unlisted", "private")  # "direct" is a private mention, not a post
DEFAULT_VISIBILITY = "public"
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_STATUS_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_ACCOUNT_ID = _STATUS_ID
_HANDLE = re.compile(r"^[A-Za-z0-9_.-]+(@[A-Za-z0-9.-]+)?$")  # user or user@instance, without the leading @

# social.comments. Item kinds: a mention on a direct-visibility status is a "message" (a DM, 0.4.0); a mention whose status replies
# to one of the account's own statuses is a "reply"; every other mention is a "mention". Mastodon has no separate "comment" (a
# comment on a post is a reply), so that kind is never produced.
KIND_COMMENT, KIND_REPLY, KIND_MENTION = "comment", "reply", "mention"
KIND_MESSAGE = "message"  # 0.4.0: a mention on a direct-visibility status — a DM, kept apart from public mentions
COMMENT_KINDS = (KIND_COMMENT, KIND_REPLY, KIND_MENTION, KIND_MESSAGE)
DEFAULT_COMMENTS_LIMIT = 50
MAX_COMMENTS_LIMIT = 200
NOTIFICATION_PAGE_SIZE = 40  # Mastodon's default page; its maximum is 80 (verify)
MAX_NOTIFICATION_PAGES = 25  # a poll that finds more than this many new mentions picks the rest up next time

# A freshly uploaded video/audio is processed in the background (202); small images are ready at once on 4.0+.
MEDIA_POLL_ATTEMPTS = 10
MEDIA_POLL_SECONDS = 2
DOWNLOAD_TIMEOUT = 30
UPLOAD_TIMEOUT = 60
ERROR_TEXT_LIMIT = 300

# The stable codes a failure carries (MastodonError.code) and how Marvin handles each. The provider never retries or
# alerts by itself. A retried post is safe: the status goes out under a content-derived Idempotency-Key.
CODE_REFUSED = "refused"  # Mastodon can't take this post (over the limit, too many images, a 422): change the post
CODE_AUTH = "auth"  # no token, or Mastodon rejected it / its scopes (401/403): fix the connection
CODE_UNSUPPORTED = "unsupported"  # the network can't do what was asked (e.g. delete someone else's reply)
CODE_CONFIG = "config"  # the connection's settings are unusable (no server, not https, bad visibility)
CODE_RATE_LIMITED = "rate_limited"  # 429 (its Retry-After becomes retry_after)
CODE_UNAVAILABLE = "unavailable"  # the server couldn't be reached, timed out or failed on its side (5xx)
CODE_UNKNOWN = "unknown"
ERROR_POLICY: ErrorPolicy = {
    CODE_UNAVAILABLE: Handle(retry=Retry(backoff=(60, 300, 900)), then=Handle(notify=True)),
    CODE_RATE_LIMITED: Handle(retry=Retry(backoff=(60, 300, 900)), then=Handle(notify=True)),
    CODE_AUTH: Handle(notify=True, retry=Retry(backoff=(), on_recovery=True)),
    "*": Handle(),
}


class MastodonError(IntegrationError):
    """A readable failure with a stable ``code``. An ``IntegrationError`` (so a ValueError): an older Marvin fails the
    step with the message, and a workflow's on-failure steps read the code as ``${error.code}``."""

    def __init__(self, message: str, code: str = CODE_UNKNOWN, *, retry_after: float | None = None) -> None:
        super().__init__(message, code=code, retry_after=retry_after)


def _header(resp: Response, name: str) -> str:
    return next((str(v).strip() for k, v in (resp.headers or {}).items() if k.lower() == name.lower()), "")


def _retry_after(resp: Response) -> float | None:
    """Seconds from ``Retry-After`` (delta-seconds or an HTTP date) or Mastodon's ``X-RateLimit-Reset`` (ISO time)."""
    value = _header(resp, "retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                return max(0.0, (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    value = _header(resp, "x-ratelimit-reset")
    if value:
        try:
            return max(0.0, datetime.fromisoformat(value).timestamp() - time.time())
        except ValueError:
            pass
    return None


def _detail(resp: Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:ERROR_TEXT_LIMIT]
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])[:ERROR_TEXT_LIMIT]
    return resp.text[:ERROR_TEXT_LIMIT]


def _fail(resp: Response, what: str) -> MastodonError:
    status, detail = resp.status_code, _detail(resp)
    base = f"Mastodon {what} failed: HTTP {status}: {detail}"
    if status == 401:
        return MastodonError(
            f"{base}. The access token was rejected: create a new one (Preferences → Development) and update the connection.", CODE_AUTH
        )
    if status == 403:
        return MastodonError(
            f"{base}. The token may lack a scope (post_status needs write:statuses and write:media; reading comments needs read:notifications, "
            "read:statuses and read:accounts; replies, messages, reposts and deletes need write:statuses; likes need write:favourites), "
            "or the account is limited or suspended.",
            CODE_AUTH,
        )
    if status == 429:
        return MastodonError(f"{base}. Mastodon is rate limiting this account; try again shortly.", CODE_RATE_LIMITED, retry_after=_retry_after(resp))
    if status >= 500:
        return MastodonError(base, CODE_UNAVAILABLE)
    if status == 422:
        return MastodonError(base, CODE_REFUSED)
    return MastodonError(base, CODE_UNKNOWN)


def _positive_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _flag(value) -> bool:
    """A workflow argument as a bool — templates can hand over the strings "true"/"false"."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _sniff_image(data: bytes) -> str | None:
    """The MIME type of the common image formats by their magic bytes, for hosts that serve them as octet-stream."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_STR = {"type": "string"}
_POST_OUTPUT = {
    "type": "object",
    "properties": {
        "posted": {"type": "boolean"},
        "post_id": {"type": ["string", "null"], "description": "The status id."},
        "url": {"type": ["string", "null"], "description": "The status's permalink (its `url`)."},
        "network": {"type": "string", "const": NETWORK},
        "would_post": {"type": "object", "description": "On a dry run: the text, image plan and visibility that would go out."},
    },
    "required": ["posted", "network"],
}


_COMMENT_ITEM = {
    "type": "object",
    "properties": {
        "id": {**_STR, "description": "The mention's status id: pass it to reply, like and delete_comment."},
        "kind": {
            "type": "string",
            "enum": [KIND_REPLY, KIND_MENTION, KIND_MESSAGE],
            "description": "reply: answers one of your statuses. message: a direct status (a DM). mention: anything else.",
        },
        "post_id": {"type": ["string", "null"], "description": "The status this one replies to, if any."},
        "post_url": {"type": ["string", "null"], "description": "Permalink of that status when it is yours."},
        "author": {"type": "object", "properties": {"id": _STR, "handle": {**_STR, "description": "user or user@instance."}, "name": _STR}},
        "text": {**_STR, "description": "The status as plain text."},
        "created_at": {**_STR, "description": "ISO 8601, UTC."},
        "url": {"type": ["string", "null"]},
        "network": {"type": "string", "const": NETWORK},
    },
}
_COMMENTS_OUTPUT = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": _COMMENT_ITEM, "description": "Oldest first."},
        "cursor": {**_STR, "description": "Pass as 'since' next time."},
    },
    "required": ["items", "cursor"],
}


@register_provider
class MastodonProvider(IntegrationProvider):
    slug = "mastodon"
    name = "Mastodon"
    description = (
        "Post to your Mastodon account (text, a link, up to four images with alt text), read mentions and replies, "
        "and reply, favourite, boost, message or delete your own replies — posting and sending after someone approves it."
    )
    category = CATEGORY_DESTINATION
    icon = "🐘"
    logo = "logo.svg"
    post_limits: ClassVar[dict] = {"max": DEFAULT_MAX_CHARACTERS, "unit": "characters", "links": "clickable"}
    """A status: 500 characters by default (a server may allow more; posting checks the server's own number), all of it shown."""

    guide = "guide.md"
    # A Playbook (0.3, SDK 0.15): how to write a post for Mastodon, a skill folder in the package's data. A workspace adds it from the card
    # as a draft Playbook, reads it and publishes it; an older Marvin skips it.
    content = (playbook_blueprint(__package__, "playbooks/mastodon-post"),)
    recipes = (
        WorkflowRecipe(
            id="post-on-publish",
            title="Post to Mastodon when published",
            outcome=(
                "When an entry is published, an editor is asked whether to post it; once approved, its title and summary go to "
                "Mastodon, and the post's link is kept on the entry."
            ),
            category="Social and community",
            tags=("mastodon", "social", "publishing", "approval"),
            definition={
                "trigger": {"type": "event", "event": "entry_published"},
                "conditions": [{"field": "entry.entry_type", "op": "eq", "value": "{{entry_type}}"}],
                "actions": [
                    {
                        "kind": "approval",
                        "id": "ok",
                        "message": "Post “${entry.title}” to Mastodon? Text: ${entry.summary}",
                        "approvers": "editor",
                        "expires_in_hours": 48,
                    },
                    {
                        "kind": "integration",
                        "id": "post",
                        "integration": "{{mastodon_integration}}",
                        "action": "post_status",
                        "args": {"text": "${entry.title}\n\n${entry.summary}", "dry_run": False},
                        "when": {"field": "steps.ok.output.decision", "op": "eq", "value": "approved"},
                    },
                    {
                        "kind": "entry",
                        "op": "set_metadata",
                        "metadata": {"mastodon_post_id": "${steps.post.output.post_id}", "mastodon_url": "${steps.post.output.url}"},
                        "when": {"field": "steps.post.output.posted", "op": "eq", "value": True},
                    },
                ],
            },
            variables={
                "entry_type": {
                    "type": "entry_type_slug",
                    "description": "Entries of this type are offered to Mastodon when published.",
                    "example": "post",
                },
                "mastodon_integration": {
                    "type": "integration_slug",
                    "description": "The Mastodon connection. Its token needs the `write:statuses` and `write:media` permissions.",
                    "example": "mastodon",
                },
            },
            side_effects=(
                {"kind": "external_post", "description": "One public Mastodon post per approved run."},
                {"kind": "content_mutation", "description": "Saves the post's link on the entry."},
            ),
        ),
    )

    credentials = (
        CredentialField(
            key="access_token",
            label="Access token",
            help=(
                "Your access token from Preferences → Development → New application. Posting needs write:statuses and write:media; "
                "reading and answering mentions also needs read:notifications, read:statuses, read:accounts and write:favourites."
            ),
        ),
    )
    config_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "title": "Server", "description": "Your instance, e.g. https://mastodon.social"},
            "visibility": {
                "type": "string",
                "title": "Visibility",
                "enum": list(VISIBILITIES),
                "default": DEFAULT_VISIBILITY,
                "description": "public (everyone, listed), unlisted (everyone, not in public timelines) or private (followers only).",
            },
            "language": {"type": "string", "title": "Language", "description": "Optional ISO 639-1 code for posts, e.g. en."},
        },
        "required": ["server"],
        "additionalProperties": False,
    }

    error_policy: ClassVar[ErrorPolicy] = ERROR_POLICY

    actions = (
        ProviderAction(
            key="post_status",
            label="Post to Mastodon",
            description=(
                "Post a status: text (with the link appended on a new line), up to 4 images with alt text (the instance's own limit). "
                "Public and not quietly undoable, so a person approves it first. A dry run reports what would go out without uploading or posting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        **_STR,
                        "description": "The status text. Counted the Mastodon way: a URL is 23 characters; the instance's limit applies.",
                    },
                    "images": {
                        "type": "array",
                        "items": _STR,
                        "maxItems": CONTRACT_MAX_IMAGES,
                        "description": "Public https image URLs (0–10; Mastodon takes up to the instance's limit, 4 by default).",
                    },
                    "alt": {"type": "array", "items": _STR, "description": "Alt text, one per image, in the same order (optional)."},
                    "link": {**_STR, "description": "A URL appended to the text on a new line unless it is already in it."},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            capability=CAPABILITY,
            output_schema=_POST_OUTPUT,
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="delete_status",
            label="Delete a status",
            description="Delete one of your own statuses by id (the post_id a post returned). Not undoable, so a person approves it first.",
            input_schema={
                "type": "object",
                "properties": {"id": {**_STR, "description": "The status id."}},
                "required": ["id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"deleted": {"type": "boolean"}, "id": _STR, "network": {"type": "string", "const": NETWORK}},
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="comments",
            label="Read mentions and replies",
            description=(
                "Read the mentions of your account (Mastodon has no separate comments: a reply to one of your statuses arrives as a mention) as "
                "normalised items, oldest first. A mention that replies to one of your own statuses is kind 'reply'; any other is 'mention'. "
                "Pass back the cursor from the last call as 'since' to get only what is new."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        **_STR,
                        "description": "The cursor this action returned last time (opaque). Omit it on a first run: you get the newest items.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_COMMENTS_LIMIT, "default": DEFAULT_COMMENTS_LIMIT},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(COMMENT_KINDS)},
                        "description": "Only these kinds (default: all).",
                    },
                },
                "additionalProperties": False,
            },
            capability=CAP_COMMENTS,
            output_schema=_COMMENTS_OUTPUT,
            cost_hint="free",
        ),
        ProviderAction(
            key="reply",
            label="Reply to a mention",
            description=(
                "Reply to a mention or reply with a status that mentions its author. Private (the default) is a direct status only they see; "
                "private=false posts a public reply in the thread. A dry run reports what would go out without posting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "comment_id": {**_STR, "description": "The id of the item to answer (an item's id from comments)."},
                    "text": {**_STR, "description": "The reply. The author's @handle is put in front unless it is already in the text."},
                    "private": {"type": "boolean", "default": True},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["comment_id", "text"],
                "additionalProperties": False,
            },
            capability=CAP_REPLY,
            output_schema={
                "type": "object",
                "properties": {
                    "replied": {"type": "boolean"},
                    "reply_id": {"type": ["string", "null"]},
                    "private": {"type": "boolean"},
                    "url": {"type": ["string", "null"]},
                    "network": {"type": "string", "const": NETWORK},
                    "would_reply": {"type": "object", "description": "On a dry run: the text and visibility that would go out."},
                },
                "required": ["replied", "network"],
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="like",
            label="Favourite a mention or reply",
            description="Favourite (like) a status by id. Easily undone, so it needs no approval.",
            input_schema={
                "type": "object",
                "properties": {"comment_id": {**_STR, "description": "The status id."}},
                "required": ["comment_id"],
                "additionalProperties": False,
            },
            capability=CAP_LIKE,
            output_schema={"type": "object", "properties": {"liked": {"type": "boolean"}, "network": {"type": "string", "const": NETWORK}}},
            cost_hint="free",
        ),
        ProviderAction(
            key="repost",
            label="Boost a status",
            description="Boost (repost) a status by id to your followers. Public, so a person approves it first.",
            input_schema={
                "type": "object",
                "properties": {"post_id": {**_STR, "description": "The status id to boost."}},
                "required": ["post_id"],
                "additionalProperties": False,
            },
            capability=CAP_REPOST,
            output_schema={
                "type": "object",
                "properties": {
                    "reposted": {"type": "boolean"},
                    "url": {"type": ["string", "null"], "description": "The boosted post's permalink."},
                    "network": {"type": "string", "const": NETWORK},
                },
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="message",
            label="Send a direct message",
            description=(
                "Send a direct status (visible only to the person mentioned) to an account given by handle or id. "
                "A dry run reports what would go out without sending."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "object",
                        "properties": {
                            "id": {**_STR, "description": "The account id (its handle is looked up)."},
                            "handle": {**_STR, "description": "user or user@instance, with or without the leading @."},
                        },
                        "additionalProperties": False,
                    },
                    "text": {**_STR, "description": "The message. The @handle is put in front unless it is already in the text."},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["to", "text"],
                "additionalProperties": False,
            },
            capability=CAP_MESSAGE,
            output_schema={
                "type": "object",
                "properties": {
                    "sent": {"type": "boolean"},
                    "message_id": {"type": ["string", "null"]},
                    "network": {"type": "string", "const": NETWORK},
                    "would_send": {"type": "object", "description": "On a dry run: the text and recipient that would go out."},
                },
                "required": ["sent", "network"],
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="delete_comment",
            label="Delete one of your replies",
            description=(
                "Delete one of your own statuses (such as a reply) by id. Mastodon can't delete other people's replies: that is refused "
                "with code 'unsupported'. Not undoable, so a person approves it first."
            ),
            input_schema={
                "type": "object",
                "properties": {"comment_id": {**_STR, "description": "The id of your own status."}},
                "required": ["comment_id"],
                "additionalProperties": False,
            },
            capability=CAP_DELETE_COMMENT,
            output_schema={"type": "object", "properties": {"deleted": {"type": "boolean"}, "network": {"type": "string", "const": NETWORK}}},
            cost_hint="free",
            requires_approval=True,
        ),
    )

    # ---- plumbing ---------------------------------------------------------------------------
    _sleep = staticmethod(time.sleep)  # patched in tests

    @staticmethod
    def _cfg(ctx: IntegrationContext) -> dict:
        return ctx.config or {}

    def _server(self, ctx: IntegrationContext) -> str:
        """The instance's base URL, https only (the token travels in every request)."""
        raw = str(self._cfg(ctx).get("server") or "").strip()
        if not raw:
            raise MastodonError("No Mastodon server configured: set Server to your instance, e.g. https://mastodon.social.", CODE_CONFIG)
        parts = urlsplit(raw if "://" in raw else f"https://{raw}")
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.path.strip("/") or parts.query or parts.fragment:
            raise MastodonError(f"The Mastodon server must be a plain https address like https://mastodon.social (got {raw!r}).", CODE_CONFIG)
        return f"https://{parts.netloc.lower()}"

    def _visibility(self, ctx: IntegrationContext) -> str:
        value = str(self._cfg(ctx).get("visibility") or "").strip().lower() or DEFAULT_VISIBILITY
        if value not in VISIBILITIES:
            raise MastodonError(f"Visibility must be one of {', '.join(VISIBILITIES)} (got {value!r}).", CODE_CONFIG)
        return value

    def _language(self, ctx: IntegrationContext) -> str | None:
        value = str(self._cfg(ctx).get("language") or "").strip()
        if value and not _LANGUAGE.match(value):
            raise MastodonError(f"Language must be an ISO 639-1 code such as 'en' (got {value!r}).", CODE_CONFIG)
        return value.lower() or None

    @staticmethod
    def _require_secret(ctx: IntegrationContext) -> None:
        if not ctx.secret:
            raise MastodonError("No Mastodon access token configured.", CODE_AUTH)

    @staticmethod
    def _auth(ctx: IntegrationContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.secret}"}

    def _request(
        self, ctx: IntegrationContext, method: str, path: str, *, what: str, ok: tuple[int, ...] = (200,), headers=None, **kwargs
    ) -> Response:
        """One call to the instance with the token. Anything outside ``ok`` raises a MastodonError."""
        server = self._server(ctx)
        try:
            resp = getattr(ctx.http, method)(f"{server}{path}", headers={**self._auth(ctx), **(headers or {})}, **kwargs)
        except Exception as e:  # the SDK client raises its own errors for timeouts and blocked hosts
            raise MastodonError(f"Could not reach {server} ({what}): {e}", CODE_UNAVAILABLE) from e
        if resp.status_code not in ok:
            raise _fail(resp, what)
        return resp

    @staticmethod
    def _json(resp: Response, what: str, expect: type = dict):
        try:
            data = resp.json()
        except ValueError as e:
            raise MastodonError(f"Mastodon {what} returned something that isn't JSON: {resp.text[:100]!r}", CODE_UNKNOWN) from e
        if not isinstance(data, expect):
            raise MastodonError(f"Mastodon {what} returned an unexpected body.", CODE_UNKNOWN)
        return data

    # ---- lifecycle --------------------------------------------------------------------------

    def check(self, ctx: IntegrationContext) -> tuple[str, str | None]:
        if not ctx.secret:
            return ("unconfigured", "Missing access token.")
        if not self._cfg(ctx).get("server"):
            return ("unconfigured", "Missing server.")
        try:
            resp = self._request(ctx, "get", "/api/v1/accounts/verify_credentials", what="credential check", ok=(200, 403))
        except IntegrationError as e:
            return ("error", str(e))
        # verify_credentials wants the profile / read:accounts scope. A token with only write scopes is answered 403 — but a
        # 403 proves the token authenticated (a bad or revoked one is a 401), so the connection is fine for posting.
        return ("ok", None) if resp.status_code in (200, 403) else ("error", f"HTTP {resp.status_code}")

    def run_action(self, key: str, args: dict, ctx: IntegrationContext) -> dict:
        handler = {
            "post_status": self._action_post_status,
            "delete_status": self._action_delete_status,
            "comments": self._action_comments,
            "reply": self._action_reply,
            "like": self._action_like,
            "repost": self._action_repost,
            "message": self._action_message,
            "delete_comment": self._action_delete_comment,
        }.get(key)
        if handler is None:
            raise NotImplementedError(f"mastodon has no action '{key}'")
        self._require_secret(ctx)
        return handler(args or {}, ctx)

    # ---- the instance's limits ------------------------------------------------------------------

    def _limits(self, ctx: IntegrationContext, *, tolerate_failure: bool) -> dict:
        """What this instance allows, from ``GET /api/v2/instance``; Mastodon's defaults for whatever it doesn't say.

        A dry run may fall back to the defaults when the instance can't be read; a real post may not (it would be guessing).
        A 404 means a server without the v2 endpoint (pre-4.0 Mastodon, some forks): the defaults apply there too."""
        try:
            resp = self._request(ctx, "get", "/api/v2/instance", what="instance info", ok=(200, 404))
            data = self._json(resp, "instance info") if resp.status_code == 200 else {}
            source = "instance" if resp.status_code == 200 else "default"
        except IntegrationError as e:
            if not tolerate_failure:
                raise
            ctx.logger.info("mastodon: instance limits unavailable, using defaults: %s", e)
            data, source = {}, "default"
        config = data.get("configuration") or {}
        statuses, media = config.get("statuses") or {}, config.get("media_attachments") or {}
        return {
            "max_characters": _positive_int(statuses.get("max_characters"), DEFAULT_MAX_CHARACTERS),
            "max_media": _positive_int(statuses.get("max_media_attachments"), DEFAULT_MAX_MEDIA),
            "url_length": _positive_int(statuses.get("characters_reserved_per_url"), URL_PLACEHOLDER),
            "description_limit": _positive_int(media.get("description_limit"), DEFAULT_DESCRIPTION_LIMIT),
            "image_size_limit": _positive_int(media.get("image_size_limit"), DEFAULT_IMAGE_SIZE_LIMIT),
            "source": source,
        }

    def _create_status(
        self,
        ctx: IntegrationContext,
        text: str,
        visibility: str,
        language: str | None,
        *,
        media_ids: list[str] | None = None,
        key_media: list | None = None,
        in_reply_to: str | None = None,
        what: str,
    ) -> dict:
        """``POST /api/v1/statuses`` under an Idempotency-Key made from the content, so a retry returns the first status."""
        form: list[tuple[str, str]] = [("status", text), ("visibility", visibility)]
        form += [("media_ids[]", mid) for mid in media_ids or []]
        if language:
            form.append(("language", language))
        fields = {"status": text, "visibility": visibility, "language": language}
        if in_reply_to:
            form.append(("in_reply_to_id", in_reply_to))
            fields["in_reply_to_id"] = in_reply_to
        key = self._idempotency_key(ctx, self._server(ctx), fields, key_media or [])
        resp = self._request(
            ctx,
            "post",
            "/api/v1/statuses",
            what=what,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Idempotency-Key": key},
            data=urlencode(form).encode(),
        )
        status = self._json(resp, what)
        if not status.get("id"):
            raise MastodonError(f"Mastodon accepted the {what} but returned no status id.", CODE_UNKNOWN)
        return status

    # ---- post_status ----------------------------------------------------------------------------

    @staticmethod
    def _parse_post(args: dict) -> dict:
        """Validate the social.post arguments (no network): ``{text, images, alts, link, dry_run}``."""
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise MastodonError("post_status needs 'text' (a non-empty string).", CODE_REFUSED)
        images = args.get("images") or []
        if not isinstance(images, list) or any(not isinstance(u, str) or not u.strip() for u in images):
            raise MastodonError("'images' must be a list of public https image URLs.", CODE_REFUSED)
        images = [u.strip() for u in images]
        if len(images) > CONTRACT_MAX_IMAGES:
            raise MastodonError(f"social.post takes at most {CONTRACT_MAX_IMAGES} images ({len(images)} given).", CODE_REFUSED)
        if any(not u.startswith("https://") for u in images):
            raise MastodonError("Every image URL must be public https; Marvin downloads it and uploads it to Mastodon.", CODE_REFUSED)
        alts = args.get("alt") or []
        if not isinstance(alts, list) or any(a is not None and not isinstance(a, str) for a in alts):
            raise MastodonError("'alt' must be a list of strings, one per image.", CODE_REFUSED)
        if len(alts) > len(images):
            raise MastodonError(f"'alt' has {len(alts)} entries for {len(images)} images; give one per image.", CODE_REFUSED)
        alts = [(a or "").strip() for a in alts] + [""] * (len(images) - len(alts))
        link = args.get("link")
        if link is not None and not isinstance(link, str):
            raise MastodonError("'link' must be a URL string.", CODE_REFUSED)
        link = (link or "").strip() or None
        if link and not re.match(r"^https?://\S+$", link):
            raise MastodonError(f"'link' must be an http(s) URL (got {link!r}).", CODE_REFUSED)
        return {"text": text.strip(), "images": images, "alts": alts, "link": link, "dry_run": _flag(args.get("dry_run", False))}

    @staticmethod
    def _check_length(text: str, limits: dict, server: str, *, hint: str, appended: bool = False) -> None:
        """Refuse a status the instance would answer with a 422: the message says how it was counted and how to fix it."""
        used, limit = count_characters(text, limits["url_length"]), limits["max_characters"]
        if used > limit:
            raise MastodonError(
                f"The post is {used} characters but {server} allows {limit} (a URL counts as {limits['url_length']}"
                f"{', and the link was appended' if appended else ''}). {hint}",
                CODE_REFUSED,
            )

    def _compose(self, post: dict, limits: dict, server: str) -> str:
        """The status text with the link appended on a new line (Mastodon builds its own preview card), checked against the
        instance's limits. Mastodon can't take what is refused here: the message says so and how to fix it."""
        text, link = post["text"], post["link"]
        if link and link not in text:
            text = f"{text}\n{link}"
        self._check_length(
            text,
            limits,
            server,
            appended=bool(link and link not in post["text"]),
            hint='Give Mastodon a shorter text with per_network, e.g. {"mastodon": {"text": "…"}}.',
        )
        if len(post["images"]) > limits["max_media"]:
            raise MastodonError(
                f"Mastodon on {server} takes at most {limits['max_media']} images per post ({len(post['images'])} given). "
                'Give Mastodon fewer images with per_network, e.g. {"mastodon": {"images": [...]}}.',
                CODE_REFUSED,
            )
        too_long = [i + 1 for i, alt in enumerate(post["alts"]) if len(alt) > limits["description_limit"]]
        if too_long:
            raise MastodonError(
                f"The alt text of image {', '.join(map(str, too_long))} is over {server}'s {limits['description_limit']}-character limit.",
                CODE_REFUSED,
            )
        return text

    def _fetch_image(self, ctx: IntegrationContext, url: str, size_limit: int) -> tuple[bytes, str, str]:
        """Download one image through ``ctx.http`` (SSRF-guarded in Marvin). No Authorization header: the token is for the
        Mastodon server only, never for the host an image comes from. Returns ``(bytes, content_type, filename)``."""
        try:
            resp = ctx.http.get(url, timeout=DOWNLOAD_TIMEOUT)
        except Exception as e:
            raise MastodonError(f"Could not download the image {url}: {e}", CODE_UNKNOWN) from e
        if not resp.ok:
            raise MastodonError(
                f"Could not download the image {url}: HTTP {resp.status_code}.", CODE_UNAVAILABLE if resp.status_code >= 500 else CODE_UNKNOWN
            )
        data = resp.content
        if not data:
            raise MastodonError(f"The image {url} is empty.", CODE_REFUSED)
        if len(data) > size_limit:
            raise MastodonError(f"The image {url} is {len(data) // 1024} KB; the instance's limit is {size_limit // 1024} KB.", CODE_REFUSED)
        content_type = _header(resp, "content-type").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            # Some hosts serve images as octet-stream; trust the bytes, not the label, but never upload a web page.
            content_type = _sniff_image(data) or ""
        if not content_type:
            raise MastodonError(f"{url} did not return an image.", CODE_REFUSED)
        name = urlsplit(url).path.rsplit("/", 1)[-1] or "image"
        if "." not in name:
            name += mimetypes.guess_extension(content_type) or ""
        return data, content_type, name

    def _wait_processed(self, ctx: IntegrationContext, media_id: str) -> None:
        """Poll ``GET /api/v1/media/:id`` until Mastodon has finished processing: 206 = still working, 200 = ready."""
        for attempt in range(MEDIA_POLL_ATTEMPTS):
            resp = self._request(ctx, "get", f"/api/v1/media/{quote(media_id, safe='')}", what="image processing check", ok=(200, 206))
            if resp.status_code == 200:
                return
            if attempt < MEDIA_POLL_ATTEMPTS - 1:
                self._sleep(MEDIA_POLL_SECONDS)
        raise MastodonError(
            f"Mastodon is still processing an uploaded image after {MEDIA_POLL_ATTEMPTS} checks; nothing was posted. Try again in a minute.",
            CODE_UNAVAILABLE,
        )

    def _upload_image(self, ctx: IntegrationContext, url: str, alt: str, size_limit: int) -> str:
        data, content_type, filename = self._fetch_image(ctx, url, size_limit)
        body, header = encode_multipart(
            {"description": alt} if alt else {}, file_field="file", filename=filename, content_type=content_type, data=data
        )
        resp = self._request(
            ctx, "post", "/api/v2/media", what="image upload", ok=(200, 202), headers={"Content-Type": header}, data=body, timeout=UPLOAD_TIMEOUT
        )
        media_id = str(self._json(resp, "image upload").get("id") or "")
        if not media_id:
            raise MastodonError("Mastodon accepted the image but returned no media id.", CODE_UNKNOWN)
        if resp.status_code == 202:
            self._wait_processed(ctx, media_id)
        return media_id

    @staticmethod
    def _idempotency_key(ctx: IntegrationContext, server: str, fields: dict, images: list[tuple[str, str]]) -> str:
        """Derived from the content (and the retry chain's seed when core supplies one), so the same call sent twice is one
        post: Mastodon returns the first status for a repeated key (it keeps keys for an hour)."""
        material = json.dumps([ctx.idempotency_seed or "", server, fields, images], sort_keys=True, ensure_ascii=False)
        return f"marvin-{hashlib.sha256(material.encode()).hexdigest()[:48]}"

    def _action_post_status(self, args: dict, ctx: IntegrationContext) -> dict:
        post = self._parse_post(args)
        server, visibility, language = self._server(ctx), self._visibility(ctx), self._language(ctx)
        limits = self._limits(ctx, tolerate_failure=post["dry_run"])
        text = self._compose(post, limits, server)
        media = list(zip(post["images"], post["alts"], strict=True))
        if post["dry_run"]:
            return {
                "posted": False,
                "dry_run": True,
                "post_id": None,
                "url": None,
                "network": NETWORK,
                "would_post": {
                    "server": server,
                    "text": text,
                    "characters": count_characters(text, limits["url_length"]),
                    "limit": limits["max_characters"],
                    "limits_from": limits["source"],
                    "visibility": visibility,
                    "language": language,
                    "media": [{"url": url, "alt": alt or None} for url, alt in media],
                },
            }

        media_ids = [self._upload_image(ctx, url, alt, limits["image_size_limit"]) for url, alt in media]
        status = self._create_status(ctx, text, visibility, language, media_ids=media_ids, key_media=media, what="post")
        return {
            "posted": True,
            "dry_run": False,
            "post_id": str(status["id"]),
            "url": status.get("url"),
            "network": NETWORK,
            "visibility": status.get("visibility") or visibility,
            "images": len(media_ids),
        }

    # ---- delete_status ------------------------------------------------------------------------------

    def _action_delete_status(self, args: dict, ctx: IntegrationContext) -> dict:
        status_id = str(args.get("id") or "").strip()
        if not _STATUS_ID.match(status_id):
            raise MastodonError("delete_status needs 'id', the status id a post returned as post_id.", CODE_REFUSED)
        self._request(ctx, "delete", f"/api/v1/statuses/{status_id}", what="delete")
        return {"deleted": True, "id": status_id, "network": NETWORK}

    # ---- engagement: shared helpers -------------------------------------------------------------------

    def _get(self, ctx: IntegrationContext, path: str, what: str, expect: type = dict):
        return self._json(self._request(ctx, "get", path, what=what), what, expect)

    @staticmethod
    def _id_arg(args: dict, name: str, action: str) -> str:
        value = str(args.get(name) or "").strip()
        if not _STATUS_ID.match(value):
            raise MastodonError(f"{action} needs '{name}', a Mastodon status id.", CODE_REFUSED)
        return value

    @staticmethod
    def _text_arg(args: dict, action: str) -> str:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise MastodonError(f"{action} needs 'text' (a non-empty string).", CODE_REFUSED)
        return text.strip()

    def _me(self, ctx: IntegrationContext) -> dict:
        """The connected account (``verify_credentials``, which needs read:accounts): its id tells our own statuses from others'."""
        me = self._get(ctx, "/api/v1/accounts/verify_credentials", "account lookup")
        if not me.get("id"):
            raise MastodonError("Mastodon returned no account id for this token.", CODE_UNKNOWN)
        return me

    def _status(self, ctx: IntegrationContext, status_id: str) -> dict:
        return self._get(ctx, f"/api/v1/statuses/{quote(status_id, safe='')}", "status lookup")

    @staticmethod
    def _with_mention(text: str, acct: str) -> str:
        """``text`` with ``@acct`` in front unless it already mentions that account (a local ``@user`` is not ``@user@elsewhere``)."""
        if re.search(rf"(?<![\w@])@{re.escape(acct)}(?![\w@-])", text, re.IGNORECASE):
            return text
        return f"@{acct} {text}"

    # ---- comments (social.comments) ---------------------------------------------------------------------------

    @staticmethod
    def _parse_comments(args: dict) -> tuple[str | None, int, tuple[str, ...]]:
        since = args.get("since")
        if since is not None and (not isinstance(since, str) or (since and not _STATUS_ID.match(since))):
            raise MastodonError("'since' must be the cursor this plugin returned last time.", CODE_REFUSED)
        raw = args.get("limit")
        try:
            limit = DEFAULT_COMMENTS_LIMIT if raw is None else int(raw)
        except (TypeError, ValueError):
            limit = 0
        if not 1 <= limit <= MAX_COMMENTS_LIMIT:
            raise MastodonError(f"'limit' must be a whole number from 1 to {MAX_COMMENTS_LIMIT} (default {DEFAULT_COMMENTS_LIMIT}).", CODE_REFUSED)
        kinds = args.get("kinds")
        if kinds is None:
            kinds = COMMENT_KINDS
        if not isinstance(kinds, (list, tuple)) or any(k not in COMMENT_KINDS for k in kinds):
            raise MastodonError(f"'kinds' must be a list drawn from {', '.join(COMMENT_KINDS)}.", CODE_REFUSED)
        return since or None, limit, tuple(kinds)

    @staticmethod
    def _id_key(value) -> tuple[int, str]:
        text = str(value)
        return (int(text), text) if text.isdigit() else (-1, text)

    def _fetch_mentions(self, ctx: IntegrationContext, since: str | None, limit: int) -> list[dict]:
        """Mention notifications newer than ``since``, newest first within each page: ``GET /api/v1/notifications`` pages back
        with ``max_id`` until an empty page. Without a cursor it stops once ``limit`` are in hand (the newest ones; a first run
        should not replay a whole history)."""
        found: dict[str, dict] = {}
        max_id: str | None = None
        for _ in range(MAX_NOTIFICATION_PAGES):
            params = [("types[]", "mention"), ("limit", str(NOTIFICATION_PAGE_SIZE))]
            if since:
                params.append(("since_id", since))
            if max_id:
                params.append(("max_id", max_id))
            page = self._get(ctx, f"/api/v1/notifications?{urlencode(params)}", "notifications", list)
            page = [n for n in page if isinstance(n, dict) and n.get("id") is not None]
            if not page:
                break
            for note in page:
                found[str(note["id"])] = note
            oldest = min((str(n["id"]) for n in page), key=self._id_key)
            if oldest == max_id:  # the server ignored max_id: don't loop forever
                break
            max_id = oldest
            if not since and len(found) >= limit:
                break
        else:
            ctx.logger.warning(
                "mastodon: more than %d new mentions; the rest are picked up on the next poll", MAX_NOTIFICATION_PAGES * NOTIFICATION_PAGE_SIZE
            )
        return sorted(found.values(), key=lambda n: self._id_key(n["id"]))

    @staticmethod
    def _iso_utc(value) -> str | None:
        try:
            when = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value) if value else None
        return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(UTC).isoformat()

    def _comment(self, note: dict, me: dict, server: str) -> dict | None:
        """One mention notification as a social.comments item (None when its status is gone)."""
        status = note.get("status")
        if not isinstance(status, dict) or not status.get("id"):
            return None
        author = status.get("account") or note.get("account") or {}
        parent = str(status["in_reply_to_id"]) if status.get("in_reply_to_id") else None
        to_us = parent is not None and str(status.get("in_reply_to_account_id") or "") == str(me["id"])
        direct = status.get("visibility") == "direct"  # a DM: only the people it mentions see it
        # The permalink of one of our own (local) statuses is {server}/@{username}/{id} (verify); a parent that is someone else's has no known URL.
        post_url = f"{server}/@{me.get('username') or me.get('acct')}/{parent}" if to_us else None
        return {
            "id": str(status["id"]),
            "kind": KIND_MESSAGE if direct else KIND_REPLY if to_us else KIND_MENTION,
            "post_id": None if direct else parent,
            "post_url": None if direct else post_url,
            "author": {
                "id": str(author.get("id") or ""),
                "handle": author.get("acct") or author.get("username") or "",
                "name": author.get("display_name") or author.get("username") or author.get("acct") or "",
            },
            "text": html_to_text(status.get("content")),
            "created_at": self._iso_utc(status.get("created_at") or note.get("created_at")),
            "url": status.get("url") or status.get("uri"),
            "network": NETWORK,
        }

    def _action_comments(self, args: dict, ctx: IntegrationContext) -> dict:
        since, limit, kinds = self._parse_comments(args)
        notes = self._fetch_mentions(ctx, since, limit)
        if not notes:
            return {"items": [], "cursor": since or ""}
        server, me = self._server(ctx), self._me(ctx)
        entries = [(str(n["id"]), self._comment(n, me, server)) for n in notes]
        entries = [(nid, item) for nid, item in entries if item and item["kind"] in kinds]
        newest = str(notes[-1]["id"])
        if since is None:  # no cursor: the newest ``limit``, oldest first
            kept = entries[-limit:]
            cursor = newest
        elif len(entries) > limit:  # more waiting: stop after the last one handed over, the next poll starts there
            kept = entries[:limit]
            cursor = kept[-1][0]
        else:
            kept, cursor = entries, newest
        return {"items": [item for _, item in kept], "cursor": cursor}

    # ---- reply (social.reply) -------------------------------------------------------------------------------

    def _action_reply(self, args: dict, ctx: IntegrationContext) -> dict:
        comment_id = self._id_arg(args, "comment_id", "reply")
        text = self._text_arg(args, "reply")
        private = True if args.get("private") is None else _flag(args["private"])
        dry_run = _flag(args.get("dry_run", False))
        server, configured, language = self._server(ctx), self._visibility(ctx), self._language(ctx)
        limits = self._limits(ctx, tolerate_failure=dry_run)
        acct = str((self._status(ctx, comment_id).get("account") or {}).get("acct") or "")
        if not acct:
            raise MastodonError(f"Mastodon returned no author for status {comment_id}, so the reply can't mention them.", CODE_UNKNOWN)
        # A private reply is a direct status (only the people it mentions see it); a public one is public, or unlisted if the
        # connection is set to unlisted. Followers-only ("private" in the connection) is not a reply mode here.
        visibility = "direct" if private else ("unlisted" if configured == "unlisted" else "public")
        text = self._with_mention(text, acct)
        self._check_length(text, limits, server, hint="Shorten the reply.")
        if dry_run:
            return {
                "replied": False,
                "dry_run": True,
                "reply_id": None,
                "private": private,
                "url": None,
                "network": NETWORK,
                "would_reply": {
                    "server": server,
                    "in_reply_to_id": comment_id,
                    "to": acct,
                    "text": text,
                    "characters": count_characters(text, limits["url_length"]),
                    "limit": limits["max_characters"],
                    "limits_from": limits["source"],
                    "visibility": visibility,
                    "language": language,
                },
            }
        status = self._create_status(ctx, text, visibility, language, in_reply_to=comment_id, what="reply")
        return {"replied": True, "dry_run": False, "reply_id": str(status["id"]), "private": private, "url": status.get("url"), "network": NETWORK}

    # ---- like / repost (social.like, social.repost) --------------------------------------------------------------

    def _action_like(self, args: dict, ctx: IntegrationContext) -> dict:
        comment_id = self._id_arg(args, "comment_id", "like")
        self._request(ctx, "post", f"/api/v1/statuses/{comment_id}/favourite", what="like")
        return {"liked": True, "network": NETWORK}

    def _action_repost(self, args: dict, ctx: IntegrationContext) -> dict:
        post_id = self._id_arg(args, "post_id", "repost")
        resp = self._request(ctx, "post", f"/api/v1/statuses/{post_id}/reblog", what="repost")
        status = self._json(resp, "repost")
        # The response is the new boost wrapping the original in "reblog"; the boosted post's permalink is the useful one (verify).
        url = (status.get("reblog") or {}).get("url") or status.get("url")
        return {"reposted": True, "url": url, "network": NETWORK}

    # ---- message (social.message) ------------------------------------------------------------------------------

    def _action_message(self, args: dict, ctx: IntegrationContext) -> dict:
        to = args.get("to")
        if not isinstance(to, dict):
            raise MastodonError("message needs 'to': {id} or {handle}.", CODE_REFUSED)
        text = self._text_arg(args, "message")
        dry_run = _flag(args.get("dry_run", False))
        handle = str(to.get("handle") or "").strip().removeprefix("@")
        account_id = str(to.get("id") or "").strip()
        if handle and not _HANDLE.match(handle):
            raise MastodonError(f"'to.handle' must look like user or user@instance (got {handle!r}).", CODE_REFUSED)
        if not handle:
            if not _ACCOUNT_ID.match(account_id):
                raise MastodonError("message needs 'to.handle' or 'to.id' (a Mastodon account id).", CODE_REFUSED)
            handle = str(self._get(ctx, f"/api/v1/accounts/{account_id}", "account lookup").get("acct") or "")
            if not handle:
                raise MastodonError(f"Mastodon returned no handle for account {account_id}.", CODE_UNKNOWN)
        server, language = self._server(ctx), self._language(ctx)
        limits = self._limits(ctx, tolerate_failure=dry_run)
        text = self._with_mention(text, handle)
        self._check_length(text, limits, server, hint="Shorten the message.")
        if dry_run:
            return {
                "sent": False,
                "dry_run": True,
                "message_id": None,
                "network": NETWORK,
                "would_send": {
                    "server": server,
                    "to": handle,
                    "text": text,
                    "characters": count_characters(text, limits["url_length"]),
                    "limit": limits["max_characters"],
                    "limits_from": limits["source"],
                    "visibility": "direct",
                    "language": language,
                },
            }
        status = self._create_status(ctx, text, "direct", language, what="message")
        return {"sent": True, "dry_run": False, "message_id": str(status["id"]), "network": NETWORK}

    # ---- delete_comment (social.delete_comment) ----------------------------------------------------------------

    def _action_delete_comment(self, args: dict, ctx: IntegrationContext) -> dict:
        comment_id = self._id_arg(args, "comment_id", "delete_comment")
        author = str((self._status(ctx, comment_id).get("account") or {}).get("id") or "")
        if not author or author != str(self._me(ctx)["id"]):
            raise MastodonError("Mastodon can delete only your own replies: that status belongs to someone else.", CODE_UNSUPPORTED)
        self._request(ctx, "delete", f"/api/v1/statuses/{comment_id}", what="delete")
        return {"deleted": True, "network": NETWORK}
