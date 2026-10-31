"""The bridge itself: glues the Matrix application service, the SQLite store,
the Cloud API client and the webhook receiver together.

Flow:
  1. An admin invites the bot to a room and runs ``!wa vincular <rótulo>``.
  2. Subscribers are added (``!wa inscrever`` or ``matrix-wacloud import-csv``).
  3. Any plain text message an admin posts in a linked room becomes a
     WhatsApp template message to every active subscriber of that room.
  4. Meta's status webhooks (sent/delivered/read/failed) are stored and can be
     inspected with ``!wa status``.
  5. A subscriber who replies SAIR/PARAR/STOP on WhatsApp is opted out.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path

from mautrix.appservice import AppService
from mautrix.appservice.state_store import FileASStateStore
from mautrix.types import Event, EventType, Membership, MessageType

from .cloudapi import CloudAPIClient, CloudAPIError
from .config import Config
from .store import Store
from .util import InvalidPhone, mask_phone, normalize_phone, sanitize_param, split_phone_args
from .webhook import InboundMessage, StatusUpdate, WebhookHandler

log = logging.getLogger("matrix_wacloud.bridge")

OPT_OUT_KEYWORDS = {"SAIR", "PARAR", "STOP", "CANCELAR", "DESCADASTRAR"}

HELP = """Comandos (apenas administradores):
!wa vincular <rótulo> [template] [idioma] — liga esta sala ao WhatsApp
!wa desvincular — desliga esta sala
!wa inscrever <telefone> [origem do consentimento] — inscreve um número
!wa remover <telefone> — remove um número
!wa inscritos — quantos números ativos esta sala tem
!wa status — situação das entregas do último aviso
Qualquer outra mensagem de texto de um administrador numa sala vinculada é enviada aos inscritos."""

STATUS_LABELS = [("read", "lidas"), ("delivered", "entregues"), ("sent", "enviadas"),
                 ("accepted", "aceitas pela API"), ("queued", "na fila"), ("failed", "falharam")]


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = Store(config.database)
        Path(config.state_file).parent.mkdir(parents=True, exist_ok=True)
        hs, asc = config.homeserver, config.appservice
        self.az = AppService(
            server=hs.address, domain=hs.domain, verify_ssl=hs.verify_ssl,
            as_token=asc.as_token, hs_token=asc.hs_token,
            bot_localpart=asc.bot_localpart, id=asc.id,
            state_store=FileASStateStore(config.state_file, binary=False),
            log="matrix_wacloud.appservice",
        )
        self.az.matrix_event_handler(self.handle_event)
        self.webhook = WebhookHandler(
            verify_token=config.whatsapp.verify_token, app_secret=config.whatsapp.app_secret,
            on_status=self.handle_status, on_message=self.handle_inbound,
        )
        self.webhook.register(self.az.app, config.whatsapp.webhook_path)
        self.wa: CloudAPIClient | None = None

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        asc, wa = self.config.appservice, self.config.whatsapp
        await self.az.start(asc.hostname, asc.port)
        self.wa = CloudAPIClient(
            self.az.http_session, wa.phone_number_id, wa.access_token,
            api_base=wa.api_base, api_version=wa.api_version, max_retries=wa.max_retries,
        )
        # The homeserver may not be up yet when running under docker compose.
        for attempt in range(30):
            try:
                await self.az.intent.ensure_registered()
                break
            except Exception as e:
                log.info("Homeserver not reachable yet (%s), retrying...", e)
                await asyncio.sleep(2)
        else:
            raise RuntimeError("Could not reach the homeserver")
        try:
            await self.az.intent.set_displayname(asc.bot_displayname)
        except Exception:
            log.warning("Could not set bot display name", exc_info=True)
        self.az.ready = True
        log.info("Bridge ready as %s, webhook at %s", self.config.bot_mxid, wa.webhook_path)

    async def stop(self) -> None:
        await self.az.stop()
        self.store.close()

    # ---- Matrix side -----------------------------------------------------
    def is_admin(self, user_id: str) -> bool:
        return user_id in self.config.admins

    async def handle_event(self, evt: Event) -> None:
        if evt.sender == self.config.bot_mxid:
            return
        if evt.type == EventType.ROOM_MEMBER:
            await self._handle_member(evt)
        elif evt.type == EventType.ROOM_MESSAGE:
            await self._handle_message(evt)

    async def _handle_member(self, evt) -> None:
        if evt.state_key != self.config.bot_mxid or evt.content.membership != Membership.INVITE:
            return
        if not self.is_admin(evt.sender):
            log.info("Ignoring invite to %s from non-admin %s", evt.room_id, evt.sender)
            return
        await self.az.intent.join_room(evt.room_id)
        await self.az.intent.send_notice(
            evt.room_id, "Olá! Use `!wa vincular <rótulo>` para ligar esta sala ao WhatsApp. `!wa ajuda` lista os comandos."
        )

    async def _handle_message(self, evt) -> None:
        content = evt.content
        if getattr(content, "msgtype", None) != MessageType.TEXT:
            return
        relates = getattr(content, "relates_to", None)
        if relates is not None and str(getattr(relates, "rel_type", "") or "") == "m.replace":
            return  # ignore edits: the original message was already sent
        body = (content.body or "").strip()
        if not body or not self.is_admin(evt.sender):
            return
        if body.lower().startswith("!wa"):
            await self._handle_command(evt.room_id, evt.sender, body[3:].strip())
            return
        if self.store.get_link(evt.room_id):
            await self.broadcast(evt.room_id, evt.event_id, evt.sender, body)

    async def _reply(self, room_id: str, text: str) -> None:
        await self.az.intent.send_notice(room_id, text)

    async def _handle_command(self, room_id: str, sender: str, args_str: str) -> None:
        try:
            args = shlex.split(args_str)
        except ValueError:
            args = args_str.split()
        cmd = args[0].lower() if args else "ajuda"
        rest = args[1:]
        wa = self.config.whatsapp

        if cmd in ("ajuda", "help"):
            await self._reply(room_id, HELP)
        elif cmd == "vincular":
            if not rest:
                await self._reply(room_id, "Uso: !wa vincular <rótulo> [template] [idioma]")
                return
            label = rest[0]
            template = rest[1] if len(rest) > 1 else wa.default_template
            language = rest[2] if len(rest) > 2 else wa.default_language
            self.store.link_room(room_id, label, template, language, sender)
            await self._reply(room_id, f"Sala vinculada como '{label}' (template {template}, idioma {language}).")
        elif cmd == "desvincular":
            ok = self.store.unlink_room(room_id)
            await self._reply(room_id, "Sala desvinculada." if ok else "Esta sala não estava vinculada.")
        elif cmd == "inscrever":
            if not rest:
                await self._reply(room_id, "Uso: !wa inscrever <telefone> [origem do consentimento]")
                return
            if not self.store.get_link(room_id):
                await self._reply(room_id, "Vincule a sala primeiro com !wa vincular.")
                return
            phone_raw, extra = split_phone_args(rest)
            try:
                phone = normalize_phone(phone_raw)
            except InvalidPhone as e:
                await self._reply(room_id, str(e))
                return
            source = " ".join(extra) or f"manual:{sender}"
            new = self.store.add_subscriber(room_id, phone, source)
            await self._reply(room_id, f"{mask_phone(phone)} {'inscrito' if new else 'reativado'}.")
        elif cmd == "remover":
            if not rest:
                await self._reply(room_id, "Uso: !wa remover <telefone>")
                return
            try:
                phone = normalize_phone(split_phone_args(rest)[0])
            except InvalidPhone as e:
                await self._reply(room_id, str(e))
                return
            ok = self.store.remove_subscriber(room_id, phone)
            await self._reply(room_id, f"{mask_phone(phone)} removido." if ok else "Número não encontrado.")
        elif cmd == "inscritos":
            n = len(self.store.active_subscribers(room_id))
            await self._reply(room_id, f"{n} número(s) ativo(s) nesta sala.")
        elif cmd == "status":
            await self._reply(room_id, self.status_text(room_id))
        else:
            await self._reply(room_id, f"Comando desconhecido: {cmd}. Use !wa ajuda.")

    def status_text(self, room_id: str) -> str:
        alert = self.store.last_alert(room_id)
        if alert is None:
            return "Nenhum aviso enviado nesta sala ainda."
        summary = self.store.delivery_summary(alert["id"])
        total = sum(summary.values())
        parts = [f"{summary[k]} {label}" for k, label in STATUS_LABELS if summary.get(k)]
        text = f"Último aviso: {total} destinatário(s) — " + (", ".join(parts) or "sem entregas")
        fails = self.store.failures(alert["id"])
        if fails:
            codes: dict[str, int] = {}
            for f in fails:
                key = f"{f['error_code']} ({f['error_title']})"
                codes[key] = codes.get(key, 0) + 1
            text += "\nFalhas: " + "; ".join(f"{k}: {n}" for k, n in codes.items())
        return text

    # ---- broadcast -------------------------------------------------------
    async def _sender_name(self, room_id: str, user_id: str) -> str:
        try:
            member = await self.az.intent.get_room_member_info(room_id, user_id)
            if member and member.displayname:
                return member.displayname
        except Exception:
            pass
        return user_id.split(":")[0].lstrip("@")

    async def broadcast(self, room_id: str, event_id: str, sender: str, text: str) -> None:
        link = self.store.get_link(room_id)
        if link is None:
            return
        alert_id = self.store.create_alert(room_id, event_id, sender, text)
        if alert_id is None:
            return  # duplicate transaction
        phones = self.store.active_subscribers(room_id)
        if not phones:
            await self._reply(room_id, "Nenhum número inscrito nesta sala; nada foi enviado ao WhatsApp.")
            return
        wa = self.config.whatsapp
        tokens = wa.params_for(link.template)
        values = {"label": link.label, "text": text}
        if "sender" in tokens:
            values["sender"] = await self._sender_name(room_id, sender)
        params = [sanitize_param(values[t]) for t in tokens]

        sem = asyncio.Semaphore(max(1, wa.max_concurrency))
        ok = failed = 0
        errors: dict[str, int] = {}

        async def send_one(phone: str) -> None:
            nonlocal ok, failed
            delivery_id = self.store.create_delivery(alert_id, phone)
            async with sem:
                try:
                    wamid = await self.wa.send_template(phone, link.template, link.language, params)
                except CloudAPIError as e:
                    failed += 1
                    errors[f"{e.code}"] = errors.get(f"{e.code}", 0) + 1
                    self.store.mark_failed(delivery_id, e.code, e.title)
                    log.warning("Send to %s failed: %s", mask_phone(phone), e)
                    return
            ok += 1
            self.store.mark_accepted(delivery_id, wamid)

        await asyncio.gather(*(send_one(p) for p in phones))
        msg = f"Encaminhado ao WhatsApp: {ok} aceito(s) pela API"
        if failed:
            msg += f", {failed} falha(s) [códigos: {', '.join(f'{c}×{n}' for c, n in errors.items())}]"
        msg += ". Use !wa status para acompanhar entregas e leituras."
        await self._reply(room_id, msg)

    # ---- WhatsApp side (webhooks) -----------------------------------------
    async def handle_status(self, s: StatusUpdate) -> None:
        matched = self.store.apply_status(s.wamid, s.status, s.timestamp, s.raw,
                                          s.error_code, s.error_title)
        log.info("Status %s for %s%s%s", s.status, mask_phone(s.recipient or ""),
                 f" (error {s.error_code})" if s.error_code else "",
                 "" if matched else " [unknown message]")

    async def handle_inbound(self, m: InboundMessage) -> None:
        word = (m.text or "").strip().upper()
        if word in OPT_OUT_KEYWORDS:
            n = self.store.opt_out_everywhere(m.sender)
            log.info("Opt-out from %s (%d subscription(s))", mask_phone(m.sender), n)
            try:
                # The user just wrote to us, so the 24h window is open: free text is allowed.
                await self.wa.send_text(m.sender, "Pronto: você não receberá mais avisos por aqui.")
            except CloudAPIError as e:
                log.warning("Could not confirm opt-out: %s", e)
        else:
            log.info("Inbound %s message from %s (ignored)", m.type, mask_phone(m.sender))
