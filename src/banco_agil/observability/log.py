"""Log técnico do sistema, com mascaramento de dados pessoais.

O cliente nunca vê stack trace: a falha vira uma mensagem gentil na conversa e
uma linha técnica aqui. CPF e outros documentos são mascarados antes de gravar
(LGPD: o log não é lugar de dado pessoal completo).
"""

from __future__ import annotations

import logging
import re
import traceback
from pathlib import Path

from banco_agil.errors import BancoAgilError

__all__ = ["mascarar_dados_pessoais", "obter_logger", "configurar_logging", "registrar_falha"]

_NOME_LOGGER = "banco_agil"
_LOGGER = logging.getLogger(_NOME_LOGGER)
_configurado = False

# CPF com ou sem máscara: 11 dígitos, eventualmente pontuados. As bordas olham
# só para dígito: com `\b` um CPF colado a uma letra (`user52998224725`) passaria
# em claro, e 12+ dígitos seguidos continuam fora do alcance do padrão.
_PADRAO_CPF = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
_PADRAO_CHAVE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|senha|authorization)\b\s*[:=]\s*\S+"
)


def mascarar_dados_pessoais(texto: str) -> str:
    """Mascara CPF e pares chave=valor sensíveis em uma linha de log."""
    texto = _PADRAO_CPF.sub(lambda casado: f"***{casado.group()[-2:]}", texto)
    return _PADRAO_CHAVE.sub(r"\1=***", texto)


def configurar_logging(caminho_arquivo: Path | None = None, *, nivel: str = "INFO") -> None:
    """Configura o logger do sistema (uma vez por processo)."""
    global _configurado
    _LOGGER.setLevel(getattr(logging, nivel.upper(), logging.INFO))
    if _configurado:
        return

    formato = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")

    if caminho_arquivo is not None:
        caminho_arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo = logging.FileHandler(caminho_arquivo, encoding="utf-8")
        arquivo.setFormatter(formato)
        _LOGGER.addHandler(arquivo)

    _configurado = True


def obter_logger() -> logging.Logger:
    """Logger do sistema (sem handler de arquivo até ``configurar_logging``)."""
    return _LOGGER


def registrar_falha(origem: str, erro: BaseException) -> None:
    """Grava a falha em nível técnico, já mascarada."""
    detalhe = mascarar_dados_pessoais(f"{type(erro).__name__}: {erro}")
    _LOGGER.warning("%s | %s", origem, detalhe)
    if not isinstance(erro, BancoAgilError):
        # O rastro é formatado e mascarado aqui: `exc_info=erro` entregaria o
        # texto cru ao handler e o CPF iria inteiro para o arquivo de log.
        rastro = mascarar_dados_pessoais(
            "".join(traceback.format_exception(type(erro), erro, erro.__traceback__))
        )
        _LOGGER.debug("%s | traceback de erro inesperado\n%s", origem, rastro)
