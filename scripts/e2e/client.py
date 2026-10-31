import asyncio, json, time, uuid
import aiohttp
HS = "http://localhost:8008"; FAKE = "http://localhost:9999"; BOT = "@wacloud:localtest"

async def main():
    async with aiohttp.ClientSession() as s:
        r = await (await s.post(f"{HS}/_matrix/client/v3/login", json={"type": "m.login.password", "identifier": {"type": "m.id.user", "user": "allan"}, "password": "senha-teste-123"})).json()
        H = {"Authorization": f"Bearer {r['access_token']}"}
        room = (await (await s.post(f"{HS}/_matrix/client/v3/createRoom", json={"name": "Avisos BSI", "preset": "private_chat"}, headers=H)).json())["room_id"]
        print("room", room)
        await s.post(f"{HS}/_matrix/client/v3/rooms/{room}/invite", json={"user_id": BOT}, headers=H)
        for _ in range(30):
            jm = await (await s.get(f"{HS}/_matrix/client/v3/rooms/{room}/joined_members", headers=H)).json()
            if BOT in jm.get("joined", {}): break
            await asyncio.sleep(0.5)
        print("bot joined:", BOT in jm.get("joined", {}))
        async def say(text):
            await s.put(f"{HS}/_matrix/client/v3/rooms/{room}/send/m.room.message/{uuid.uuid4().hex}", json={"msgtype": "m.text", "body": text}, headers=H)
            await asyncio.sleep(1.0)
        await say("!wa vincular BSI aviso_unirio pt_BR")
        await say("!wa inscrever (21) 90000-0001 formulario-teste")
        await say("!wa inscrever 21900000002")
        await say("!wa inscrever 21900000003")
        await say("Não haverá aula hoje no campus Urca.\nMotivo: falta de água.")
        await asyncio.sleep(1)
        reqs = await (await s.get(f"{FAKE}/_test/requests")).json()
        print("META recebeu", len(reqs), "envios:")
        for q in reqs: print("  ", q["wamid"], q["body"]["to"], q["body"]["template"]["name"], [p["text"] for p in q["body"]["template"]["components"][0]["parameters"]])
        w = {q["body"]["to"]: q["wamid"] for q in reqs}
        bad = await (await s.post(f"{FAKE}/_test/webhook", json={"bad_signature": True, "statuses": [{"wamid": w["5521900000001"], "status": "read", "recipient": "5521900000001"}]})).json()
        print("webhook assinatura inválida ->", bad)
        st = [
            {"wamid": w["5521900000001"], "status": "sent", "recipient": "5521900000001"},
            {"wamid": w["5521900000001"], "status": "read", "recipient": "5521900000001"},
            {"wamid": w["5521900000001"], "status": "delivered", "recipient": "5521900000001"},
            {"wamid": w["5521900000002"], "status": "delivered", "recipient": "5521900000002"},
            {"wamid": w["5521900000003"], "status": "failed", "recipient": "5521900000003", "error": 130497},
        ]
        print("webhook statuses ->", await (await s.post(f"{FAKE}/_test/webhook", json={"statuses": st})).json())
        print("webhook inbound SAIR ->", await (await s.post(f"{FAKE}/_test/webhook", json={"messages": [{"from": "5521900000002", "text": "Sair"}]})).json())
        await asyncio.sleep(1)
        await say("!wa status")
        await say("!wa inscritos")
        reqs = await (await s.get(f"{FAKE}/_test/requests")).json()
        print("último envio à META:", json.dumps(reqs[-1]["body"], ensure_ascii=False))
        msgs = await (await s.get(f"{HS}/_matrix/client/v3/rooms/{room}/messages", params={"dir": "f", "limit": "100"}, headers=H)).json()
        print("---- conversa na sala ----")
        for e in msgs["chunk"]:
            if e["type"] == "m.room.message":
                who = "BOT " if e["sender"] == BOT else "ALLAN"
                print(f"[{who}] {e['content']['body']}")
asyncio.run(main())
