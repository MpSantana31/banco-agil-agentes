"""Leitura e escrita do `.env` — com confirmação obrigatória antes de gravar.

Regras (higiene de credenciais):

1. **Nada é escrito sem ``confirmado=True``.** O código se recusa e levanta
   ``ConfirmacaoNecessaria`` — sobrescrever `.env` apaga chave de API, então a
   confirmação é explícita e vem da pessoa, nunca de um default.
2. **Backup antes de tocar no arquivo** (``.env.bak-<timestamp>``).
3. **Edição cirúrgica:** só as chaves gerenciadas mudam; comentários e variáveis
   de outros sistemas ficam intactos.
4. **Segredo nunca aparece inteiro** em tela, log ou preview — só mascarado.
5. Arquivo final com permissão ``0600``.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from banco_agil.errors import ConfirmacaoNecessaria, ValidationError

__all__ = [
    "AlteracaoEnv",
    "CHAVES_GERENCIADAS",
    "CHAVES_SECRETAS",
    "ler_env",
    "mascarar_valor",
    "planejar_alteracoes",
    "gravar_env",
]

# Só estas chaves a aba de configuração pode escrever.
CHAVES_GERENCIADAS: tuple[str, ...] = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_FALLBACK_1",
    "LLM_FALLBACK_2",
    "LLM_TEMPERATURE",
    "LLM_BASE_URL",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "OLLAMA_API_KEY",
)

CHAVES_SECRETAS: frozenset[str] = frozenset(chave for chave in CHAVES_GERENCIADAS if "KEY" in chave)

_CABECALHO_NOVAS = "# --- cadeia de modelos (escrita pela aba de configuração) ---"


@dataclass(frozen=True, slots=True)
class AlteracaoEnv:
    """Uma diferença entre o `.env` atual e o que se pretende gravar."""

    chave: str
    valor_atual: str
    valor_novo: str

    @property
    def removendo(self) -> bool:
        return self.valor_novo == ""

    def valor_atual_visivel(self) -> str:
        return mascarar_valor(self.chave, self.valor_atual)

    def valor_novo_visivel(self) -> str:
        return mascarar_valor(self.chave, self.valor_novo)

    def descricao(self) -> str:
        """Texto para revisão humana — **sempre** com segredo mascarado."""
        origem = self.valor_atual_visivel() or "(ausente)"
        destino = "(remover)" if self.removendo else (self.valor_novo_visivel() or "(vazio)")
        return f"{self.chave}: {origem} → {destino}"


def mascarar_valor(chave: str, valor: str) -> str:
    """Mostra um segredo só pelas pontas; valor comum aparece inteiro."""
    if not valor:
        return ""
    if chave not in CHAVES_SECRETAS:
        return valor
    if len(valor) <= 10:
        return "*" * len(valor)
    return f"{valor[:6]}…{valor[-4:]}"


def _sem_aspas(valor: str) -> str:
    if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
        return valor[1:-1]
    return valor


def ler_env(caminho: Path | str) -> dict[str, str]:
    """Lê as variáveis do arquivo (ignora comentários e linhas vazias)."""
    caminho = Path(caminho)
    if not caminho.exists():
        return {}

    valores: dict[str, str] = {}
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        conteudo = linha.strip()
        if not conteudo or conteudo.startswith("#") or "=" not in conteudo:
            continue
        chave, _, valor = conteudo.partition("=")
        valores[chave.strip()] = _sem_aspas(valor.strip())
    return valores


def planejar_alteracoes(
    caminho: Path | str, novos_valores: Mapping[str, str | None]
) -> list[AlteracaoEnv]:
    """Diz exatamente o que mudaria — para revisão antes de confirmar."""
    _validar_chaves(novos_valores)
    atuais = ler_env(caminho)

    alteracoes: list[AlteracaoEnv] = []
    for chave, valor in novos_valores.items():
        novo = "" if valor is None else str(valor).strip()
        antigo = atuais.get(chave, "")
        if novo != antigo:
            alteracoes.append(AlteracaoEnv(chave=chave, valor_atual=antigo, valor_novo=novo))
    return alteracoes


def gravar_env(
    caminho: Path | str,
    novos_valores: Mapping[str, str | None],
    *,
    confirmado: bool,
    quando: dt.datetime | None = None,
) -> Path:
    """Grava as chaves gerenciadas no `.env` e devolve o caminho do backup.

    Levanta ``ConfirmacaoNecessaria`` se ``confirmado`` não for ``True`` — a
    escrita nunca acontece por descuido.
    """
    if not confirmado:
        raise ConfirmacaoNecessaria(
            "Escrita no .env exige confirmação explícita (confirmado=True). Nada foi alterado."
        )
    _validar_chaves(novos_valores)

    caminho = Path(caminho)
    linhas_originais = caminho.read_text(encoding="utf-8").splitlines() if caminho.exists() else []
    backup = _criar_backup(caminho, quando=quando)

    pendentes = {chave: valor for chave, valor in novos_valores.items()}
    linhas_finais: list[str] = []

    for linha in linhas_originais:
        conteudo = linha.strip()
        if not conteudo or conteudo.startswith("#") or "=" not in conteudo:
            linhas_finais.append(linha)
            continue

        chave = conteudo.partition("=")[0].strip()
        if chave not in pendentes:
            linhas_finais.append(linha)
            continue

        valor = pendentes.pop(chave)
        if valor is None or str(valor).strip() == "":
            continue  # remover a chave
        linhas_finais.append(f"{chave}={str(valor).strip()}")

    novas = {chave: valor for chave, valor in pendentes.items() if valor}
    if novas:
        linhas_finais.append("")
        linhas_finais.append(_CABECALHO_NOVAS)
        linhas_finais.extend(f"{chave}={str(valor).strip()}" for chave, valor in novas.items())

    conteudo_final = "\n".join(linhas_finais).rstrip("\n") + "\n"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(conteudo_final, encoding="utf-8")
    os.chmod(caminho, 0o600)
    return backup


def _validar_chaves(valores: Mapping[str, str | None]) -> None:
    desconhecidas = [chave for chave in valores if chave not in CHAVES_GERENCIADAS]
    if desconhecidas:
        raise ValidationError(
            f"Chave(s) fora da lista gerenciada: {', '.join(sorted(desconhecidas))}. "
            f"Permitidas: {', '.join(CHAVES_GERENCIADAS)}."
        )


def _criar_backup(caminho: Path, *, quando: dt.datetime | None = None) -> Path:
    momento = (quando or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    destino = caminho.parent / f"{caminho.name}.bak-{momento}"
    if caminho.exists():
        shutil.copy2(caminho, destino)
        os.chmod(destino, 0o600)
    return destino
