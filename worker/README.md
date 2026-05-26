# BuyGoods Postback Receiver

Cloudflare Worker que recebe webhooks da BuyGoods a cada venda e expõe o saldo pendente.

**Por que Cloudflare Worker?** Endpoint HTTPS público (BuyGoods precisa), free tier generoso (100k req/dia), KV pra persistência (1k writes/dia free), zero servidor pra manter.

---

## Caminho rápido: deploy pelo dashboard web (sem CLI)

### 1. Conta Cloudflare
- https://dash.cloudflare.com/sign-up — gratuito
- Pode usar mesmo email do GitHub

### 2. Criar KV namespace
- Sidebar → **Workers & Pages** → **KV**
- **Create a namespace** → nome: `buygoods-sales` → Add
- Anota o **Namespace ID** (32 caracteres hex)

### 3. Criar o Worker
- **Workers & Pages** → **Create** → **Create Worker**
- Nome sugerido: `buygoods-postback`
- Quando aparecer o editor "Hello World", clica **Edit code**
- **Apaga tudo** e cola o conteúdo de `worker.js` deste diretório
- **Save and Deploy**

### 4. Bind do KV
- No worker → **Settings** → **Bindings** → **Add binding** → **KV Namespace**
- Variable name: `SALES`
- KV namespace: `buygoods-sales` (o que você criou)
- Save

### 5. Secrets (env vars criptografadas)
Gera 2 tokens aleatórios no terminal:
```bash
openssl rand -hex 32   # postback token
openssl rand -hex 32   # read token
```

No worker → **Settings** → **Variables and Secrets** → **Add variable**:
- Type: **Secret**
- Variable name: `POSTBACK_TOKEN` → cola o primeiro hex → Deploy
- Repete pro `READ_TOKEN`

### 6. Pegar a URL do worker
No topo do dashboard do worker: `https://buygoods-postback.<seu-usuario>.workers.dev`

Testa:
```bash
curl https://buygoods-postback.<seu-usuario>.workers.dev/
# Deve responder JSON: {"service":"buygoods-postback","endpoints":[...]}
```

---

## Configurar postback na BuyGoods

1. Login em https://backoffice.buygoods.com
2. Acessa a página do produto/promoção → **Settings** → **Postback pixels**
3. **Add New** → cola a URL abaixo, **substituindo os placeholders**:

```
https://buygoods-postback.<seu-usuario>.workers.dev/postback?token=POSTBACK_TOKEN_AQUI&commission={COMMISSION_AMOUNT}&order={ORDERID}&subid={SUBID}&product={PRODUCT_ID}&status={STATUS}
```

4. Salva. A partir das próximas vendas, a BuyGoods vai disparar pro worker.

---

## Conectar com o dashboard

Adiciona no `dashboard/secrets.json`:
```json
{
  "lootrush_api_key": "...",
  "wise_cnpj_token": "...",
  "wise_llp_token": "...",
  "buygoods_worker_url": "https://buygoods-postback.<seu-usuario>.workers.dev",
  "buygoods_read_token": "READ_TOKEN_QUE_VOCE_GEROU"
}
```

Reinicia o `python3 dashboard/server.py` e abre Controle de Caixa. Deveria aparecer uma linha verde live "BuyGoods pendente: $X.XX".

---

## Caminho alternativo: deploy via CLI (wrangler)

Se preferir terminal:

```bash
cd worker
npm install -g wrangler
wrangler login                              # abre browser pra OAuth
wrangler kv:namespace create SALES          # cria KV, mostra o id
# Edita wrangler.toml e cola o id retornado
wrangler secret put POSTBACK_TOKEN          # cola o token
wrangler secret put READ_TOKEN              # cola o token
wrangler deploy                             # publica
```

URL retornada: `https://buygoods-postback.<sua-conta>.workers.dev`

---

## Testes manuais

```bash
WORKER="https://buygoods-postback.<seu-usuario>.workers.dev"
PB_TOKEN="<POSTBACK_TOKEN>"
RD_TOKEN="<READ_TOKEN>"

# Simula uma venda
curl "$WORKER/postback?token=$PB_TOKEN&commission=42.50&order=test-001&subid=campaign1&product=prod-abc"

# Vê pendente
curl "$WORKER/pending?token=$RD_TOKEN"

# Marca payout (zera pendente)
curl -X POST "$WORKER/mark-payout?token=$RD_TOKEN"

# Vê pendente de novo (deve estar zerado)
curl "$WORKER/pending?token=$RD_TOKEN"

# Lista últimas 50 vendas (incluindo as antes do payout)
curl "$WORKER/sales?token=$RD_TOKEN&limit=50"
```

---

## Reset automático quando BUYGOODS paga (LootRush deposit)

O `server.py` do dashboard verifica a LootRush a cada chamada de `/buygoods-pending`. Se achar um deposit "BUYGOODS" mais novo que o `last_payout` do worker, chama `/mark-payout?ts=<deposit_ts>` automaticamente, zerando o pendente até o próximo lote.

---

## Limites de segurança

- O `POSTBACK_TOKEN` vai no postback URL que fica armazenado na BuyGoods. Se vazar, alguém pode injetar vendas fake (não afeta dinheiro real, só a métrica de pendente). Pra rotacionar: gere novo, atualiza secret no worker, atualiza URL na BuyGoods.
- O `READ_TOKEN` fica só no seu `secrets.json` local + worker. Se vazar, alguém pode ler quanto você tem pendente e marcar payouts. Rotaciona se suspeitar.
