"""Receiving side: Meta webhook verification, signature check and parsing."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from aiohttp import web

log = logging.getLogger("matrix_wacloud.webhook")


@dataclass
class StatusUpdate:
    wamid: str
    status: str
    timestamp: int | None
    recipient: str | None
    error_code: int | None = None
    error_title: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class InboundMessage:
    wamid: str
    sender: str
    type: str
    text: str | None
    timestamp: int | None
    raw: dict = field(default_factory=dict)


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Validate ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body with the app secret)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_payload(payload: dict) -> tuple[list[StatusUpdate], list[InboundMessage]]:
    statuses: list[StatusUpdate] = []
    messages: list[InboundMessage] = []
    if payload.get("object") != "whatsapp_business_account":
        return statuses, messages
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            for s in value.get("statuses", []) or []:
                errors = s.get("errors") or []
                err = errors[0] if errors else {}
                statuses.append(StatusUpdate(
                    wamid=s.get("id", ""),
                    status=s.get("status", ""),
                    timestamp=_int(s.get("timestamp")),
                    recipient=s.get("recipient_id"),
                    error_code=_int(err.get("code")) if err else None,
                    error_title=err.get("title") if err else None,
                    raw=s,
                ))
            for m in value.get("messages", []) or []:
                mtype = m.get("type", "")
                text = None
                if mtype == "text":
                    text = (m.get("text") or {}).get("body")
                elif mtype == "button":
                    text = (m.get("button") or {}).get("text")
                messages.append(InboundMessage(
                    wamid=m.get("id", ""), sender=m.get("from", ""), type=mtype,
                    text=text, timestamp=_int(m.get("timestamp")), raw=m,
                ))
    return statuses, messages


StatusHandler = Callable[[StatusUpdate], Awaitable[None]]
MessageHandler = Callable[[InboundMessage], Awaitable[None]]


class WebhookHandler:
    """aiohttp handlers for GET (verification) and POST (notifications)."""

    def __init__(self, verify_token: str, app_secret: str,
                 on_status: StatusHandler, on_message: MessageHandler) -> None:
        self.verify_token = verify_token
        self.app_secret = app_secret
        self.on_status = on_status
        self.on_message = on_message
        if not app_secret:
            log.warning("whatsapp.app_secret is empty: webhook signatures will NOT be verified")

    def register(self, app: web.Application, path: str) -> None:
        app.router.add_get(path, self.handle_verify)
        app.router.add_post(path, self.handle_notify)

    async def handle_verify(self, request: web.Request) -> web.Response:
        q = request.query
        if q.get("hub.mode") == "subscribe" and hmac.compare_digest(
            q.get("hub.verify_token", ""), self.verify_token
        ):
            return web.Response(text=q.get("hub.challenge", ""))
        return web.Response(status=403, text="forbidden")

    async def handle_notify(self, request: web.Request) -> web.Response:
        body = await request.read()
        if self.app_secret and not verify_signature(
            self.app_secret, body, request.headers.get("X-Hub-Signature-256")
        ):
            log.warning("Rejected webhook with invalid signature from %s", request.remote)
            return web.Response(status=401, text="invalid signature")
        try:
            payload = json.loads(body)
        except ValueError:
            return web.Response(status=400, text="invalid json")
        statuses, messages = parse_payload(payload)
        for s in statuses:
            try:
                await self.on_status(s)
            except Exception:
                log.exception("Error handling status %s", s.wamid)
        for m in messages:
            try:
                await self.on_message(m)
            except Exception:
                log.exception("Error handling inbound message %s", m.wamid)
        # Always 200 quickly, otherwise Meta keeps retrying the same payload.
        return web.Response(text="ok")
