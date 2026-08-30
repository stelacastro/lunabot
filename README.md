# 🔎 LunaBot

Bot do Telegram que recebe uma foto e encontra sua origem na internet usando o **Google Lens** (via SerpApi). Responde com o link da fonte, uma miniatura da imagem encontrada, e permite navegar entre múltiplos resultados através de botões interativos.

---

## ✨ Funcionalidades

- 📸 **Busca reversa via Google Lens** — encontra fotos de pessoas, lugares, produtos, artes, memes e mais (não só anime/arte, como serviços tipo SauceNAO).
- 🖼️ **Miniatura do resultado** — mostra a imagem encontrada, não só o link cru.
- 🏷️ **Nome amigável da fonte** — em vez do domínio cru, mostra "Instagram", "Pinterest", "DeviantArt", etc.
- 🔘 **Botões interativos**:
  - 🔄 *Buscar de novo* — refaz a busca do zero
  - 📋 *Ver outros resultados* — lista até 5 fontes possíveis
  - ❌ *Não é isso* — avança para o próximo resultado mais provável
- ⏳ **Feedback de progresso em tempo real** — mensagens de status atualizadas a cada etapa da busca.
- 📋 **Menu de comandos nativo** — atalho `/` ao lado da caixa de texto no Telegram.
- 👋 **Tela de boas-vindas** configurada — descrição visível antes mesmo do usuário apertar "Iniciar".
- 🔐 **Segurança**: nunca expõe o token do bot a serviços terceiros; usa hospedagem temporária com auto-expiração para as imagens.
- ⚡ **Modo Webhook** — mais eficiente que polling, ideal para hospedagem em nuvem.

---

## 🗺️ Como funciona

```
Usuário envia foto
        │
        ▼
Bot baixa a foto do Telegram (temporário)
        │
        ▼
Upload para o ImgBB (base64, auto-expira em 2 min)
        │
        ▼
URL pública enviada ao SerpApi (engine=google_lens)
        │
        ▼
Resultados (exact_matches + visual_matches) processados
        │
        ▼
Bot exibe o melhor resultado: foto + título + fonte + link
        │
        ▼
Arquivo local apagado · cópia no ImgBB expira sozinha
```

**Por que não usar o link de arquivo do próprio Telegram diretamente?**
Porque esse link contém o **token do bot** na URL (`api.telegram.org/file/bot<TOKEN>/...`). Repassar isso a um serviço terceiro (SerpApi) arriscaria vazar o token. Por isso a imagem passa primeiro pelo ImgBB, que gera uma URL pública limpa e temporária.

---

## 📋 Pré-requisitos

| Requisito | Observação |
|---|---|
| Python 3.12 ou 3.13 | Evite 3.14 — incompatibilidade conhecida com `python-telegram-bot` 21.6 (`RuntimeError: no current event loop`) |
| Conta no Telegram | Para criar o bot via [@BotFather](https://t.me/BotFather) |
| Conta no [SerpApi](https://serpapi.com) | Plano grátis: 250 buscas/mês |
| Conta no [ImgBB](https://imgbb.com) | Plano grátis, sem cartão |
| Domínio público HTTPS | Necessário para o webhook (ex: gerado automaticamente pelo Railway) |

---

## ⚙️ Instalação local

```bash
# 1. Clone o repositório e entre na pasta
git clone <seu-repositorio>
cd <seu-repositorio>

# 2. Crie e ative um ambiente virtual
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

# 3. Instale as dependências
pip install -r requirements.txt
```

`requirements.txt`:
```
python-telegram-bot[webhooks]==21.6
aiohttp==3.10.10
python-dotenv==1.0.1
```

---

## 🔑 Configuração das chaves

### 1. Token do bot no Telegram
1. Fale com [@BotFather](https://t.me/BotFather)
2. Use `/newbot` e siga as instruções
3. Copie o token gerado (formato `123456789:ABC-DEF...`)

> ⚠️ **Nunca compartilhe esse token publicamente** (nem em prints de tela). Se vazar, revogue-o imediatamente: `/mybots` → seu bot → **API Token** → **Revoke current token**.

### 2. Chave do SerpApi (Google Lens)
1. Crie conta em https://serpapi.com/users/sign_up
2. Copie a chave em https://serpapi.com/manage-api-key

### 3. Chave do ImgBB
1. Crie conta em https://imgbb.com
2. Gere a chave em https://api.imgbb.com/

### 4. Arquivo `.env` (uso local)

Crie um arquivo `.env` na raiz do projeto:

```env
TELEGRAM_BOT_TOKEN=seu_token_do_botfather
SERPAPI_API_KEY=sua_chave_do_serpapi
IMGBB_API_KEY=sua_chave_do_imgbb
WEBHOOK_URL=https://seu-dominio-publico.com
```

No PowerShell, crie sem BOM (evita falha silenciosa de leitura):
```powershell
Set-Content -Path .env -Encoding ascii -Value @"
TELEGRAM_BOT_TOKEN=seu_token_aqui
SERPAPI_API_KEY=sua_chave_aqui
IMGBB_API_KEY=sua_chave_aqui
WEBHOOK_URL=https://seu-dominio-publico.com
"@
```

> O `.env` nunca deve ser commitado — adicione ao `.gitignore`.

---

## ☁️ Deploy no Railway

O bot roda em **modo webhook**, então precisa de um domínio público HTTPS (o Railway gera um automaticamente).

1. **Suba o código** para um repositório no GitHub e conecte-o ao Railway.
2. **Gere um domínio público**: no serviço → **Settings** → **Networking** → **Generate Domain**.
   - Isso popula a variável `RAILWAY_PUBLIC_DOMAIN` automaticamente, que o bot usa para montar `WEBHOOK_URL` sozinho (não precisa configurar manualmente, a menos que o domínio detectado não seja o correto).
3. **Configure as variáveis de ambiente** em **Variables**:

| Variável | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | seu token |
| `SERPAPI_API_KEY` | sua chave |
| `IMGBB_API_KEY` | sua chave |
| `PYTHON_VERSION` | `3.12.9` |
| `WEBHOOK_URL` *(opcional)* | use apenas se o domínio automático do Railway não bater com o configurado em Networking |
| `WEBHOOK_SECRET_TOKEN` *(opcional, recomendado)* | uma string aleatória, para validar que as requisições realmente vêm do Telegram |

4. O `Procfile` já está configurado para modo **web** (necessário para expor a porta HTTP do webhook):
```
web: python bot_busca_reversa.py
```

5. Confirme nos **Deploy Logs** que aparece:
```
Bot iniciado em modo webhook: https://seu-dominio.up.railway.app/<hash>
```

> Note que o caminho da URL termina em um **hash** (64 caracteres), não no token cru — isso evita expor o token na URL pública, já que ele contém `:` e apareceria de forma insegura no link.

---

## 🖥️ Executando localmente (sem webhook em produção)

Rodar localmente ainda usa o modo webhook do código — então você precisa de uma URL pública mesmo em teste local (ex: via [ngrok](https://ngrok.com) apontando pra sua porta local), ou adaptar temporariamente para `run_polling()` durante o desenvolvimento.

```bash
python bot_busca_reversa.py
```

Se tudo estiver certo:
```
INFO - Bot iniciado em modo webhook: https://.../...
```

---

## 🤖 Comandos disponíveis

| Comando | Descrição |
|---|---|
| `/start` | Mensagem de boas-vindas |
| `/help` | Instruções de uso, dicas e explicação dos botões |

---

## 🛠️ Solução de problemas

| Sintoma | Causa provável | Solução |
|---|---|---|
| `Microsoft Visual C++ 14.0 or greater is required` | Falta compilador C++ para o `aiohttp` | Instale "Desenvolvimento para desktop com C++" (Visual Studio Build Tools) ou atualize o `pip` antes de instalar |
| `RuntimeError: There is no current event loop` | Python 3.14 incompatível com PTB 21.6 | Use Python 3.12 ou 3.13 |
| `RuntimeError: TELEGRAM_BOT_TOKEN não foi definida` | `.env` ausente/mal formatado ou `python-dotenv` não instalado | Confira `pip show python-dotenv`, `Test-Path .env` e `Get-Content .env` |
| `telegram.error.Conflict: terminated by other getUpdates request` | Duas instâncias rodando ao mesmo tempo (ex: local + nuvem) | Pare o processo local: `Get-Process python \| Stop-Process -Force` |
| `Wrong response from the webhook: 404 Not Found` | URL do webhook não bate com o domínio realmente exposto | Confirme que `WEBHOOK_URL`/`RAILWAY_PUBLIC_DOMAIN` é **exatamente** o domínio ativo em Networking |
| Deploy falha com `Railpack ... start FastAPI/Flask/Django` | Plataforma não reconheceu o tipo de app | Garanta que o `Procfile` existe com `web: python bot_busca_reversa.py` |
| `GitHub Repo not found` no Railway | Integração com GitHub perdeu permissão | Reconecte em **Settings → Source**, ou reautorize o app em github.com/settings/installations |
| `ImgBB retornou status HTTP 400` / `Empty upload source` | Upload multipart binário rejeitado pelo servidor | Já corrigido: o bot envia a imagem como **base64**, formato mais compatível |
| Resultados de baixa qualidade/errados | Imagem de baixa resolução ou cena muito genérica | Envie uma foto mais nítida ou com elementos mais distintivos |
| `⚠️ Essa busca expirou` ao clicar em um botão | O bot reiniciou (novo deploy) enquanto a busca estava em memória | Envie a foto novamente — o estado da busca não é persistente entre reinicializações |

---

## 📁 Estrutura do projeto

```
.
├── bot_busca_reversa.py   # Código principal do bot
├── requirements.txt       # Dependências Python
├── Procfile                # Comando de start para o Railway (modo web)
├── runtime.txt             # Versão fixa do Python
├── .env                    # Variáveis de ambiente (não versionar)
└── README.md
```

---

## 📊 Limites dos planos gratuitos

- **SerpApi**: 250 buscas/mês.
- **ImgBB**: sem limite de uploads divulgado publicamente, sujeito a política de uso justo.
- **Railway**: créditos mensais gratuitos.
---

## 🔒 Notas de segurança

- O token do bot **nunca** é usado como parte de uma URL pública (nem no caminho do webhook, nem em serviços terceiros) — é sempre tratado como segredo.
- As imagens enviadas pelos usuários **não ficam armazenadas permanentemente**: o arquivo local é apagado logo após o processamento, e a cópia hospedada no ImgBB expira automaticamente em ~2 minutos.
- Recomenda-se configurar `WEBHOOK_SECRET_TOKEN` para que o servidor rejeite requisições que não venham realmente do Telegram.
