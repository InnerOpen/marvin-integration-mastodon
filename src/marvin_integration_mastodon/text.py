"""How Mastodon counts a status, so a post is refused here with a clear message rather than by a 422.

Mastodon's own rule (StatusLengthValidator): every URL counts as a fixed number of characters whatever its length
(23 by default; the instance reports it as ``characters_reserved_per_url``), and a mention counts only its local
part (``@user@example.social`` is ``@user``). Everything else counts one per character. The stdlib has no grapheme
segmentation, so this counts code points, which can only over-count (a family emoji is one character to Mastodon).
"""

import re
from html.parser import HTMLParser

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


class _PlainText(HTMLParser):
    """Mastodon's status HTML as plain text: paragraphs and line breaks kept, a mention or hashtag as its visible text,
    any other link as its address (the visible label is a shortened, half-hidden copy of it)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._in_link = False  # inside a plain link: its label is dropped, the href was already written

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "br":
            self.parts.append("\n")
        elif tag == "a":
            classes = (attributes.get("class") or "").split()
            if "mention" not in classes and "hashtag" not in classes and attributes.get("href"):
                self.parts.append(attributes["href"])
                self._in_link = True

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_link = False
        elif tag == "p":
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self._in_link:
            self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """A status's ``content`` (HTML) as plain text, ready to match keywords against."""
    parser = _PlainText()
    parser.feed(html or "")
    parser.close()
    text = "".join(parser.parts).replace("\u00a0", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
