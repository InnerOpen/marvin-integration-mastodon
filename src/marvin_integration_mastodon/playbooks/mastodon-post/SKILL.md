---
name: mastodon-post
description: Use when drafting a Mastodon post (a status) about something the workspace published or is about to: how Mastodon counts length, how to shape the post, hashtags, alt text for images, and the approval rule. Write the text yourself; a person approves it before anything is posted.
---

# A Mastodon post

Check whether the workspace has a voice or style Playbook too, and follow it for tone. This one covers what Mastodon needs:
the length it allows, how it counts, and how to write something that reads well there.

A post points people at real work. It is not an ad, and it is not a press release.

## Write it yourself

Write the post text yourself. Do not use `compose_entry` or `revise_entry`: those hand the writing to another AI that never
loads Playbooks, so none of this would reach it. When the post is kept as an entry (a social post type, say), save it with
`create_draft` for a new one or `update_draft` for a change, and use `get_entry_type` first to see its fields. When the text
is the answer to a request or the output of a workflow step, return just the post text, with nothing around it.

## Posting needs a person

Nothing you write is posted by you. In a workflow the text goes to an Approval step and then to the post action, and the
connection runs it only after someone approves. Draft with that in mind: a person will read exactly what you wrote. Say
in your reply that it needs approval before it goes out.

## Length

- A post may be up to **500 characters** on most servers. The server's own limit is the one that counts, and some are
  shorter or longer. If you know it, use it.
- **A link counts as 23 characters**, however long it is. A full address costs no more than a short one.
- A mention such as `@name@example.social` counts only as `@name`.
- Hashtags and emoji count like any other characters.
- Stay under the limit by a margin. About 400 characters leaves room and reads better. A longer post is rarely a better one.

If the text doesn't fit, cut words. Don't split it into a thread unless the person asks for one.

## Shape

1. **One idea**, in the first sentence. People read the start and scroll on.
2. **Why it matters or what is in it**, one or two sentences. Facts from the entry, not claims.
3. **The link**, on its own line near the end, using the `url` the tools return for the entry. If the entry has no url, say
   so in your reply and leave the link out rather than guessing one.
4. **Hashtags** last, on their own line.

Write plainly. No hype words, no exclamation marks in a row, no calls to act now. Only facts the entry states: never invent
a price, a date or a quantity.

## Hashtags

- One to three. More looks like spam and clutters the post.
- Write each in CamelCase (`#SlowFashion`, not `#slowfashion`): screen readers then read the words separately.
- Use tags people follow for the topic, not tags you made up. Leave out generic ones such as `#post` or `#new`.
- Put them at the end, after the link.

## Images and alt text

- A post can have up to four images. Each should have alt text.
- Alt text says what the image shows, in a sentence or two, for someone who can't see it. Not "image of" or a file name.
  Put in any text that is in the image.
- If the entry has an image but no description, say so in your reply and offer alt text for a person to check.
- Images must be public addresses starting with `https://`.

## Content warnings and sensitive topics

Mastodon posts can carry a content warning that hides the text behind a short label. This connection can't set one, so a
post goes out in the open. If the subject needs a warning (illness, violence, politics a community expects flagged), say so
in your reply and let a person decide. Don't write "CW:" into the text to stand in for one.

## Before you hand it back

Go through `references/examples.md` for the shape of a good post and the usual mistakes. In your reply:

- [ ] One idea, a real link, plain wording
- [ ] Under the length with room to spare, counting each link as 23
- [ ] Hashtags: one to three, CamelCase, at the end
- [ ] Alt text for each image, or a note that it is missing
- [ ] A note that it needs approval before it is posted, and any content warning you think it needs
