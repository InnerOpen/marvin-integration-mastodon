"""A fake Mastodon instance behind the SDK's HttpHelper interface, plus helpers to read what it was sent."""

import email
import json
import logging
from dataclasses import dataclass
from email import policy
from urllib.parse import parse_qs, urlsplit

from marvin_integration_sdk import IntegrationContext, Response

SERVER = "https://mastodon.example"
LOG = logging.getLogger("test")
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
INSTANCE = {"configuration": {"statuses": {"max_characters": 500, "max_media_attachments": 4, "characters_reserved_per_url": 23}}}


def reply(status=200, body=None, headers=None):
    content = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
    return Response(status_code=status, content=content, headers=headers or {})


@dataclass
class Request:
    method: str
    url: str
    headers: dict
    data: bytes | None = None
    timeout: float | None = None

    @property
    def path(self) -> str:
        return self.url.removeprefix(SERVER)

    def form(self) -> dict[str, list[str]]:
        return parse_qs((self.data or b"").decode())

    def multipart(self) -> dict[str, dict]:
        """The parts of a multipart body: name -> {value, filename, content_type}."""
        message = email.message_from_bytes(b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + self.data, policy=policy.default)
        return {
            part.get_param("name", header="content-disposition"): {
                "value": part.get_payload(decode=True),
                "filename": part.get_filename(),
                "content_type": part.get_content_type(),
            }
            for part in message.iter_parts()
        }


class FakeMastodon:
    """Routes by method and path. ``images`` maps a URL to (bytes, content-type); ``polls`` is the list of status codes the
    media endpoint answers in turn (the last repeats); ``upload_status`` is what POST /api/v2/media answers (200 or 202)."""

    def __init__(
        self,
        *,
        instance=INSTANCE,
        instance_status=200,
        images=None,
        upload_status=200,
        polls=(200,),
        poll_status_body=None,
        status_reply=None,
        raises=None,
        notifications=(),
        statuses=None,
        accounts=None,
    ):
        self.instance, self.instance_status = instance, instance_status
        self.images = images if images is not None else {}
        self.upload_status, self.polls = upload_status, list(polls)
        self.status_reply = status_reply
        self.raises = raises or {}  # "METHOD path-prefix" -> exception to raise
        self.verify = reply(200, {"id": "1", "acct": "me", "username": "me"})
        self.notifications = list(notifications)  # every mention notification the instance has, any order
        self.statuses = statuses if statuses is not None else {}  # id -> status
        self.accounts = accounts if accounts is not None else {}  # id -> account
        self.requests: list[Request] = []
        self._media = 0

    # -- what the tests read back --
    def to(self, method, path_prefix) -> list[Request]:
        return [r for r in self.requests if r.method == method and r.path.startswith(path_prefix)]

    @property
    def status_posts(self) -> list[Request]:
        return self.to("POST", "/api/v1/statuses")

    @property
    def uploads(self) -> list[Request]:
        return self.to("POST", "/api/v2/media")

    # -- HttpHelper --
    def _record(self, method, url, headers, data=None, timeout=None):
        req = Request(method, url, dict(headers or {}), data, timeout)
        self.requests.append(req)
        for key, exc in self.raises.items():
            m, prefix = key.split(" ", 1)
            if m == method and (req.path.startswith(prefix) or url == prefix):
                raise exc
        return req

    def get(self, url, *, headers=None, timeout=15):
        req = self._record("GET", url, headers, timeout=timeout)
        if url in self.images:
            content, ctype = self.images[url]
            return reply(200, content, {"Content-Type": ctype} if ctype else {})
        if req.path == "/api/v2/instance":
            return reply(self.instance_status, self.instance)
        if req.path == "/api/v1/accounts/verify_credentials":
            return self.verify
        if req.path.startswith("/api/v1/notifications"):
            return reply(200, self._notifications(parse_qs(urlsplit(url).query)))
        if req.path.startswith("/api/v1/statuses/") and req.path.rsplit("/", 1)[-1] in self.statuses:
            return reply(200, self.statuses[req.path.rsplit("/", 1)[-1]])
        if req.path.startswith("/api/v1/accounts/") and req.path.rsplit("/", 1)[-1] in self.accounts:
            return reply(200, self.accounts[req.path.rsplit("/", 1)[-1]])
        if req.path.startswith("/api/v1/media/"):
            code = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return reply(code, {"id": req.path.rsplit("/", 1)[-1]})
        return reply(404, {"error": "Record not found"})

    def _notifications(self, query) -> list[dict]:
        """What Mastodon answers: newest first, only the asked types, after since_id and before max_id, at most ``limit``."""
        assert query.get("types[]") == ["mention"]
        found = [n for n in self.notifications if n["type"] in query["types[]"]]
        if "since_id" in query:
            found = [n for n in found if int(n["id"]) > int(query["since_id"][0])]
        if "max_id" in query:
            found = [n for n in found if int(n["id"]) < int(query["max_id"][0])]
        return sorted(found, key=lambda n: -int(n["id"]))[: int(query.get("limit", ["40"])[0])]

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        req = self._record("POST", url, headers, data, timeout)
        if req.path.endswith("/favourite"):
            return reply(200, {"id": req.path.split("/")[-2], "favourited": True})
        if req.path.endswith("/reblog"):
            original = req.path.split("/")[-2]
            return reply(200, {"id": "900", "url": None, "reblog": {"id": original, "url": f"{SERVER}/@them/{original}"}})
        if req.path == "/api/v2/media":
            self._media += 1
            return reply(self.upload_status, {"id": f"m{self._media}", "url": None if self.upload_status == 202 else "https://files.example/x"})
        if req.path == "/api/v1/statuses":
            return self.status_reply or reply(200, {"id": "110001", "url": f"{SERVER}/@me/110001", "visibility": "public"})
        return reply(404, {"error": "no route"})

    def put(self, url, *, json=None, data=None, headers=None, timeout=15):
        raise AssertionError("the Mastodon provider never PUTs")

    def delete(self, url, *, headers=None, timeout=15):
        req = self._record("DELETE", url, headers, timeout=timeout)
        return reply(200, {"id": req.path.rsplit("/", 1)[-1], "text": "gone"})


def make_ctx(http, *, config=None, secret="fake-token", seed=None):
    cfg = {"server": SERVER, **(config if config is not None else {})}
    return IntegrationContext(config=cfg, secret=secret, http=http, logger=LOG, idempotency_seed=seed)
