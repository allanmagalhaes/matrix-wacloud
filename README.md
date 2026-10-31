# matrix-wacloud

*Matrix application service that forwards messages posted in Matrix rooms to WhatsApp subscribers through the **official WhatsApp Business Cloud API** — template messages, opt-in subscriber lists, delivery/read receipts and opt-out handling.*

Conector (*bridge*) de notificação entre o [Matrix](https://matrix.org) e a WhatsApp Business Cloud API, desenvolvido como Trabalho de Conclusão de Curso do Bacharelado em Sistemas de Informação da UNIRIO.

Diferente de pontes como o mautrix-whatsapp, que emulam o WhatsApp Web num número pessoal, este pacote usa somente a API oficial da Meta. Por isso ele **não** replica conversas: ele converte avisos publicados numa sala Matrix em **mensagens por template** para os inscritos daquela sala. Essa é a forma permitida pela Meta para mensagens iniciadas pela organização.

```
 sala Matrix ──(appservice)──▶ matrix-wacloud ──(Cloud API)──▶ WhatsApp dos inscritos
      ▲                              │  ▲
      └──── !wa status ◀── SQLite ◀──┘  └──(webhooks: sent/delivered/read/failed)── Meta
```

## Funcionalidades

- Vínculo de salas Matrix a um template aprovado (`!wa vincular`).
- Inscritos por sala, com registro da origem e da data do consentimento (LGPD). Importação de CSV, inclusive do Google Forms.
- Envio concorrente com repetição automática nos erros temporários da API.
- Recepção dos webhooks da Meta com **validação da assinatura `X-Hub-Signature-256`**.
- Acompanhamento de entregas e leituras por aviso (`!wa status`).
- Descadastro: o inscrito responde **SAIR** no WhatsApp e é removido de todas as salas.
- Números mascarados nos logs e nas mensagens do bot.

## Instalação

```bash
pip install matrix-wacloud
```

Requer Python 3.10+ e um homeserver Matrix onde você possa registrar um *application service* (por exemplo, Synapse).

## Configuração rápida

1. Copie `config.example.yaml` para `config.yaml` e `.env.example` para `.env`. Os segredos ficam no `.env`; o YAML os referencia como `${NOME}`.
2. Gere os tokens do appservice e cole no `.env`:
   ```bash
   matrix-wacloud generate-tokens
   ```
3. Preencha no `.env` os dados do painel da Meta: `WA_PHONE_NUMBER_ID`, `WA_ACCESS_TOKEN` (token permanente), `WA_APP_SECRET` (Configurações do app → Básico → Chave secreta) e `WA_VERIFY_TOKEN` (uma senha inventada por você).
4. Gere o registro e adicione-o ao homeserver (`app_service_config_files` no `homeserver.yaml`):
   ```bash
   matrix-wacloud generate-registration -c config.yaml -o registration.yaml
   ```
5. Teste as credenciais da Meta sem passar pelo Matrix:
   ```bash
   matrix-wacloud send-test -c config.yaml --to 5521999999999
   ```
6. Execute:
   ```bash
   matrix-wacloud run -c config.yaml
   ```
7. No painel da Meta (WhatsApp → Configuração → Webhooks), use `https://SEU_DOMINIO/whatsapp/webhook` como URL de callback, informe o mesmo `WA_VERIFY_TOKEN` e assine o campo **messages**. Ative também "Assinar webhooks" na conta do número que envia.

## Uso na sala

Convide `@wacloud:SEU_DOMINIO` para uma sala. Só administradores listados em `admins` podem convidar, comandar e disparar avisos.

| Comando | Efeito |
|---|---|
| `!wa vincular <rótulo> [template] [idioma]` | liga a sala ao WhatsApp |
| `!wa inscrever <telefone> [origem]` | inscreve um número (ex.: `(21) 99999-1234`) |
| `!wa remover <telefone>` | remove um número |
| `!wa inscritos` | quantidade de números ativos |
| `!wa status` | entregas, leituras e falhas do último aviso |
| `!wa desvincular` | desliga a sala |

Qualquer outra mensagem de texto de um administrador numa sala vinculada é enviada aos inscritos.

Para importar inscritos de um formulário:

```bash
matrix-wacloud import-csv -c config.yaml --room '!idDaSala:dominio' respostas.csv \
    --phone-column Telefone --consent-column "Carimbo de data/hora"
```

### Templates

O conteúdo do aviso entra nos parâmetros `{{1}}`, `{{2}}`... do template. A ordem é definida em `template_params`, com os valores possíveis `label` (rótulo da sala), `text` (mensagem) e `sender` (nome de quem postou). Exemplo de template **Utility** em pt_BR:

> Aviso UNIRIO ({{1}}): {{2}}

```yaml
template_params:
  aviso_unirio: [label, text]
```

Quebras de linha viram ` | `, porque a Meta rejeita parâmetros com quebra de linha. Textos longos são truncados.

## Implantação (VPS com Docker)

O `docker-compose.yml` sobe Synapse, Postgres, Caddy (HTTPS automático via Let's Encrypt) e o bridge. Do bridge, só o caminho `/whatsapp/*` fica exposto à internet.

Pré-requisitos: uma VPS com Docker e um domínio com registro DNS do tipo A apontando para ela (ex.: `avisos.seudominio.com`).

```bash
git clone https://github.com/allanmagalhaes/matrix-wacloud && cd matrix-wacloud
cp .env.example .env                      # preencha MATRIX_DOMAIN, POSTGRES_PASSWORD, WA_*...
docker compose build bridge
docker compose run --rm --no-deps bridge matrix-wacloud generate-tokens   # cole no .env

mkdir -p data/bridge && cp config.example.yaml data/bridge/config.yaml
docker compose run --rm --no-deps bridge \
    matrix-wacloud generate-registration -c /data/config.yaml -o /data/registration.yaml

# gera o homeserver.yaml do Synapse e ajusta para Postgres + appservice
docker compose run --rm --no-deps -e SYNAPSE_SERVER_NAME=$(grep ^MATRIX_DOMAIN .env | cut -d= -f2) \
    -e SYNAPSE_REPORT_STATS=no synapse generate
docker compose run --rm --no-deps -v ./data/synapse:/synapse bridge \
    python /app/deploy/patch_homeserver.py /synapse/homeserver.yaml

docker compose up -d
docker compose exec synapse register_new_matrix_user -c /data/homeserver.yaml -u allan --admin http://localhost:8008
docker compose logs -f bridge
```

Depois, entre em <https://app.element.io> com o servidor `https://SEU_DOMINIO` e o usuário criado.

## Desenvolvimento

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Para o teste de integração ponta a ponta (Synapse real + bridge + uma Graph API falsa que devolve webhooks assinados):

```bash
pip install matrix-synapse
bash scripts/e2e/run_e2e.sh
```

## Limitações conhecidas

- Mensagens iniciadas pela organização exigem template aprovado pela Meta e consentimento prévio do destinatário.
- Contas sem verificação empresarial têm limite diário de destinatários e podem sofrer restrições para outros países (erro 130497). Use um número de remetente do mesmo país dos destinatários.
- Respostas dos usuários não são repassadas às salas; apenas SAIR/PARAR/STOP é tratado.

## Licença

MIT
