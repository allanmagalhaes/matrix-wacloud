"""Ajusta o homeserver.yaml gerado pelo Synapse para este deploy:
Postgres, registro do appservice, public_baseurl e registro de usuários fechado.

Uso (dentro do container do bridge, que já tem PyYAML e o .env carregado):
    docker compose run --rm -v ./data/synapse:/synapse bridge \
        python /app/deploy/patch_homeserver.py /synapse/homeserver.yaml
"""

import os
import shutil
import sys

import yaml


def main(path: str) -> None:
    domain = os.environ["MATRIX_DOMAIN"]
    password = os.environ["POSTGRES_PASSWORD"]
    shutil.copyfile(path, path + ".orig")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg["public_baseurl"] = f"https://{domain}/"
    cfg["database"] = {
        "name": "psycopg2",
        "args": {"user": "synapse", "password": password, "database": "synapse",
                 "host": "postgres", "cp_min": 5, "cp_max": 10},
    }
    cfg["app_service_config_files"] = ["/bridge/registration.yaml"]
    cfg["enable_registration"] = False
    cfg["suppress_key_server_warning"] = True
    for listener in cfg.get("listeners", []):
        if listener.get("port") == 8008:
            listener["x_forwarded"] = True  # atrás do Caddy

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    print(f"{path} atualizado (backup em {path}.orig)")


if __name__ == "__main__":
    main(sys.argv[1])
