"""
=======================================================================
 BOT DE BUSCA REVERSA DE IMAGENS PARA TELEGRAM (via Google Lens)
=======================================================================

Funcionamento:
1. O usuário envia uma foto no chat do Telegram.
2. O bot baixa a foto temporariamente no servidor.
3. A imagem é enviada ao ImgBB (host de imagens gratuito, com
   auto-expiração) para gerar um link público temporário e seguro
   — necessário porque o Google Lens exige uma URL pública, e o
   link de arquivo do próprio Telegram contém o TOKEN do bot,
   o que seria inseguro repassar a terceiros.
4. Esse link público é enviado ao SerpApi (engine=google_lens), que
   faz a busca reversa real no Google Lens e devolve os resultados
   visuais mais relevantes.
5. O bot mostra o melhor resultado com botões interativos:
   🔄 Buscar de novo | 📋 Ver outros resultados | ❌ Não é isso
6. O arquivo local temporário é removido após o processamento; a
   cópia no ImgBB expira automaticamente sozinha.
7. O bot roda em modo WEBHOOK (não polling) — mais eficiente para
   hospedagem em nuvem (ex: Railway).

Bibliotecas necessárias:
    pip install "python-telegram-bot[webhooks]==21.6" aiohttp python-dotenv

Serviços utilizados (ambos com plano gratuito):
    - SerpApi (Google Lens):  https://serpapi.com/google-lens-api
    - ImgBB (hospedagem temporária de imagem): https://api.imgbb.com/
=======================================================================
"""

import os
import logging
import tempfile
import hashlib
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()  # Carrega variáveis do arquivo .env, se ele existir

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -----------------------------------------------------------------------
# CONFIGURAÇÃO / VARIÁVEIS DE AMBIENTE
# -----------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY")

# --- Configuração do WEBHOOK ---
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    dominio_railway = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if dominio_railway:
        WEBHOOK_URL = f"https://{dominio_railway}"

PORT = int(os.environ.get("PORT", 8443))

# Caminho da rota do webhook: hash do token (URL-safe, não expõe o
# token cru — que contém ":" e pode quebrar roteamento em alguns proxies).
CAMINHO_WEBHOOK = (
    hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).hexdigest()
    if TELEGRAM_BOT_TOKEN
    else "webhook"
)

WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_SECRET_TOKEN")

# Quantos resultados no máximo mostrar como alternativas
MAX_RESULTADOS = 5
# Tempo de expiração da imagem no ImgBB (segundos)
IMGBB_EXPIRATION_SECONDS = 120

IMGBB_ENDPOINT = "https://api.imgbb.com/1/upload"
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

# -----------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# FUNÇÃO AUXILIAR 1: sobe a imagem para o ImgBB e retorna a URL pública
# -----------------------------------------------------------------------
async def hospedar_imagem_temporariamente(caminho_imagem: Path) -> str | None:
    with open(caminho_imagem, "rb") as arquivo_imagem:
        conteudo = arquivo_imagem.read()

    form = aiohttp.FormData()
    form.add_field("key", IMGBB_API_KEY)
    form.add_field("expiration", str(IMGBB_EXPIRATION_SECONDS))
    form.add_field(
        "image",
        conteudo,
        filename=caminho_imagem.name,
        content_type="application/octet-stream",
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(IMGBB_ENDPOINT, data=form, timeout=30) as resposta:
            if resposta.status != 200:
                logger.error("ImgBB retornou status HTTP %s", resposta.status)
                return None
            dados = await resposta.json()

    if not dados.get("success"):
        logger.error("Falha ao subir imagem no ImgBB: %s", dados)
        return None

    return dados["data"]["url"]


# -----------------------------------------------------------------------
# FUNÇÃO AUXILIAR 2: consulta o Google Lens via SerpApi
# -----------------------------------------------------------------------
async def buscar_imagem_google_lens(url_imagem_publica: str) -> list[dict]:
    """
    Consulta o SerpApi (engine=google_lens) e retorna uma LISTA com até
    MAX_RESULTADOS resultados (correspondências exatas primeiro, depois
    correspondências visuais), cada um no formato:
        {
            "titulo": str,
            "url": str,
            "fonte": str | None,
            "tipo_match": "exato" | "visual",
        }
    Retorna lista vazia se nada for encontrado ou a API falhar.
    """
    params = {
        "engine": "google_lens",
        "url": url_imagem_publica,
        "api_key": SERPAPI_API_KEY,
        "hl": "pt-br",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(SERPAPI_ENDPOINT, params=params, timeout=30) as resposta:
            if resposta.status != 200:
                logger.error("SerpApi retornou status HTTP %s", resposta.status)
                return []
            dados = await resposta.json()

    if dados.get("search_metadata", {}).get("status") != "Success":
        logger.error("Busca no SerpApi não teve sucesso: %s", dados.get("search_metadata"))
        return []

    resultados: list[dict] = []
    urls_ja_vistas: set[str] = set()

    def _adicionar(item: dict, tipo: str) -> None:
        link = item.get("link")
        if not link or link in urls_ja_vistas:
            return
        urls_ja_vistas.add(link)
        resultados.append({
            "titulo": item.get("title", "Fonte encontrada"),
            "url": link,
            "fonte": item.get("source"),
            "thumbnail": item.get("thumbnail"),
            "tipo_match": tipo,
        })

    for item in dados.get("exact_matches", []):
        _adicionar(item, "exato")
    for item in dados.get("visual_matches", []):
        _adicionar(item, "visual")

    return resultados[:MAX_RESULTADOS]


# -----------------------------------------------------------------------
# FORMATAÇÃO DE MENSAGENS E TECLADOS
# -----------------------------------------------------------------------
def _escapar_markdown(texto: str | None) -> str:
    if not texto:
        return texto or ""
    caracteres_especiais = ["_", "*", "`", "["]
    for caractere in caracteres_especiais:
        texto = texto.replace(caractere, f"\\{caractere}")
    return texto


# Mapeamento de trechos de domínio -> nome amigável exibido ao usuário.
# A checagem é por "contém", então cobre subdomínios (ex: br.pinterest.com,
# m.facebook.com, www.instagram.com etc.) sem precisar listar cada variação.
DOMINIOS_AMIGAVEIS = {
    "instagram.com": "Instagram",
    "pinterest.": "Pinterest",
    "deviantart.com": "DeviantArt",
    "twitter.com": "Twitter/X",
    "x.com": "Twitter/X",
    "facebook.com": "Facebook",
    "reddit.com": "Reddit",
    "tumblr.com": "Tumblr",
    "flickr.com": "Flickr",
    "wikipedia.org": "Wikipédia",
    "youtube.com": "YouTube",
    "tiktok.com": "TikTok",
    "amazon.": "Amazon",
    "etsy.com": "Etsy",
    "shutterstock.com": "Shutterstock",
    "gettyimages.com": "Getty Images",
    "artstation.com": "ArtStation",
    "behance.net": "Behance",
    "imgur.com": "Imgur",
    "linkedin.com": "LinkedIn",
    "ebay.com": "eBay",
    "aliexpress.com": "AliExpress",
    "weheartit.com": "We Heart It",
}


def nome_amigavel_da_fonte(url: str, fonte_original: str | None) -> str:
    """
    Converte o domínio de uma URL em um nome de marca reconhecível
    (ex: 'www.instagram.com' -> 'Instagram'). Se o domínio não estiver
    mapeado, usa o campo 'source' que o próprio SerpApi já devolve, ou
    por último, capitaliza o nome do domínio como fallback.
    """
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        netloc = ""
    netloc_sem_www = netloc.removeprefix("www.")

    for trecho, nome_amigavel in DOMINIOS_AMIGAVEIS.items():
        if trecho in netloc_sem_www:
            return nome_amigavel

    if fonte_original:
        return fonte_original

    partes = netloc_sem_www.split(".")
    if partes and partes[0]:
        return partes[0].capitalize()
    return "Fonte desconhecida"


def formatar_resultado(resultado: dict, indice: int, total: int) -> str:
    """Monta o texto (Markdown) de um resultado específico."""
    rotulo_confianca = (
        "✅ *Correspondência exata encontrada!*"
        if resultado["tipo_match"] == "exato"
        else "🔎 *Resultado visual encontrado:*"
    )
    titulo = _escapar_markdown(resultado["titulo"])
    nome_fonte = nome_amigavel_da_fonte(resultado["url"], resultado.get("fonte"))
    fonte = f"\n*Fonte:* {_escapar_markdown(nome_fonte)}"
    posicao = f"\n_Resultado {indice + 1} de {total}_" if total > 1 else ""

    return (
        f"{rotulo_confianca}\n\n"
        f"*Título:* {titulo}{fonte}\n"
        f"*Link:* {resultado['url']}"
        f"{posicao}"
    )


def montar_teclado_resultado(indice: int, total: int) -> InlineKeyboardMarkup:
    """Teclado mostrado junto de um resultado individual."""
    linhas = []

    linha_navegacao = []
    if total > 1:
        linha_navegacao.append(
            InlineKeyboardButton("❌ Não é isso", callback_data="proximo")
        )
    linhas.append(linha_navegacao) if linha_navegacao else None

    linha_acoes = [InlineKeyboardButton("🔄 Buscar de novo", callback_data="novamente")]
    if total > 1:
        linha_acoes.append(
            InlineKeyboardButton("📋 Ver outros resultados", callback_data="listar")
        )
    linhas.append(linha_acoes)

    return InlineKeyboardMarkup(linhas)


def montar_teclado_lista(resultados: list[dict]) -> InlineKeyboardMarkup:
    """Teclado com um botão por resultado, para o usuário escolher qual ver."""
    linhas = []
    for i, resultado in enumerate(resultados):
        rotulo = resultado["titulo"][:40]
        emoji = "✅" if resultado["tipo_match"] == "exato" else "🔎"
        linhas.append([
            InlineKeyboardButton(f"{emoji} {rotulo}", callback_data=f"escolher:{i}")
        ])
    linhas.append([InlineKeyboardButton("🔄 Buscar de novo", callback_data="novamente")])
    return InlineKeyboardMarkup(linhas)


# -----------------------------------------------------------------------
# HELPERS DE EXIBIÇÃO "FOTO-AWARE"
# -----------------------------------------------------------------------
# O Telegram não permite editar uma mensagem de TEXTO para virar uma
# mensagem de FOTO (ou vice-versa) via edit_text/edit_message_media —
# são tipos de mensagem diferentes. Por isso, estas funções detectam o
# tipo da mensagem atual e decidem entre editar no lugar (mesmo tipo)
# ou apagar + enviar uma nova (mudança de tipo), sempre retornando a
# mensagem final para que o próximo clique de botão continue funcionando.

async def exibir_resultado(context, mensagem_atual, resultado: dict, indice: int, total: int):
    """Mostra um resultado, com miniatura (foto) quando disponível."""
    texto = formatar_resultado(resultado, indice, total)
    teclado = montar_teclado_resultado(indice, total)
    thumbnail = resultado.get("thumbnail")
    chat_id = mensagem_atual.chat_id

    if thumbnail:
        # Precisamos de uma mensagem de FOTO. Se a mensagem atual já for
        # uma foto, tentamos trocar só a mídia (mais leve); se falhar ou
        # se a mensagem atual for de texto, apagamos e enviamos uma nova.
        if mensagem_atual.photo:
            try:
                return await context.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=mensagem_atual.message_id,
                    media=InputMediaPhoto(
                        media=thumbnail, caption=texto, parse_mode=ParseMode.MARKDOWN
                    ),
                    reply_markup=teclado,
                )
            except Exception:
                logger.warning("Falha ao trocar mídia da mensagem; enviando nova.")

        try:
            await mensagem_atual.delete()
        except Exception:
            pass
        return await context.bot.send_photo(
            chat_id=chat_id,
            photo=thumbnail,
            caption=texto,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=teclado,
        )

    # Sem miniatura disponível: exibimos como mensagem de texto normal.
    return await exibir_texto(context, mensagem_atual, texto, teclado)


async def exibir_texto(context, mensagem_atual, texto: str, teclado: InlineKeyboardMarkup | None = None):
    """Mostra uma mensagem de texto simples, migrando de foto pra texto se necessário."""
    chat_id = mensagem_atual.chat_id

    if mensagem_atual.photo:
        try:
            await mensagem_atual.delete()
        except Exception:
            pass
        return await context.bot.send_message(
            chat_id=chat_id, text=texto, parse_mode=ParseMode.MARKDOWN, reply_markup=teclado
        )

    return await mensagem_atual.edit_text(texto, parse_mode=ParseMode.MARKDOWN, reply_markup=teclado)


# -----------------------------------------------------------------------
# PIPELINE DE BUSCA (reutilizado pelo envio de foto E pelo botão "buscar de novo")
# -----------------------------------------------------------------------
async def executar_pipeline_busca(
    file_id: str,
    context: ContextTypes.DEFAULT_TYPE,
    mensagem_status,
) -> None:
    """
    Baixa a imagem, hospeda, consulta o Google Lens e edita
    'mensagem_status' progressivamente até mostrar o resultado final.
    Guarda os resultados em context.user_data para uso pelos botões.
    """
    caminho_temp: Path | None = None
    try:
        if not SERPAPI_API_KEY or not IMGBB_API_KEY:
            await mensagem_status.edit_text(
                "⚠️ O bot não está configurado corretamente "
                "(SERPAPI_API_KEY ou IMGBB_API_KEY ausente). Avise o administrador."
            )
            return

        arquivo = await context.bot.get_file(file_id)

        with tempfile.TemporaryDirectory() as pasta_temp:
            caminho_temp = Path(pasta_temp) / f"{file_id}.jpg"
            await arquivo.download_to_drive(custom_path=caminho_temp)

            await mensagem_status.edit_text("📤 Enviando imagem...")
            url_publica = await hospedar_imagem_temporariamente(caminho_temp)

            if url_publica is None:
                await mensagem_status.edit_text(
                    "🚫 Não consegui preparar a imagem para a busca. Tente novamente."
                )
                return

            await mensagem_status.edit_text("🔍 Consultando o Google Lens...")
            resultados = await buscar_imagem_google_lens(url_publica)

        _remover_arquivo_temporario(caminho_temp)

        if not resultados:
            await mensagem_status.edit_text(
                "❌ Não consegui encontrar a origem dessa imagem. "
                "Tente enviar uma imagem com melhor qualidade ou outro ângulo da mesma cena.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔄 Buscar de novo", callback_data="novamente")]]
                ),
            )
            return

        # Guarda o estado da busca para os botões interativos usarem depois
        context.user_data["ultima_busca"] = {
            "file_id": file_id,
            "resultados": resultados,
            "indice_atual": 0,
        }

        await exibir_resultado(context, mensagem_status, resultados[0], 0, len(resultados))

    except aiohttp.ClientError:
        logger.exception("Erro de rede ao consultar os serviços externos.")
        await mensagem_status.edit_text(
            "🚫 Não foi possível me conectar ao serviço de busca reversa "
            "no momento. Tente novamente em instantes."
        )
    except Exception:
        logger.exception("Erro inesperado ao processar a foto.")
        await mensagem_status.edit_text(
            "🚫 Ocorreu um erro inesperado ao processar sua imagem. "
            "Tente novamente mais tarde."
        )
    finally:
        _remover_arquivo_temporario(caminho_temp)


def _remover_arquivo_temporario(caminho: Path | None) -> None:
    if caminho and caminho.exists():
        try:
            caminho.unlink()
        except OSError:
            logger.warning("Não foi possível remover o arquivo temporário: %s", caminho)


# -----------------------------------------------------------------------
# HANDLER: /start
# -----------------------------------------------------------------------
async def comando_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Olá! 👋 Envie-me uma foto/imagem e eu vou tentar encontrar "
        "a origem dela na internet através do Google Lens.\n\n"
        "Use /help para ver exemplos de uso."
    )


# -----------------------------------------------------------------------
# HANDLER: /help
# -----------------------------------------------------------------------
async def comando_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto_ajuda = (
        "🤖 *Como usar este bot*\n\n"
        "1️⃣ Envie uma *foto* diretamente no chat (não como arquivo/documento).\n"
        "2️⃣ Aguarde alguns segundos enquanto eu busco a origem dela no Google Lens.\n"
        "3️⃣ Eu te mostro o resultado mais provável, com botões para:\n"
        "   • 🔄 *Buscar de novo* — refaz a busca do zero\n"
        "   • 📋 *Ver outros resultados* — mostra até 5 fontes possíveis\n"
        "   • ❌ *Não é isso* — pula para o próximo resultado mais provável\n\n"
        "*Dicas para melhores resultados:*\n"
        "• Envie fotos nítidas e com boa resolução\n"
        "• Funciona melhor com imagens públicas na internet (fotos de stock, "
        "posts de redes sociais, artes, produtos, etc.)\n"
        "• Fotos muito genéricas ou privadas podem não ter correspondência\n\n"
        "*Comandos disponíveis:*\n"
        "/start — mensagem de boas-vindas\n"
        "/help — esta mensagem"
    )
    await update.message.reply_text(texto_ajuda, parse_mode=ParseMode.MARKDOWN)


# -----------------------------------------------------------------------
# HANDLER PRINCIPAL: recebe a foto e inicia o pipeline de busca
# -----------------------------------------------------------------------
async def processar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mensagem = update.message
    foto = mensagem.photo[-1]

    mensagem_status = await mensagem.reply_text("🔎 Iniciando busca...")
    await executar_pipeline_busca(foto.file_id, context, mensagem_status)


# -----------------------------------------------------------------------
# HANDLER: cliques nos botões inline
# -----------------------------------------------------------------------
async def tratar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # remove o "relógio de carregando" do botão

    estado = context.user_data.get("ultima_busca")
    if not estado:
        await exibir_texto(
            context, query.message,
            "⚠️ Essa busca expirou. Envie a foto novamente para buscar de novo."
        )
        return

    acao = query.data

    if acao == "novamente":
        mensagem_status = await exibir_texto(context, query.message, "🔎 Iniciando nova busca...")
        await executar_pipeline_busca(estado["file_id"], context, mensagem_status)
        return

    if acao == "listar":
        resultados = estado["resultados"]
        teclado = montar_teclado_lista(resultados)
        await exibir_texto(
            context, query.message,
            "📋 *Resultados encontrados:*\nEscolha um para ver os detalhes.",
            teclado,
        )
        return

    if acao == "proximo":
        resultados = estado["resultados"]
        indice_atual = estado["indice_atual"]
        proximo_indice = indice_atual + 1

        if proximo_indice >= len(resultados):
            await exibir_texto(
                context, query.message,
                "🤷 Não há mais resultados alternativos para essa imagem.",
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔄 Buscar de novo", callback_data="novamente")]]
                ),
            )
            return

        estado["indice_atual"] = proximo_indice
        await exibir_resultado(
            context, query.message, resultados[proximo_indice], proximo_indice, len(resultados)
        )
        return

    if acao.startswith("escolher:"):
        indice_escolhido = int(acao.split(":", 1)[1])
        resultados = estado["resultados"]

        if indice_escolhido >= len(resultados):
            await exibir_texto(context, query.message, "⚠️ Resultado inválido.")
            return

        estado["indice_atual"] = indice_escolhido
        await exibir_resultado(
            context, query.message, resultados[indice_escolhido], indice_escolhido, len(resultados)
        )
        return

    logger.warning("Callback desconhecido recebido: %s", acao)


# -----------------------------------------------------------------------
# HANDLER: erros não tratados pelo próprio framework
# -----------------------------------------------------------------------
async def tratador_de_erros(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exceção não tratada: %s", context.error, exc_info=context.error)


# -----------------------------------------------------------------------
# CONFIGURAÇÃO DA DESCRIÇÃO (aparece ANTES do usuário apertar "Iniciar")
# -----------------------------------------------------------------------
async def configurar_descricao_bot(aplicacao: Application) -> None:
    """
    Define a mensagem exibida na tela inicial do bot, ANTES do usuário
    apertar 'Iniciar' pela primeira vez (chat ainda vazio) — é o campo
    'description' da API do Telegram. Também define a 'short_description',
    que aparece no perfil do bot e quando ele é compartilhado/encaminhado.
    """
    await aplicacao.bot.set_my_description(
        description=(
            "🔎 Encontre a origem de qualquer imagem!\n\n"
            "Envie uma foto e eu busco no Google Lens onde ela foi "
            "publicada originalmente — perfil, post ou site.\n\n"
            "Toque em Iniciar e me mande uma foto para começar."
        )
    )
    await aplicacao.bot.set_my_short_description(
        short_description="Envie uma foto e eu encontro a origem dela na internet via Google Lens."
    )
    logger.info("Descrição do bot configurada com sucesso.")


# -----------------------------------------------------------------------
# CONFIGURAÇÃO DO MENU DE COMANDOS (aparece ao lado da caixa de texto)
# -----------------------------------------------------------------------
async def configurar_menu_comandos(aplicacao: Application) -> None:
    """
    Registra a lista de comandos do bot junto ao Telegram. Isso faz
    aparecer um atalho (ícone de menu "/") ao lado da caixa de mensagem
    no app do Telegram, com cada comando e sua descrição — facilita
    muito a descoberta de funcionalidades pelo usuário, sem precisar
    digitar tudo manualmente.

    Executado automaticamente uma vez, na inicialização do bot
    (via post_init do Application).
    """
    await aplicacao.bot.set_my_commands([
        ("start", "Iniciar o bot e ver a mensagem de boas-vindas"),
        ("help", "Ver instruções de uso e dicas"),
    ])
    logger.info("Menu de comandos configurado com sucesso.")


async def configurar_bot_no_inicio(aplicacao: Application) -> None:
    """Roda todas as configurações de perfil/menu do bot ao iniciar."""
    await configurar_descricao_bot(aplicacao)
    await configurar_menu_comandos(aplicacao)


# -----------------------------------------------------------------------
# INICIALIZAÇÃO DO BOT
# -----------------------------------------------------------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "A variável de ambiente TELEGRAM_BOT_TOKEN não foi definida."
        )
    if not WEBHOOK_URL:
        raise RuntimeError(
            "Nenhuma URL pública encontrada para o webhook. Defina a variável "
            "de ambiente WEBHOOK_URL manualmente (ex: https://seu-app.up.railway.app), "
            "ou garanta que o serviço tenha um domínio público gerado na plataforma."
        )

    aplicacao = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(configurar_bot_no_inicio)
        .build()
    )

    aplicacao.add_handler(CommandHandler("start", comando_start))
    aplicacao.add_handler(CommandHandler("help", comando_help))
    aplicacao.add_handler(MessageHandler(filters.PHOTO, processar_foto))
    aplicacao.add_handler(CallbackQueryHandler(tratar_callback))
    aplicacao.add_error_handler(tratador_de_erros)

    url_completa_webhook = f"{WEBHOOK_URL.rstrip('/')}/{CAMINHO_WEBHOOK}"
    logger.info("Bot iniciado em modo webhook: %s", url_completa_webhook)

    aplicacao.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=CAMINHO_WEBHOOK,
        webhook_url=url_completa_webhook,
        secret_token=WEBHOOK_SECRET_TOKEN or None,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()