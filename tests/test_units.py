"""The two helpers with their own rules: Mastodon's character counting and the hand-built multipart body."""

import pytest

from marvin_integration_mastodon.multipart import encode_multipart, safe_filename
from marvin_integration_mastodon.text import count_characters


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("hello", 5),
        ("héllo 🐘", 7),
        ("https://example.org/a/very/long/path?with=query#and-fragment", 23),
        ("see https://example.org/a/very/long/path now", 4 + 23 + 4),
        ("(https://example.org/x).", 1 + 23 + 2),  # punctuation after a link is the sentence's
        ("http://a.example", 23),
        ("two https://a.example/1 and https://b.example/2", 4 + 23 + 5 + 23),
        ("a=https://example.org/x", len("a=https://example.org/x")),  # not a link: it follows "="
        ("@user", 5),
        ("@user@mastodon.social hi", len("@user hi")),
        ("mail me@example.org", len("mail me@example.org")),  # not a mention: it follows a word character
    ],
)
def test_count_characters(text, expected):
    assert count_characters(text) == expected


def test_the_reserved_url_length_is_a_parameter():
    assert count_characters("https://example.org/x", 10) == 10


def test_multipart_body_has_the_fields_and_the_file():
    body, header = encode_multipart({"description": "alt ✓"}, file_field="file", filename="a b.png", content_type="image/png", data=b"\x00\x01png")
    boundary = header.split("boundary=")[1]
    assert header.startswith("multipart/form-data; boundary=")
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="description"\r\n\r\nalt \xe2\x9c\x93\r\n' in body
    assert b'name="file"; filename="a_b.png"\r\nContent-Type: image/png\r\n\r\n\x00\x01png\r\n' in body


def test_the_boundary_is_never_inside_the_data(monkeypatch):
    import marvin_integration_mastodon.multipart as mp

    class Ids:
        values = iter(["a" * 32, "b" * 32])

        def __init__(self):
            self.hex = next(Ids.values)

    monkeypatch.setattr(mp.uuid, "uuid4", Ids)
    _body, header = encode_multipart({}, file_field="file", filename="x.png", content_type="image/png", data=b"----marvin" + b"a" * 32)
    assert header.endswith("b" * 32)


@pytest.mark.parametrize(("name", "expected"), [("hero.png", "hero.png"), ('a"b\r\n.png', "a_b_.png"), ("../../etc/passwd", "etc_passwd"), ("", "image"), ("日本語", "image")])
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected
