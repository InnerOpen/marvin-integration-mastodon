"""Mastodon provider: post a status (text, a link, up to a few images with alt text) through ``social.post``.

Marvin's shared ``social.post`` capability gives every connected network one argument shape; ``post_status`` maps it
onto Mastodon's REST API: the instance's limits are read from ``GET /api/v2/instance``, each image is downloaded and
uploaded with ``POST /api/v2/media`` (waiting for Mastodon to finish processing it when it answers 202), then the
status goes out with ``POST /api/v1/statuses`` under an ``Idempotency-Key`` so a retried call can't double-post.

The provider is pure with respect to Marvin: it gets ``config``, the resolved ``secret`` (the access token), a ``logger``
and a safe ``http`` client via ``ctx`` and returns dicts. It never touches the database; whether a post may go out is
Marvin's approval step, not the provider's business (the action just declares ``requires_approval``).
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
    register_provider,
)

from .multipart import encode_multipart
from .text import URL_PLACEHOLDER, count_characters

NETWORK = "mastodon"
CAPABILITY = "social.post"

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
        return MastodonError(f"{base}. The access token was rejected: create a new one (Preferences → Development) and update the connection.", CODE_AUTH)
    if status == 403:
        return MastodonError(
            f"{base}. The token may lack a scope (it needs write:statuses and write:media), or the account is limited or suspended.", CODE_AUTH
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


@register_provider
class MastodonProvider(IntegrationProvider):
    slug = "mastodon"
    name = "Mastodon"
    description = "Post a status to your Mastodon account — text, a link and up to four images with alt text — after someone approves it."
    category = CATEGORY_DESTINATION
    icon = "🐘"
    logo = "logo.svg"

    guide = "guide.md"
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
                    "description": "The Mastodon connection; its token needs write:statuses and write:media.",
                    "example": "mastodon",
                },
            },
            side_effects=(
                {"kind": "external_post", "description": "One public Mastodon status per approved run."},
                {"kind": "content_mutation", "description": "Sets mastodon_post_id and mastodon_url on the entry."},
            ),
        ),
    )

    credentials = (
        CredentialField(
            key="access_token",
            label="Access token",
            help="Your access token from Preferences → Development → New application, with the write:statuses and write:media scopes.",
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
                    "text": {**_STR, "description": "The status text. Counted the Mastodon way: a URL is 23 characters; the instance's limit applies."},
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

    def _request(self, ctx: IntegrationContext, method: str, path: str, *, what: str, ok: tuple[int, ...] = (200,), headers=None, **kwargs) -> Response:
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
    def _json(resp: Response, what: str) -> dict:
        try:
            data = resp.json()
        except ValueError as e:
            raise MastodonError(f"Mastodon {what} returned something that isn't JSON: {resp.text[:100]!r}", CODE_UNKNOWN) from e
        if not isinstance(data, dict):
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
        handler = {"post_status": self._action_post_status, "delete_status": self._action_delete_status}.get(key)
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

    def _compose(self, post: dict, limits: dict, server: str) -> str:
        """The status text with the link appended on a new line (Mastodon builds its own preview card), checked against the
        instance's limits. Mastodon can't take what is refused here: the message says so and how to fix it."""
        text, link = post["text"], post["link"]
        if link and link not in text:
            text = f"{text}\n{link}"
        used, limit = count_characters(text, limits["url_length"]), limits["max_characters"]
        if used > limit:
            raise MastodonError(
                f"The post is {used} characters but {server} allows {limit} (a URL counts as {limits['url_length']}"
                f"{', and the link was appended' if link and link not in post['text'] else ''}). "
                'Give Mastodon a shorter text with per_network, e.g. {"mastodon": {"text": "…"}}.',
                CODE_REFUSED,
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
                f"The alt text of image {', '.join(map(str, too_long))} is over {server}'s {limits['description_limit']}-character limit.", CODE_REFUSED
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
            raise MastodonError(f"Could not download the image {url}: HTTP {resp.status_code}.", CODE_UNAVAILABLE if resp.status_code >= 500 else CODE_UNKNOWN)
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
        body, header = encode_multipart({"description": alt} if alt else {}, file_field="file", filename=filename, content_type=content_type, data=data)
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
        form: list[tuple[str, str]] = [("status", text), ("visibility", visibility)]
        form += [("media_ids[]", mid) for mid in media_ids]
        if language:
            form.append(("language", language))
        key = self._idempotency_key(ctx, server, {"status": text, "visibility": visibility, "language": language}, media)
        resp = self._request(
            ctx,
            "post",
            "/api/v1/statuses",
            what="post",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Idempotency-Key": key},
            data=urlencode(form).encode(),
        )
        status = self._json(resp, "post")
        if not status.get("id"):
            raise MastodonError("Mastodon accepted the post but returned no status id.", CODE_UNKNOWN)
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
