#!/usr/bin/env bash
# Teste de integração ponta a ponta, sem Docker e sem a Meta de verdade:
# Synapse real (SQLite) + matrix-wacloud + uma "Graph API" falsa (fake_meta.py)
# que registra os envios e devolve webhooks assinados.
#
# Pré-requisitos (num virtualenv):  pip install -e '.[dev]' matrix-synapse
# Uso:  bash scripts/e2e/run_e2e.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT
cd "$WORK"

export MATRIX_DOMAIN=localtest MATRIX_ADMIN_LOCALPART=allan
export MWC_HOMESERVER_URL=http://localhost:8008 MWC_APPSERVICE_URL=http://localhost:29330
export MWC_AS_TOKEN=astoken_e2e MWC_HS_TOKEN=hstoken_e2e
export WA_PHONE_NUMBER_ID=000000 WA_ACCESS_TOKEN=fake-token WA_APP_SECRET=fake-app-secret WA_VERIFY_TOKEN=verify-e2e
export MWC_DATA_DIR="$WORK/data"
mkdir -p data

sed 's#api_version: v26.0#api_version: v26.0\n  api_base: http://localhost:9999#' "$ROOT/config.example.yaml" > config.yaml
matrix-wacloud generate-registration -c config.yaml -o registration.yaml >/dev/null

python -m synapse.app.homeserver --server-name localtest --config-path hs.yaml --generate-config --report-stats=no >/dev/null
# atenção: o homeserver.yaml gerado termina sem quebra de linha
printf '\napp_service_config_files: [%s/registration.yaml]\nsuppress_key_server_warning: true\n' "$WORK" >> hs.yaml
cat >> hs.yaml <<'YAML'
rc_message: {per_second: 1000, burst_count: 1000}
rc_login: {address: {per_second: 1000, burst_count: 1000}, account: {per_second: 1000, burst_count: 1000}, failed_attempts: {per_second: 1000, burst_count: 1000}}
rc_joins: {local: {per_second: 1000, burst_count: 1000}, remote: {per_second: 1000, burst_count: 1000}}
rc_invites: {per_room: {per_second: 1000, burst_count: 1000}, per_user: {per_second: 1000, burst_count: 1000}}
YAML

python -m synapse.app.homeserver -c hs.yaml > synapse.out 2>&1 &
python "$HERE/fake_meta.py" > fake.out 2>&1 &
for _ in $(seq 1 60); do curl -s localhost:8008/_matrix/client/versions >/dev/null && break; sleep 1; done
register_new_matrix_user -c hs.yaml -u allan -p senha-teste-123 --admin http://localhost:8008 >/dev/null

matrix-wacloud run -c config.yaml > bridge.out 2>&1 &
for _ in $(seq 1 30); do grep -q "Bridge ready" bridge.out && break; sleep 1; done

python "$HERE/client.py"
echo "---- log do bridge ----"
grep "matrix_wacloud" bridge.out | tail -15
echo "(arquivos temporários em $WORK)"
