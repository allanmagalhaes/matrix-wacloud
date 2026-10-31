"""Cloud API client against a local fake Graph API server."""

import aiohttp
import pytest
from aiohttp import web

from matrix_wacloud.cloudapi import CloudAPIClient, CloudAPIError


def test_template_payload_with_and_without_params():
    p = CloudAPIClient.template_payload("5521900000001", "aviso", "pt_BR", ["BSI", "Sem aula"])
    assert p["type"] == "template"
    assert p["template"]["language"] == {"code": "pt_BR"}
    assert p["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "BSI"}, {"type": "text", "text": "Sem aula"}]
    p2 = CloudAPIClient.template_payload("5521900000001", "hello_world", "en_US", [])
    assert "components" not in p2["template"]


@pytest.fixture
async def fake_graph(aiohttp_server):
    """Serves a queue of canned responses and records requests."""
    state = {"responses": [], "requests": []}

    async def messages(request: web.Request):
        state["requests"].append({"auth": request.headers.get("Authorization"), "json": await request.json()})
        status, body = state["responses"].pop(0)
        if isinstance(body, dict):
            return web.json_response(body, status=status)
        return web.Response(status=status, text=body)

    app = web.Application()
    app.router.add_post("/v26.0/123/messages", messages)
    server = await aiohttp_server(app)
    state["base"] = str(server.make_url("")).rstrip("/")
    return state


async def make_client(session, fake, **kw):
    return CloudAPIClient(session, "123", "tok", api_base=fake["base"], backoff_base=0, **kw)


async def test_send_template_success(fake_graph):
    fake_graph["responses"] = [(200, {"messaging_product": "whatsapp", "messages": [{"id": "wamid.ABC"}]})]
    async with aiohttp.ClientSession() as s:
        client = await make_client(s, fake_graph)
        assert await client.send_template("5521900000001", "hello_world", "en_US") == "wamid.ABC"
    req = fake_graph["requests"][0]
    assert req["auth"] == "Bearer tok"
    assert req["json"]["template"]["name"] == "hello_world"


async def test_non_retryable_error_is_raised_immediately(fake_graph):
    fake_graph["responses"] = [(400, {"error": {
        "message": "(#132001) Template name does not exist in the translation", "code": 132001,
        "error_data": {"details": "template name (x) does not exist in pt_BR"}, "fbtrace_id": "F1"}})]
    async with aiohttp.ClientSession() as s:
        client = await make_client(s, fake_graph, max_retries=3)
        with pytest.raises(CloudAPIError) as ei:
            await client.send_template("5521900000001", "x", "pt_BR")
    assert ei.value.code == 132001 and not ei.value.retryable
    assert ei.value.fbtrace_id == "F1" and "pt_BR" in ei.value.details
    assert len(fake_graph["requests"]) == 1


async def test_retryable_errors_then_success(fake_graph):
    fake_graph["responses"] = [
        (400, {"error": {"message": "(#130429) Rate limit hit", "code": 130429}}),
        (503, "unavailable"),
        (200, {"messages": [{"id": "wamid.OK"}]}),
    ]
    async with aiohttp.ClientSession() as s:
        client = await make_client(s, fake_graph, max_retries=3)
        assert await client.send_template("5521900000001", "t", "pt_BR") == "wamid.OK"
    assert len(fake_graph["requests"]) == 3


async def test_retries_exhausted(fake_graph):
    fake_graph["responses"] = [(500, "err"), (500, "err")]
    async with aiohttp.ClientSession() as s:
        client = await make_client(s, fake_graph, max_retries=1)
        with pytest.raises(CloudAPIError) as ei:
            await client.send_template("5521900000001", "t", "pt_BR")
    assert ei.value.http_status == 500
    assert len(fake_graph["requests"]) == 2


async def test_send_text_payload(fake_graph):
    fake_graph["responses"] = [(200, {"messages": [{"id": "wamid.T"}]})]
    async with aiohttp.ClientSession() as s:
        client = await make_client(s, fake_graph)
        await client.send_text("5521900000001", "oi")
    assert fake_graph["requests"][0]["json"]["text"] == {"preview_url": False, "body": "oi"}
