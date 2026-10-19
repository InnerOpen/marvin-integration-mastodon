"""How Mastodon counts a status, so a post is refused here with a clear message rather than by a 422.

Mastodon's own rule (StatusLengthValidator): every URL counts as a fixed number of characters whatever its length
(23 by default; the instance reports it as ``characters_reserved_per_url``), and a mention counts only its local
part (``@user@example.social`` is ``@user``). Everything else counts one per character. The stdlib has no grapheme
segmentation, so this counts code points, which can only over-count (a family emoji is one character to Mastodon).
"""

import re

URL_PLACEHOLDER = 23
# Mirrors Mastodon's URL / mention patterns: neither may directly follow a word character, "=" or "/".
_URL = re.compile(r"(?<![=/\w])https?://[^\s<>]+")
_MENTION = re.compile(r"(?<![=/\w])@([a-z0-9_]+(?:[a-z0-9_.-]+[a-z0-9_]+)?)(?:@[\w.-]+\w+)?", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\""  # punctuation after a link belongs to the sentence, not the URL


def count_characters(text: str, url_length: int = URL_PLACEHOLDER) -> int:
    """The length Mastodon checks against the instance's ``max_characters``."""

    def url(match: re.Match) -> str:
        found = match.group(0)
        bare = found.rstrip(_TRAILING)
        return "x" * url_length + found[len(bare) :]

    return len(_MENTION.sub(lambda m: f"@{m.group(1)}", _URL.sub(url, text)))
