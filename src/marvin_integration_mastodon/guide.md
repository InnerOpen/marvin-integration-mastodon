# Mastodon

Posts a status to one Mastodon account: text, an optional link, and up to four images with alt text. It implements
Marvin's shared `social.post` capability, so a workflow can say "post this" and every connected network posts it.
It can post and delete its own statuses; it cannot read timelines, reply, edit, schedule or post polls.

## Connect it

1. On your instance: **Preferences → Development → New application**. Give it the scopes `write:statuses` and
   `write:media` (also `profile`, or `read:accounts` on older servers, if you want the connection check to read
   your profile; posting does not need it). Copy **Your access token**.
2. In Marvin: **Settings → Integrations → Mastodon → Configure**. Paste the token as the **Access token** and set
   **Server** (for example `https://mastodon.social`). Optional: **Visibility** (`public`, `unlisted` or `private`;
   default `public`) and **Language** (an ISO 639-1 code such as `en`).

## Actions

- `post_status` (`social.post`, needs approval): args `text` (required), `images` (public https image URLs),
  `alt` (one per image), `link`, `dry_run`. Returns `posted`, `post_id`, `url`, `network` ("mastodon") and, on a
  dry run, `would_post`.
- `delete_status` (needs approval): arg `id` (the `post_id` a post returned). Returns `deleted`, `id`, `network`.

Every post is public and cannot be quietly undone, so put an **Approval** step before the post step.

## What Mastodon refuses (the step fails with a message)

- **Too long.** The limit is the instance's (500 by default). A URL counts as 23 characters however long it is, and
  the `link` argument is appended to the text on a new line unless the text already has it. For a shorter Mastodon
  text, use `per_network`: `{"mastodon": {"text": "…"}}`.
- **Too many images.** The instance's limit (4 by default). Use `per_network` to give Mastodon fewer.
- **Images** must be public https URLs. Marvin downloads each one and uploads it; the alt text becomes the image's
  description (up to 1,500 characters by default).

A retried post is safe: it is sent with an `Idempotency-Key` made from its content, so Mastodon returns the first
status instead of posting twice.
