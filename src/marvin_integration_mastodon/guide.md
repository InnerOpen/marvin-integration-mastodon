# Mastodon

Posts a status to one Mastodon account (text, an optional link, up to four images with alt text) and takes part in the
conversation around it. It implements Marvin's shared social capabilities, so a workflow or the auto-reply engine can say
"post this", "read the new mentions" or "reply" and every connected network does it. It cannot read timelines, edit,
schedule, post polls or hide other people's replies.

## Connect it

1. On your instance: **Preferences → Development → New application**. Leave **Redirect URI** as
   `urn:ietf:wg:oauth:2.0:oob` (Marvin uses the token, not a redirect). Give it the scopes you need, and no others:
   `write:statuses` and `write:media` to post; `read:notifications`, `read:statuses` and `read:accounts` to read and answer
   mentions; `write:favourites` to like. With `read:accounts` the connection check reads your profile (without it, a 403 is
   still treated as a working token). Copy **Your access token**.
2. In Marvin: **Settings → Integrations → Mastodon → Configure**. Paste the token as the **Access token** and set
   **Server** (for example `https://mastodon.social`). Optional: **Visibility** (`public`, `unlisted` or `private`;
   default `public`; an `unlisted` setting also makes public replies unlisted) and **Language** (an ISO 639-1 code such
   as `en`).

## Actions

- `post_status` (`social.post`, needs approval): args `text` (required), `images` (public https image URLs),
  `alt` (one per image), `link`, `dry_run`. Returns `posted`, `post_id`, `url`, `network` ("mastodon") and, on a
  dry run, `would_post`.
- `delete_status` (needs approval): arg `id` (the `post_id` a post returned). Returns `deleted`, `id`, `network`.
- `comments` (`social.comments`): args `since` (the cursor returned last time), `limit` (default 50), `kinds`. Returns
  `items` (oldest first) and `cursor`. Mastodon has no separate comments: your mentions are the source, and a mention that
  replies to one of your own statuses is kind `reply`, any other is `mention` (`comment` is never produced). Each item has
  `id` (the status id), `kind`, `post_id`, `post_url`, `author` (`id`, `handle`, `name`), `text` (plain text),
  `created_at`, `url` and `network`.
  A mention on a **direct** status (only the people it mentions see it) comes back as kind `message` — a DM, which
  Marvin's Social inbox keeps and answers only by a direct status back (`message`), never publicly.
- `reply` (`social.reply`, needs approval): args `comment_id`, `text`, `private` (default true), `dry_run`. Private is a
  direct status only the person sees; `private: false` replies publicly. The person's `@handle` is put in front unless it
  is already in the text. Returns `replied`, `reply_id`, `private`, `url`, `network` and, on a dry run, `would_reply`.
- `like` (`social.like`): arg `comment_id`. Returns `liked`, `network`.
- `repost` (`social.repost`, needs approval): arg `post_id`. Returns `reposted`, `url`, `network`.
- `message` (`social.message`, needs approval): args `to` (`{handle}` or `{id}`), `text`, `dry_run`. A direct status
  mentioning them. Returns `sent`, `message_id`, `network`, and `would_send` on a dry run.
- `delete_comment` (`social.delete_comment`, needs approval): arg `comment_id`. Deletes one of your own statuses; for
  anyone else's it fails with code `unsupported` ("Mastodon can delete only your own replies"). Returns `deleted`, `network`.

Posts, replies, messages, boosts and deletes can't be quietly undone, so put an **Approval** step before them.

## What Mastodon refuses (the step fails with a message)

- **Too long.** The limit is the instance's (500 by default). A URL counts as 23 characters however long it is, a mention
  counts as its local part, and the `link` argument is appended to the text on a new line unless the text already has it.
  For a shorter Mastodon text, use `per_network`: `{"mastodon": {"text": "…"}}`. A reply or message counts the
  `@handle` that is put in front.
- **Too many images.** The instance's limit (4 by default). Use `per_network` to give Mastodon fewer.
- **Images** must be public https URLs. Marvin downloads each one and uploads it; the alt text becomes the image's
  description (up to 1,500 characters by default).
- **A missing scope** (403): the message names the scopes each action needs.
- **Hiding replies** isn't something Mastodon offers, so there is no hide action.

A retried post, reply or message is safe: it is sent with an `Idempotency-Key` made from its content, so Mastodon returns
the first status instead of posting twice.
