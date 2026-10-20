# marvin-integration-mastodon

A [Marvin](https://claude.ai/code) integration for **Mastodon**. It posts text, an optional link, and up to four images
with alt text through Marvin's shared **`social.post`** capability, and (since 0.2.0) takes part in the conversation
through the shared social engagement capabilities: read mentions and replies, reply (privately by default), favourite,
boost, send a direct message and delete your own replies. Every connected network answers the same capabilities, with no
third-party aggregator in between. It talks to your instance's REST API directly. Version 0.2.0.

An installed Marvin finds it through the `marvin.integrations` entry point: no changes to Marvin core.

## Set it up

1. **Create an access token on your instance.** Preferences → Development → **New application**. Name it (for
   example "Marvin"), and tick the scopes below. Untick the defaults (`read`, `write`, `follow`, `push`) and
   grant only what you use. Save, open the application and copy **Your access token**.

   | Scope | Needed for |
   |---|---|
   | `write:statuses` | `post_status`, `reply`, `message`, `repost`, `delete_comment`, `delete_status` |
   | `write:media` | images on `post_status` |
   | `read:notifications` | `comments` (the mention notifications) |
   | `read:statuses` | `reply`, `delete_comment` (they read the status first) |
   | `read:accounts` | `comments` and `delete_comment` (who is "me"), `message` by id, and the health check |
   | `write:favourites` | `like` |

   A token that only posts needs just the first two. Grant `read:accounts` for engagement (`profile` is the narrower scope on Mastodon 4.3+ but only
   covers your own profile; **verify**). A token missing a scope is
   answered 403 and the step fails with an `auth` error naming the scopes.
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

## Engagement (social.comments and friends)

Each action declares one capability, so Marvin's shared auto-reply engine and workflows reach it the same way on every
network. Gated actions (`requires_approval`) need an Approval step in a workflow.

| Action | Capability | Gated | Does |
|---|---|---|---|
| `comments` | `social.comments` | no | Mentions as `{items, cursor}`, oldest first |
| `reply` | `social.reply` | yes | A direct (private, default) or public status replying to an item |
| `like` | `social.like` | no | Favourite a status |
| `repost` | `social.repost` | yes | Boost a status |
| `message` | `social.message` | yes | A direct status to an account |
| `delete_comment` | `social.delete_comment` | yes | Delete one of your own statuses |

There is no `social.hide`: Mastodon can't hide other people's replies.

- **`comments`** `{since?, limit = 50, kinds?}`. Source: `GET /api/v1/notifications?types[]=mention`. The `cursor` is
  the id of the last notification returned; pass it back as `since` and it is sent as `since_id`. Mastodon answers
  newest first, so the plugin pages back with `max_id` (40 a page) until it has everything newer than the cursor, then
  returns the oldest `limit` of them; if more are waiting the cursor stops after the last one returned, so the next poll
  continues from there. Without a cursor you get the newest `limit` (a first run doesn't replay history). `kinds`
  filters, and the cursor still moves past what was filtered out. Each item is
  `{id, kind, post_id, post_url, author: {id, handle, name}, text, created_at, url, network: "mastodon"}`:
  - `id`: the mention's **status id** (what `reply`, `like` and `delete_comment` take as `comment_id`).
  - `kind`: **`reply`** when the status's `in_reply_to_account_id` is your account (it answers one of your statuses),
    otherwise **`mention`**. Mastodon has no separate "comment" (a comment on a post is a reply), so `comment` is never
    produced and `kinds: ["comment"]` returns nothing.
  - `post_id`: the status it replies to (`in_reply_to_id`), or `null`. `post_url`: that status's permalink when it is
    yours (`{server}/@{you}/{id}`), else `null`.
  - `author.handle` is the `acct` (`user`, or `user@instance` for a remote account, no leading `@`); `author.name` the
    display name (or the username).
  - `text`: the status HTML as plain text. Paragraphs and line breaks are kept, mentions and hashtags keep their visible
    text, other links become their full address.
  - `created_at`: ISO 8601 UTC. `url`: the status permalink.
- **`reply`** `{comment_id, text, private = true, dry_run}`. Fetches the status for the author's `acct`, prepends
  `@acct` unless the text already mentions that account, and posts with `in_reply_to_id`. `private` is visibility
  `direct`; `private: false` is `public`, or `unlisted` if the connection's Visibility is `unlisted` (a followers-only
  connection still replies publicly: that isn't a reply mode). The same character check (a URL counts as 23, a mention
  as its local part, the `@acct` included) and the same content-derived `Idempotency-Key` as `post_status` apply, so a
  retry can't answer twice. Returns `{replied, reply_id, private, url, network}`, plus `would_reply` on a dry run.
- **`like`** `{comment_id}` → `{liked, network}` (`POST /api/v1/statuses/:id/favourite`).
- **`repost`** `{post_id}` → `{reposted, url, network}` (`POST /api/v1/statuses/:id/reblog`; `url` is the boosted post).
- **`message`** `{to: {id?, handle?}, text, dry_run}` → `{sent, message_id, network}`: a `direct` status mentioning
  `@handle` (handle wins when both are given; with only `id` it is looked up with `GET /api/v1/accounts/:id`). Same
  character check and idempotency; `would_send` on a dry run.
- **`delete_comment`** `{comment_id}` → `{deleted, network}`. It reads the status and deletes it only if it is yours;
  otherwise it refuses with code **`unsupported`** ("Mastodon can delete only your own replies"). `delete_status`
  (any id, unchanged) is still there.

Failures carry the plugin's stable codes (`refused`, `auth`, `unsupported`, `rate_limited`, `unavailable`, `config`,
`unknown`); a bad argument or an over-long reply is `refused`.

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
- **Health check:** `GET /api/v1/accounts/verify_credentials`. With `read:accounts` (needed for engagement anyway) it
  answers 200. A token with only write scopes is answered 403, which the check still treats as healthy (a bad or revoked
  token is a 401).

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
   a 403 as healthy (a bad or revoked token is a 401). Confirm the 403 and its body on a real server. With the read scopes
   for engagement the caveat goes away: the check gets a 200.
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

### Engagement (0.2.0), also not run against a live server

9. **verify** `since_id` semantics. The plugin sends `since_id` with `max_id` paging, which returns every notification
   newer than the cursor but walks back from the newest. `min_id` pages forward from the cursor and may be the better
   fit; it would change the paging only, not the cursor. A poll that finds more than 1,000 new mentions (25 pages of 40)
   logs a warning and the oldest ones beyond that window can be missed.
10. **verify** `types[]=mention` on `GET /api/v1/notifications` (current Mastodon; older servers used `exclude_types[]`),
    and that the page `limit` of 40 is accepted everywhere (the documented maximum is 80). Notifications of a filtered or
    blocked account may be missing, which can make a page shorter than `limit`; the plugin therefore pages until an empty
    page rather than until a short one.
11. **verify** The `reply` kind rests on the mention status's `in_reply_to_account_id` equalling your account id (read
    from `verify_credentials`), instead of fetching each parent status. A reply to someone else's status that mentions you
    is a `mention`.
12. **verify** `post_url` is built as `{server}/@{username}/{id}`, which is how Mastodon names a local account's status
    permalinks; the plugin doesn't fetch the parent to confirm. A fork may differ.
13. **verify** Plain-text conversion. It assumes Mastodon's markup (`a.mention`, `a.hashtag`, `span.invisible`, `p`, `br`).
    A plain link is output as its `href`; other servers' HTML (Pleroma, GoToSocial) may differ slightly.
14. **verify** `POST /api/v1/statuses/:id/reblog` answers a new status wrapping the original in `reblog`; the plugin
    returns the original's `url` (falling back to the wrapper's). Boosting a private or direct status is a 422, surfaced
    as `refused`. Boosting an already boosted status is answered 200 by Mastodon (not checked).
15. **verify** `direct` statuses: Mastodon delivers one only to the accounts it mentions, so the `@acct` prefix is what
    makes `reply` and `message` private to that person. The character count treats `@user@instance` as `@user` (as for
    posts). The `Idempotency-Key` is made from the status text, visibility, language and `in_reply_to_id`, so the same
    reply sent twice within the hour is one status.
16. **verify** `GET /api/v1/statuses/:id` of a `direct` status needs `read:statuses` and that the token's account is a
    recipient; `reply` and `delete_comment` fail with Mastodon's 404/403 text otherwise. Mastodon's own answer when you try
    to `DELETE` another account's status (404 or 403) isn't relied on: the plugin checks the author first.
17. **verify** `message` by `id` needs `read:accounts` for `GET /api/v1/accounts/:id` (public on most servers, but not
    on ones with authorized-fetch or limited-federation modes).
