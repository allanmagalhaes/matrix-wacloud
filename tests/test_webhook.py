import hashlib
import hmac
import json

from aiohttp import web

from matrix_wacloud.webhook import WebhookHandler, parse_payload, verify_signature

SECRET = "app-secret"

# Shape of a real payload received on 23/09/2026 (phone numbers/ids anonymised).
FAILED_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [{"id": "111", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": "222"},
        "statuses": [{
            "id": "wamid.FAIL", "status": "failed", "timestamp": "1790197715",
            "recipient_id": "5521900000001",
            "errors": [{"code": 130497,
                        "title": "Business account is restricted from messaging users in this country."}],
        }],
    }}]}],
}

INBOUND_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [{"id": "111", "changes": [{"field": "messages", "value": {
        "messages": [{"from": "5521900000001", "id": "wamid.IN", "timestamp": "1790200000",
                      "type": "text", "text": {"body": "sair"}}],
    }}]}],
}


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature():
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(body, "other"))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "md5=abc")


def test_parse_failed_status():
    statuses, messages = parse_payload(FAILED_PAYLOAD)
    assert messages == []
    (s,) = statuses
    assert (s.wamid, s.status, s.timestamp, s.error_code) == ("wamid.FAIL", "failed", 1790197715, 130497)


def test_parse_inbound_and_ignores_other_objects():
    _, messages = parse_payload(INBOUND_PAYLOAD)
    assert messages[0].sender == "5521900000001" and messages[0].text == "sair"
    assert parse_payload({"object": "page"}) == ([], [])


async def _client(aiohttp_client_factory, received):
    async def on_status(s):
        received.append(("status", s))

    async def on_message(m):
        received.append(("message", m))

    app = web.Application()
    WebhookHandler("verify-me", SECRET, on_status, on_message).register(app, "/wh")
    return await aiohttp_client_factory(app)


async def test_http_verification_and_signature(aiohttp_client):
    received = []
    client = await _client(aiohttp_client, received)

    r = await client.get("/wh", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                        "hub.challenge": "12345"})
    assert r.status == 200 and await r.text() == "12345"
    r = await client.get("/wh", params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                        "hub.challenge": "1"})
    assert r.status == 403

    body = json.dumps(FAILED_PAYLOAD).encode()
    r = await client.post("/wh", data=body, headers={"X-Hub-Signature-256": sign(body, "bad")})
    assert r.status == 401 and received == []
    r = await client.post("/wh", data=body, headers={"X-Hub-Signature-256": sign(body)})
    assert r.status == 200
    assert received[0][0] == "status" and received[0][1].error_code == 130497
