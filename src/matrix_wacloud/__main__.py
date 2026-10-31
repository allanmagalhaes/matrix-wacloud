"""Command line interface.

    matrix-wacloud generate-tokens
    matrix-wacloud generate-registration -c config.yaml -o registration.yaml
    matrix-wacloud run -c config.yaml
    matrix-wacloud import-csv -c config.yaml --room '!sala:dominio' inscritos.csv
    matrix-wacloud send-test -c config.yaml --to 5521999999999
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re
import secrets
import signal
import sys
from datetime import datetime

import aiohttp
import yaml

from . import __version__
from .config import ConfigError, load_config


def cmd_generate_tokens(_args) -> int:
    print(f"MWC_AS_TOKEN={secrets.token_urlsafe(48)}")
    print(f"MWC_HS_TOKEN={secrets.token_urlsafe(48)}")
    print("# Cole estas linhas no seu .env (nunca no git).")
    return 0


def build_registration(cfg) -> dict:
    asc, domain = cfg.appservice, cfg.homeserver.domain
    return {
        "id": asc.id,
        "url": asc.address,
        "as_token": asc.as_token,
        "hs_token": asc.hs_token,
        "sender_localpart": asc.bot_localpart,
        "rate_limited": False,
        "namespaces": {
            "users": [{"exclusive": True, "regex": f"@{re.escape(asc.bot_localpart)}:{re.escape(domain)}"}],
            "aliases": [],
            "rooms": [],
        },
    }


def cmd_generate_registration(args) -> int:
    cfg = load_config(args.config)
    with open(args.output, "w", encoding="utf-8") as f:
        yaml.safe_dump(build_registration(cfg), f, sort_keys=False, allow_unicode=True)
    print(f"Registro gravado em {args.output}. Adicione-o a 'app_service_config_files' no homeserver.yaml.")
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if cfg.log_level != "DEBUG":
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    from .bridge import Bridge  # imported late so other commands don't need mautrix loaded

    async def main() -> None:
        bridge = Bridge(cfg)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await bridge.start()
        await stop.wait()
        await bridge.stop()

    asyncio.run(main())
    return 0


def _parse_consent_ts(value: str | None) -> int | None:
    if not value:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value.strip(), fmt).timestamp())
        except ValueError:
            continue
    return None


def cmd_import_csv(args) -> int:
    from .store import Store
    from .util import InvalidPhone, normalize_phone

    cfg = load_config(args.config)
    store = Store(cfg.database)
    if not store.get_link(args.room):
        print(f"A sala {args.room} não está vinculada. Rode '!wa vincular' nela antes.", file=sys.stderr)
        return 2
    added = updated = skipped = 0
    with open(args.file, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        reader = csv.DictReader(f, dialect=dialect)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
        phone_col = cols.get(args.phone_column.lower())
        if phone_col is None:
            print(f"Coluna '{args.phone_column}' não encontrada. Colunas: {reader.fieldnames}", file=sys.stderr)
            return 2
        consent_col = cols.get(args.consent_column.lower()) if args.consent_column else None
        for row in reader:
            raw = (row.get(phone_col) or "").strip()
            if not raw:
                skipped += 1
                continue
            try:
                phone = normalize_phone(raw)
            except InvalidPhone:
                print(f"Ignorado (telefone inválido): {raw}", file=sys.stderr)
                skipped += 1
                continue
            ts = _parse_consent_ts(row.get(consent_col)) if consent_col else None
            if store.add_subscriber(args.room, phone, f"csv:{args.source}", ts):
                added += 1
            else:
                updated += 1
    print(f"{added} novo(s), {updated} reativado(s)/atualizado(s), {skipped} ignorado(s).")
    return 0


def cmd_send_test(args) -> int:
    cfg = load_config(args.config)
    from .cloudapi import CloudAPIClient, CloudAPIError
    from .util import normalize_phone

    async def main() -> int:
        wa = cfg.whatsapp
        async with aiohttp.ClientSession() as session:
            client = CloudAPIClient(session, wa.phone_number_id, wa.access_token,
                                    api_base=wa.api_base, api_version=wa.api_version, max_retries=0)
            try:
                wamid = await client.send_template(normalize_phone(args.to), args.template, args.language,
                                                   args.param or None)
            except CloudAPIError as e:
                print(f"Falhou: {e}", file=sys.stderr)
                return 1
        print(f"Aceito pela API: {wamid}\nVeja o resultado final (delivered/read/failed) no webhook.")
        return 0

    return asyncio.run(main())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="matrix-wacloud", description="Matrix → WhatsApp Cloud API bridge")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("generate-tokens", help="gera as_token/hs_token aleatórios")
    s.set_defaults(func=cmd_generate_tokens)

    s = sub.add_parser("generate-registration", help="gera o registration.yaml para o homeserver")
    s.add_argument("-c", "--config", required=True)
    s.add_argument("-o", "--output", default="registration.yaml")
    s.set_defaults(func=cmd_generate_registration)

    s = sub.add_parser("run", help="executa o bridge")
    s.add_argument("-c", "--config", required=True)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("import-csv", help="importa inscritos (com consentimento) de um CSV")
    s.add_argument("-c", "--config", required=True)
    s.add_argument("--room", required=True, help="ID da sala Matrix (!abc:dominio)")
    s.add_argument("--phone-column", default="telefone")
    s.add_argument("--consent-column", default="Carimbo de data/hora",
                   help="coluna com data/hora do consentimento (padrão do Google Forms)")
    s.add_argument("--source", default="formulario", help="descrição da origem do consentimento")
    s.add_argument("file")
    s.set_defaults(func=cmd_import_csv)

    s = sub.add_parser("send-test", help="envia um template direto (sem Matrix) para testar credenciais")
    s.add_argument("-c", "--config", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--template", default="hello_world")
    s.add_argument("--language", default="en_US")
    s.add_argument("--param", action="append", help="parâmetro do corpo (repita para vários)")
    s.set_defaults(func=cmd_send_test)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"Erro de configuração: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
