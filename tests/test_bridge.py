"""Bridge logic with the Matrix and WhatsApp sides replaced by fakes."""

import pytest

from matrix_wacloud.bridge import Bridge
from matrix_wacloud.cloudapi import CloudAPIError
from matrix_wacloud.config import parse_config
from matrix_wacloud.webhook import InboundMessage, StatusUpdate

ADMIN = "@admin:example.org"
ROOM = "!sala:example.org"


class FakeWA:
    def __init__(self):
        self.sent = []
        self.texts = []
        self.fail_for = {}

    async def send_template(self, to, name, language, params=None):
        if to in self.fail_for:
            raise CloudAPIError(self.fail_for[to], "simulated")
        self.sent.append((to, name, language, params))
        return f"wamid.{len(self.sent)}"

    async def send_text(self, to, text):
        self.texts.append((to, text))
        return "wamid.text"


@pytest.fixture
async def bridge(tmp_path):
    cfg = parse_config({
        "homeserver": {"address": "http://hs:8008", "domain": "example.org"},
        "appservice": {"id": "wacloud", "bot_localpart": "wacloud", "as_token": "a", "hs_token": "h",
                       "address": "http://bridge:29330"},
        "whatsapp": {"phone_number_id": "1", "access_token": "t", "verify_token": "v",
                     "default_template": "aviso_unirio", "default_language": "pt_BR",
                     "template_params": {"hello_world": [], "aviso_unirio": ["label", "text"]}},
        "database": str(tmp_path / "b.db"),
        "state_file": str(tmp_path / "state.json"),
        "admins": [ADMIN],
    })
    b = Bridge(cfg)
    b.wa = FakeWA()
    b.replies = []

    async def reply(room_id, text):
        b.replies.append(text)

    b._reply = reply
    yield b
    b.store.close()


async def cmd(b, text, sender=ADMIN):
    await b._handle_command(ROOM, sender, text)
    return b.replies[-1]


async def test_full_flow(bridge):
    assert "vinculada" in await cmd(bridge, 'vincular "Curso BSI"')
    assert bridge.store.get_link(ROOM).template == "aviso_unirio"
    assert "inscrito" in await cmd(bridge, "inscrever (21) 90000-0001 formulario-teste")
    await cmd(bridge, "inscrever 21900000002")
    assert "2 número(s)" in await cmd(bridge, "inscritos")

    bridge.wa.fail_for["5521900000002"] = 131026
    await bridge.broadcast(ROOM, "$ev1", ADMIN, "Sem aula hoje.\nFalta de água no campus.")
    assert bridge.wa.sent == [("5521900000001", "aviso_unirio", "pt_BR",
                               ["Curso BSI", "Sem aula hoje. | Falta de água no campus."])]
    assert "1 aceito(s)" in bridge.replies[-1] and "131026" in bridge.replies[-1]

    # same event again (homeserver retry) must not resend
    await bridge.broadcast(ROOM, "$ev1", ADMIN, "Sem aula hoje.")
    assert len(bridge.wa.sent) == 1

    await bridge.handle_status(StatusUpdate("wamid.1", "delivered", 10, "5521900000001"))
    await bridge.handle_status(StatusUpdate("wamid.1", "read", 20, "5521900000001"))
    status = await cmd(bridge, "status")
    assert "2 destinatário(s)" in status and "1 lidas" in status and "1 falharam" in status


async def test_hello_world_sends_no_params(bridge):
    await cmd(bridge, "vincular Teste hello_world en_US")
    await cmd(bridge, "inscrever 21900000001")
    await bridge.broadcast(ROOM, "$ev", ADMIN, "qualquer coisa")
    assert bridge.wa.sent[0][1:] == ("hello_world", "en_US", [])


async def test_no_subscribers_and_unlinked(bridge):
    await bridge.broadcast(ROOM, "$x", ADMIN, "oi")   # not linked: silently ignored
    assert bridge.replies == []
    await cmd(bridge, "vincular X")
    await bridge.broadcast(ROOM, "$y", ADMIN, "oi")
    assert "Nenhum número" in bridge.replies[-1]


async def test_invalid_phone_and_unknown_command(bridge):
    await cmd(bridge, "vincular X")
    assert "Invalid phone" in await cmd(bridge, "inscrever 123")
    assert "desconhecido" in await cmd(bridge, "foo")


async def test_inbound_opt_out(bridge):
    await cmd(bridge, "vincular X")
    await cmd(bridge, "inscrever 21900000001")
    await bridge.handle_inbound(InboundMessage("wamid.in", "5521900000001", "text", " Sair ", 1))
    assert bridge.store.active_subscribers(ROOM) == []
    assert bridge.wa.texts and bridge.wa.texts[0][0] == "5521900000001"
