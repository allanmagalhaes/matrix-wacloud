import hashlib, hmac, json, itertools
import aiohttp
from aiohttp import web
SECRET = "fake-app-secret"
BRIDGE = "http://localhost:29330/whatsapp/webhook"
reqs, counter = [], itertools.count(1)

async def messages(request):
    body = await request.json()
    assert request.headers["Authorization"] == "Bearer fake-token"
    wamid = f"wamid.E2E{next(counter)}"
    reqs.append({"wamid": wamid, "path": request.path, "body": body})
    return web.json_response({"messaging_product": "whatsapp", "contacts": [{"input": body["to"], "wa_id": body["to"]}], "messages": [{"id": wamid}]})

async def get_reqs(request):
    return web.json_response(reqs)

async def trigger(request):
    d = await request.json()  # {statuses:[{wamid,status,recipient,error?}], messages:[{from,text}]}
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "1338355389366744"}}
    if d.get("statuses"):
        value["statuses"] = []
        for s in d["statuses"]:
            st = {"id": s["wamid"], "status": s["status"], "timestamp": "1790200000", "recipient_id": s["recipient"]}
            if s.get("error"):
                st["errors"] = [{"code": s["error"], "title": "simulated error"}]
            value["statuses"].append(st)
    if d.get("messages"):
        value["messages"] = [{"from": m["from"], "id": "wamid.IN1", "timestamp": "1790200001", "type": "text", "text": {"body": m["text"]}} for m in d["messages"]]
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "1324472779656999", "changes": [{"field": "messages", "value": value}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if d.get("bad_signature"):
        sig = "sha256=deadbeef"
    async with aiohttp.ClientSession() as s:
        async with s.post(BRIDGE, data=raw, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"}) as r:
            return web.json_response({"bridge_status": r.status, "bridge_body": await r.text()})

app = web.Application()
app.router.add_post("/v26.0/{pid}/messages", messages)
app.router.add_get("/_test/requests", get_reqs)
app.router.add_post("/_test/webhook", trigger)
web.run_app(app, port=9999, print=None)
