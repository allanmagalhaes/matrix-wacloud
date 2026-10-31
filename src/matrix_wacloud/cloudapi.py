"""Minimal async client for the WhatsApp Business Cloud API (send side)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger("matrix_wacloud.cloudapi")

# Error codes worth retrying (throughput / temporary unavailability / unknown).
RETRYABLE_CODES = {1, 2, 4, 80007, 130429, 131000, 131016, 133004}


@dataclass
class CloudAPIError(Exception):
    code: int | None
    title: str
    details: str = ""
    http_status: int | None = None
    fbtrace_id: str | None = None

    def __str__(self) -> str:
        return f"WhatsApp API error {self.code}: {self.title}" + (f" ({self.details})" if self.details else "")

    @property
    def retryable(self) -> bool:
        return (self.http_status is not None and self.http_status >= 500) or self.code in RETRYABLE_CODES


class CloudAPIClient:
    def __init__(self, session: aiohttp.ClientSession, phone_number_id: str, access_token: str,
                 api_base: str = "https://graph.facebook.com", api_version: str = "v26.0",
                 max_retries: int = 3, backoff_base: float = 1.0) -> None:
        self.session = session
        self.url = f"{api_base.rstrip('/')}/{api_version}/{phone_number_id}/messages"
        self.headers = {"Authorization": f"Bearer {access_token}"}
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    @staticmethod
    def template_payload(to: str, name: str, language: str, body_params: list[str] | None) -> dict:
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        if body_params:
            template["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in body_params],
            }]
        return {"messaging_product": "whatsapp", "recipient_type": "individual",
                "to": to, "type": "template", "template": template}

    @staticmethod
    def text_payload(to: str, text: str) -> dict:
        return {"messaging_product": "whatsapp", "recipient_type": "individual",
                "to": to, "type": "text", "text": {"preview_url": False, "body": text}}

    async def send_template(self, to: str, name: str, language: str,
                            body_params: list[str] | None = None) -> str:
        """Send a template message. Returns the message id (``wamid``)."""
        return await self._post(self.template_payload(to, name, language, body_params))

    async def send_text(self, to: str, text: str) -> str:
        """Free-form text: only delivered inside the 24h customer-service window."""
        return await self._post(self.text_payload(to, text))

    async def _post(self, payload: dict) -> str:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._post_once(payload)
            except CloudAPIError as e:
                if not e.retryable or attempt > self.max_retries:
                    raise
                delay = self.backoff_base * 2 ** (attempt - 1)
                log.warning("Retryable error (%s), attempt %d, retrying in %.1fs", e, attempt, delay)
                await asyncio.sleep(delay)
            except aiohttp.ClientError as e:
                if attempt > self.max_retries:
                    raise CloudAPIError(None, f"Network error: {e}") from e
                delay = self.backoff_base * 2 ** (attempt - 1)
                log.warning("Network error (%s), attempt %d, retrying in %.1fs", e, attempt, delay)
                await asyncio.sleep(delay)

    async def _post_once(self, payload: dict) -> str:
        async with self.session.post(self.url, json=payload, headers=self.headers) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}
            if resp.status >= 400 or "error" in data:
                err = data.get("error", {}) if isinstance(data, dict) else {}
                raise CloudAPIError(
                    code=err.get("code"),
                    title=err.get("message") or f"HTTP {resp.status}",
                    details=(err.get("error_data") or {}).get("details", ""),
                    http_status=resp.status,
                    fbtrace_id=err.get("fbtrace_id"),
                )
            try:
                return data["messages"][0]["id"]
            except (KeyError, IndexError, TypeError):
                raise CloudAPIError(None, "Unexpected response", str(data)[:300], resp.status) from None
