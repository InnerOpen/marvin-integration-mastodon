"""A multipart/form-data body, built by hand.

The SDK's ``HttpHelper`` takes ``json=`` or raw ``data=`` bytes plus headers; it has no ``files=``. Mastodon's media
upload is multipart-only (``file`` + ``description``), so the body is assembled here and sent as ``data=`` with the
boundary in the ``Content-Type`` header.
"""

from __future__ import annotations

import re
import uuid

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, default: str = "image") -> str:
    """A name safe inside a quoted Content-Disposition parameter."""
    cleaned = _UNSAFE.sub("_", name).strip("._")
    return cleaned[:100] or default


def encode_multipart(fields: dict[str, str], *, file_field: str, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    """``(body, content_type_header)`` for one file part and any number of text fields."""
    boundary = f"----marvin{uuid.uuid4().hex}"
    while boundary.encode() in data:  # a collision is astronomically unlikely, but it is one comparison
        boundary = f"----marvin{uuid.uuid4().hex}"
    crlf = b"\r\n"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            str(value).encode("utf-8"),
        ]
    chunks += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_filename(filename)}"'.encode(),
        f"Content-Type: {content_type}".encode(),
        b"",
        data,
        f"--{boundary}--".encode(),
        b"",
    ]
    return crlf.join(chunks), f"multipart/form-data; boundary={boundary}"
