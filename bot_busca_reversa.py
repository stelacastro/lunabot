"""
=======================================================================
 BOT DE BUSCA REVERSA DE IMAGENS PARA TELEGRAM (via Google Lens)
=======================================================================

Funcionamento:
1. O usuário envia uma foto no chat do Telegram.
2. O bot baixa a foto temporariamente no servidor.
3. A imagem é enviada para o ImgBB (host de imagens gratuito, com
   auto-expiração) para gerar um link público temporário e seguro
   — necessário porque o Google Lens exige uma URL pública, e o
   link de arquivo do próprio Telegram contém o TOKEN do bot,
   o que seria inseguro repassar a terceiros.
4. Esse link público é enviado ao SerpApi (engine=google_lens), que
   faz a busca reversa real no Google Lens e devolve os resultados
   visuais mais relevantes.
5. O bot extrai o melhor resultado (exact match, se houver, senão o
   primeiro visual match) e responde ao usuário com o link da fonte.
6. O arquivo local temporário é removido após o processamento; a
   cópia no ImgBB expira automaticamente sozinha.

Bibliotecas necessárias:
    pip install python-telegram-bot==21.6 aiohttp python-dotenv

Serviços utilizados (ambos com plano gratuito):
    - SerpApi (Google Lens):  https://serpapi.com/google-lens-api
      -> 250 buscas grátis/mês
    - ImgBB (hospedagem temporária de imagem): https://api.imgbb.com/
      -> gratuito, sem necessidade de cartão de crédito
=======================================================================
"""

import os
import logging
import tempfile
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # Carrega variáveis do arquivo .env, se ele existir

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -----------------------------------------------------------------------
# CONFIGURAÇÃO / VARIÁVEIS DE AMBIENTE
# -----------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY")

# Tempo (em segundos) até a imagem expirar automaticamente no ImgBB.
# Mínimo permitido pela API do ImgBB: 60 segundos.
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
    """
    Faz upload da imagem para o ImgBB com auto-expiração e retorna
    a URL pública gerada, ou None em caso de falha.
    """
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
async def buscar_imagem_google_lens(url_imagem_publica: str) -> dict | None:
    """
    Envia a URL pública da imagem ao SerpApi (engine=google_lens) e
    retorna o melhor resultado encontrado, ou None se nada relevante
    for encontrado.

    Retorno esperado (dict):
        {
            "titulo": str,
            "url": str,
            "fonte": str | None,
            "tipo_match": "exato" | "visual",
        }
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
                return None
            dados = await resposta.json()

    if dados.get("search_metadata", {}).get("status") != "Success":
        logger.error("Busca no SerpApi não teve sucesso: %s", dados.get("search_metadata"))
        return None

    # Prioridade 1: correspondências exatas (mais confiáveis)
    correspondencias_exatas = dados.get("exact_matches", [])
    if correspondencias_exatas:
        melhor = correspondencias_exatas[0]
        return {
            "titulo": melhor.get("title", "Fonte encontrada"),
            "url": melhor.get("link"),
            "fonte": melhor.get("source"),
            "tipo_match": "exato",
        }

    # Prioridade 2: correspondências visuais (similares, não exatas)
    correspondencias_visuais = dados.get("visual_matches", [])
    if correspondencias_visuais:
        melhor = correspondencias_visuais[0]
        return {
            "titulo": melhor.get("title", "Fonte encontrada"),
            "url": melhor.get("link"),
            "fonte": melhor.get("source"),
            "tipo_match": "visual",
        }

    return None


# -----------------------------------------------------------------------
# HANDLER: /start
# -----------------------------------------------------------------------
async def comando_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Oi, Luna! 👋 Me envia uma foto que eu vou tentar procurar ela "
        "nos subúrbios da internet."
    )


# -----------------------------------------------------------------------
# HANDLER PRINCIPAL: recebe a foto e processa a busca reversa
# -----------------------------------------------------------------------
async def processar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mensagem = update.message
    caminho_temp: Path | None = None

    try:
        # Validação de configuração
        if not SERPAPI_API_KEY or not IMGBB_API_KEY:
            await mensagem.reply_text(
                "⚠️ O bot não está configurado corretamente "
                "(SERPAPI_API_KEY ou IMGBB_API_KEY ausente). Avise ao seu namorado Beni."
            )
            return

        aviso = await mensagem.reply_text("🔎 Buscando a origem da imagem, aguarde...")

        # Pega a foto em maior resolução enviada pelo usuário
        foto = mensagem.photo[-1]
        arquivo = await foto.get_file()

        with tempfile.TemporaryDirectory() as pasta_temp:
            caminho_temp = Path(pasta_temp) / f"{foto.file_unique_id}.jpg"
            await arquivo.download_to_drive(custom_path=caminho_temp)

            # 1) Sobe a imagem para obter uma URL pública temporária e segura
            url_publica = await hospedar_imagem_temporariamente(caminho_temp)

            if url_publica is None:
                await aviso.edit_text(
                    "🚫 Não consegui preparar a imagem pra busca! :( Tenta de novo?"
                )
                return

            # 2) Consulta o Google Lens com essa URL
            resultado = await buscar_imagem_google_lens(url_publica)

        # Ao sair do 'with', o arquivo local já foi apagado automaticamente.
        _remover_arquivo_temporario(caminho_temp)

        if resultado is None or not resultado.get("url"):
            await aviso.edit_text(
                "❌ Não consegui encontrar a origem dessa imagem! :("
                "Tenta enviar uma imagem com melhor qualidade ou outro ângulo da mesma cena."
            )
            return

        rotulo_confianca = (
            "✅ *BEEP, BEEP! Correspondência exata encontrada saindo quentinha para Luna!*"
            if resultado["tipo_match"] == "exato"
            else "🔎 *BEEP, BEEP! Resultado mais similar encontrado saindo quentinho para Luna:*"
        )
        titulo = _escapar_markdown(resultado["titulo"])
        fonte = f"\n*Fonte:* {_escapar_markdown(resultado['fonte'])}" if resultado.get("fonte") else ""

        texto_resposta = (
            f"{rotulo_confianca}\n\n"
            f"*Título:* {titulo}{fonte}\n"
            f"*Link:* {resultado['url']}"
        )

        await aviso.edit_text(texto_resposta, parse_mode=ParseMode.MARKDOWN)

    except aiohttp.ClientError:
        logger.exception("Erro de rede ao consultar os serviços externos.")
        await mensagem.reply_text(
            "🚫 Não foi possível me conectar ao serviço de busca reversa "
            "no momento. Tente novamente em instantes."
        )
    except Exception:
        logger.exception("Erro inesperado ao processar a foto.")
        await mensagem.reply_text(
            "🚫 Ocorreu um erro inesperado ao processar sua imagem. "
            "Tente novamente mais tarde."
        )
    finally:
        _remover_arquivo_temporario(caminho_temp)


def _remover_arquivo_temporario(caminho: Path | None) -> None:
    """Remove o arquivo temporário local do disco, se ele ainda existir."""
    if caminho and caminho.exists():
        try:
            caminho.unlink()
        except OSError:
            logger.warning("Não foi possível remover o arquivo temporário: %s", caminho)


def _escapar_markdown(texto: str) -> str:
    """Escapa caracteres especiais do Markdown legado do Telegram."""
    if not texto:
        return texto
    caracteres_especiais = ["_", "*", "`", "["]
    for caractere in caracteres_especiais:
        texto = texto.replace(caractere, f"\\{caractere}")
    return texto


# -----------------------------------------------------------------------
# HANDLER: erros não tratados pelo próprio framework
# -----------------------------------------------------------------------
async def tratador_de_erros(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exceção não tratada: %s", context.error, exc_info=context.error)


# -----------------------------------------------------------------------
# INICIALIZAÇÃO DO BOT
# -----------------------------------------------------------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "A variável de ambiente TELEGRAM_BOT_TOKEN não foi definida."
        )

    aplicacao = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    aplicacao.add_handler(CommandHandler("start", comando_start))
    aplicacao.add_handler(MessageHandler(filters.PHOTO, processar_foto))
    aplicacao.add_error_handler(tratador_de_erros)

    logger.info("Bot iniciado. Aguardando mensagens...")
    aplicacao.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
