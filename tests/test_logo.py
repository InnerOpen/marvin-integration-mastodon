"""The packaged logo exists and would pass the checks Marvin core runs before serving it.

Core's validator isn't importable here, so ``logo_problems`` mirrors its rejection rules: at most 64 KB;
a PNG must start with the PNG signature; an SVG may not contain a DOCTYPE/ENTITY, ``<script``,
``<foreignObject``, ``on*=`` attributes, an ``href``/``xlink:href`` other than ``#fragment``,
``javascript:``, or a ``url(...)``/``@import`` pointing anywhere but ``#fragment``.
"""

import re
import sys
from importlib import resources

import pytest

from marvin_integration_mastodon import MastodonProvider

MAX_BYTES = 64 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_SVG_BANNED = [
    (re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE), "DOCTYPE/ENTITY declaration"),
    (re.compile(r"<script", re.IGNORECASE), "<script>"),
    (re.compile(r"<foreignObject", re.IGNORECASE), "<foreignObject>"),
    (re.compile(r"(?<![\w:-])on[a-z]+\s*=", re.IGNORECASE), "on* event attribute"),
    (re.compile(r"javascript:", re.IGNORECASE), "javascript: URL"),
]
_HREF = re.compile(r"""(?<![\w-])(?:xlink:)?href\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
_CSS_URL = re.compile(r"""url\(\s*["']?\s*([^"')\s]*)""", re.IGNORECASE)
_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?["']?\s*([^"')\s;]*)""", re.IGNORECASE)


def logo_problems(name: str, data: bytes) -> list[str]:
    """Why core would refuse this logo; empty when it would serve it."""
    problems = []
    if len(data) > MAX_BYTES:
        problems.append(f"{len(data)} bytes is over the {MAX_BYTES}-byte limit")
    if name.lower().endswith(".png"):
        if not data.startswith(PNG_SIGNATURE):
            problems.append("PNG without the PNG signature")
    elif name.lower().endswith(".svg"):
        text = data.decode("utf-8", errors="replace")
        problems += [why for pattern, why in _SVG_BANNED if pattern.search(text)]
        problems += [f"external href {v!r}" for _, v in _HREF.findall(text) if not v.strip().startswith("#")]
        problems += [f"url() to {v!r}" for v in _CSS_URL.findall(text) if not v.startswith("#")]
        problems += [f"@import of {v!r}" for v in _IMPORT.findall(text) if not v.startswith("#")]
    else:
        problems.append("not an .svg or .png")
    return problems


def _logo_bytes() -> bytes:
    # Resolved the way the SDK's load_logo does: relative to the provider class's package.
    package = sys.modules[MastodonProvider.__module__].__package__
    resource = resources.files(package).joinpath(MastodonProvider.logo)
    assert resource.is_file(), f"{MastodonProvider.logo} is not in {package}"
    return resource.read_bytes()


def test_provider_declares_a_logo_next_to_its_icon():
    assert MastodonProvider.logo == "logo.svg"
    assert MastodonProvider.icon  # still the fallback when core refuses the logo


def test_logo_ships_in_the_package_and_passes_core_checks():
    data = _logo_bytes()
    assert 0 < len(data) <= MAX_BYTES
    assert logo_problems(MastodonProvider.logo, data) == []


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("logo.png", b"GIF89a not a png"),
        ("logo.png", PNG_SIGNATURE + b"\0" * MAX_BYTES),
        ("logo.svg", b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>'),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><!ENTITY x "y"></svg>'),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>'),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="x()"/>'),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><use href="https://e.example/a.svg#b"/></svg>'),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><image xlink:href="data:image/png;base64,AA"/></svg>'),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:x()"/></svg>'),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><path style="fill:url(https://e.example/p)"/></svg>'),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://e.example/a.css";</style></svg>'),
        ("logo.gif", b"GIF89a"),
    ],
)
def test_checker_refuses_what_core_refuses(name, data):
    assert logo_problems(name, data)


def test_checker_allows_fragment_references():
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<defs><linearGradient id="g"/></defs><path fill="url(#g)" stroke-linejoin="round"/>'
        b'<use xlink:href="#g"/></svg>'
    )
    assert logo_problems("logo.svg", svg) == []
