# marvin-integration-mastodon

A [Marvin](https://claude.ai/code) integration that posts to **Mastodon**: text, an optional link, and up to four
images with alt text. It implements Marvin's shared **`social.post`** capability, so a workflow can say "post this" and
each connected network posts it, with no third-party aggregator in between. It talks to your instance's REST API
directly. Version 0.1.0.

An installed Marvin finds it through the `marvin.integrations` entry point: no changes to Marvin core.

## Set it up

1. **Create an access token on your instance.** Preferences → Development → **New application**. Name it (for
   example "Marvin"), and tick the scopes **`write:statuses`** and **`write:media`**. Untick the defaults (`read`,
   `write`, `follow`, `push`), since posting needs only those two. Optionally also tick **`profile`** (Mastodon 4.3+) or
   **`read:accounts`** (older) so the health check can read your profile; see "verify" below. Save, open the application
   and copy **Your access token**.
2. **Install the plugin** on the Marvin host: `uv pip install marvin-integration-mastodon`, then restart Marvin.
3. **Connect it.** Settings → Integrations → Mastodon → Configure:
   - **Access token**: the token from step 1 (stored as Marvin's secret, never in the settings).
   - **Server**: your instance, e.g. `https://mastodon.social` (https only; a bare `mastodon.social` is accepted).
   - **Visibility** (optional): `public` (default), `unlisted` or `private` (followers only).
   - **Language** (optional): an ISO 639-1 code, e.g. `en`, sent with every post.
4. Save. The card should turn `ok` (Marvin calls `GET /api/v1/accounts/verify_credentials`).
5. In a workflow, put an **Approval** step before the post. Every social post needs a person's approval, and core runs a
   `social.post` action only after one. The "Post to Mastodon when published" recipe shows the shape.

## Actions

| Action | Capability | Approval | What it does |
|---|---|---|---|
| `post_status` | `social.post` | yes | Posts a status. |
| `delete_status` | none | yes | Deletes one of your own statuses by id. |

### `post_status` arguments (the `social.post` contract) and how they map

| Argument | Mastodon |
|---|---|
| `text` (required) | `status`. Checked against `configuration.statuses.max_characters` (500 if absent); a URL counts as 23 characters. |
| `link` (optional URL) | Appended to `status` on a new line unless the text already contains it. Mastodon builds its own preview card. |
| `images` (0–10 public https URLs) | Each is downloaded with `ctx.http`, uploaded with `POST /api/v2/media`, and attached as `media_ids[]`. At most `configuration.statuses.max_media_attachments` (4 if absent); more is refused. |
| `alt` (list, one per image) | The upload's `description`. A shorter list leaves the remaining images without alt text; a longer one is refused. |
| `dry_run` (bool, default `false`) | Returns `would_post` and uploads and posts nothing. If the instance can't be reached, a dry run falls back to Mastodon's default limits (`would_post.limits_from` says `default`). |

Output: `posted`, `post_id` (the status id), `url` (the status's `url`), `network` = `"mastodon"`, plus `dry_run`,
`visibility` and `images`. On a dry run `posted` is `false`, `post_id` and `url` are `null`, and `would_post` holds the
text actually sent (link appended), its character count and limit, visibility, language and the media plan.

What Mastodon can't take is refused with a clear message (an `IntegrationError`, code `refused`) before anything is
posted: text over the limit (it suggests `per_network` with a shorter text), too many images, an image that is not
https or not an image, alt text over the instance's limit, or a link that isn't http(s).

Failure codes and how Marvin handles them: `unavailable` and `rate_limited` retry (1m, 5m, 15m) and then notify; `auth`
notifies and retries once when the connection recovers; `refused`, `config` and `unknown` just fail the step.

Visibility and language come from the connection's settings, not from the arguments (the contract has no such argument).

### `delete_status`

`id` (the `post_id` a post returned) → `DELETE /api/v1/statuses/:id`. Output: `deleted`, `id`, `network`.

## How it works

- **Instance limits:** `GET /api/v2/instance` (`configuration.statuses.max_characters`, `max_media_attachments`,
  `characters_reserved_per_url`; `configuration.media_attachments.description_limit`, `image_size_limit`). A real post
  needs a readable instance (a 404, from a server with no v2 endpoint, uses the defaults); a dry run doesn't.
- **Images:** downloaded through the core's `ctx.http` (timeouts, size cap, SSRF guard). The Mastodon token is never sent
  to the image host. **Multipart:** the SDK's `HttpHelper` has only `json=` and raw `data=` bytes, no `files=`, so the
  `multipart/form-data` body (`file` + `description`) is **built by hand** with a boundary (`multipart.py`) and sent as
  `data=` with the boundary in `Content-Type`.
- **Processing:** `POST /api/v2/media` answers 200 when the file is ready and 202 when Mastodon is still processing it.
  On 202 the plugin polls `GET /api/v1/media/:id` (206 = still working, 200 = ready) up to 10 times, 2 s apart, then
  fails without posting. The pause is `MastodonProvider._sleep`, patched in tests.
- **Post:** `POST /api/v1/statuses` (form-encoded: `status`, `visibility`, `language`, `media_ids[]`) with an
  `Idempotency-Key` that is a hash of the server, text, visibility, language and images, plus Marvin's retry-chain seed
  (`ctx.idempotency_seed`) when core supplies one. The same call sent twice yields one status (Mastodon remembers keys for
  an hour, so deliberately re-posting identical content within the hour returns the first post unless the retry seed
  differs).
- **Health check:** `GET /api/v1/accounts/verify_credentials`.

## Develop

```
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

The tests use a fake HTTP client (`tests/fakes.py`) and a fake token; nothing touches a network, and no real credential
belongs in this repo. The SDK resolves from the sibling checkout `../MarvinIntegrationSDK` (see `[tool.uv.sources]`).
The logo is Mastodon's official purple mark from joinmastodon.org/branding.

## Verify (not confirmed against a live server)

The Mastodon behaviour below was taken from the API documentation (the `mastodon/documentation` repository) and has not
been run against a real instance. Each point is marked **verify**.

1. **verify** Health check scope. `verify_credentials` requires `profile` or `read:accounts`, which a
   `write:statuses write:media` token doesn't have. Mastodon should answer such a token with **403**, and the plugin treats
   a 403 as healthy (a bad or revoked token is a 401). Confirm the 403 and its body on a real server, or grant `profile`.
2. **verify** Media processing. The docs say 4.0+ processes images synchronously (200) and larger video/audio asynchronously
   (202), and that `GET /api/v1/media/:id` returns 206 while processing. The poll treats 206 as "wait" and 200 as "ready".
3. **verify** Character counting (`text.py`). URL = 23 (or the instance's `characters_reserved_per_url`), mention =
   `@user` only. The URL and mention patterns approximate Mastodon's own, and the count is in code points where Mastodon
   may count grapheme clusters, so emoji-heavy text can be over-counted (a false refusal, never a missed one). If the
   count is ever under, Mastodon's 422 is surfaced as a `refused` error.
4. **verify** Marvin's `ctx.http`: that it passes a custom `Content-Type` (the multipart boundary) and an `Idempotency-Key`
   header untouched, accepts the larger `timeout` used for uploads (60 s), that its size cap fits the images, and that it does
   not retry a POST by itself in a way that bypasses the key.
5. **verify** Instance fields. `description_limit` (default 1500) and `image_size_limit` (default 16 MB) are read from
   `configuration.media_attachments`; the plugin falls back to these defaults when absent. Forks (Pleroma/Akkoma, GoToSocial,
   Pixelfed) report limits differently and are not targeted. Mastodon before 4.0 has no `/api/v2/instance` and gets the defaults.
6. **verify** `dry_run` defaults to `false` here, because core only runs `social.post` after an Approval step. The contract
   doesn't state a default, and `instagram`'s `publish_media` defaults to a dry run; settle it in core so every network agrees
   (a workflow that omits `dry_run` would otherwise post to one network and only plan on another).
7. **verify** After a failed post, images already uploaded are orphaned on the instance (Mastodon keeps unattached media
   for about a day); nothing else is created.
8. **verify** The status response's `url` is the human permalink; `null` is returned as `null` (the plugin does not fall back to the ActivityPub `uri`).
