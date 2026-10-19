import pytest

from marvin_integration_mastodon import MastodonProvider


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """The media poll's pause is injectable: record it instead of waiting."""
    pauses: list[float] = []
    monkeypatch.setattr(MastodonProvider, "_sleep", staticmethod(pauses.append))
    return pauses
