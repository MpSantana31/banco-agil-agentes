"""Catálogo de modelos por provedor e teste de conexão de um slot.

Dois recursos da aba de configuração:

- ``listar_modelos``: tenta a listagem **real** do provedor (o que existe hoje) e
  cai numa lista curada quando não há endpoint, chave ou rede. Devolve também a
  origem, para a tela poder dizer "lista vinda da API" ou "lista curada".
- ``testar_passo``: faz uma chamada mínima no slot e classifica a falha (quota,
  modelo inexistente, chave inválida) — assim o problema aparece na tela de
  configuração, e não no meio de um atendimento.
"""

from __future__ import annotations

import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass

import httpx
from langchain_core.messages import HumanMessage

from banco_agil.config import Settings, obter_settings
from banco_agil.errors import BancoAgilError, LLMProviderError
from banco_agil.llm.corrente import PassoDaCadeia, e_falha_de_infra
from banco_agil.llm.factory import construir_modelo, modelo_padrao_do_provedor

__all__ = [
    "MODELOS_CONHECIDOS",
    "ORIGEM_API",
    "ORIGEM_CURADA",
    "ResultadoDoTeste",
    "listar_modelos",
    "testar_passo",
]

ORIGEM_API = "api"
ORIGEM_CURADA = "curado"

# Usada só se a listagem da API falhar: evita tela vazia, não substitui a real.
MODELOS_CONHECIDOS: dict[str, tuple[str, ...]] = {
    "gemini": ("gemini-3.6-flash", "gemini-3.6-pro", "gemini-3.6-flash-lite"),
    "openai": ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"),
    "openrouter": (
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "google/gemini-3.6-flash",
    ),
    "groq": ("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"),
    "togetherai": (
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "Qwen/Qwen2.5-72B-Instruct-Turbo",
    ),
    "ollama": ("llama3.1", "qwen2.5", "mistral"),
    "compativel": ("gpt-4o-mini",),
    "fake": ("fake",),
}

_ENDPOINTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1/models",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "groq": "https://api.groq.com/openai/v1/models",
    "togetherai": "https://api.together.xyz/v1/models",
    "ollama": "http://localhost:11434/api/tags",
}

_TEMPO_LIMITE = 8.0


@dataclass(frozen=True, slots=True)
class ResultadoDoTeste:
    """Resultado do botão "testar conexão" de um slot."""

    passo: PassoDaCadeia
    ok: bool
    detalhe: str
    milissegundos: int

    def descricao(self) -> str:
        estado = "ok" if self.ok else "falhou"
        return f"{self.passo.rotulo}: {estado} ({self.milissegundos} ms) — {self.detalhe}"


def listar_modelos(
    provedor: str,
    settings: Settings | None = None,
    *,
    cliente: httpx.Client | None = None,
) -> tuple[list[str], str]:
    """Devolve ``(modelos, origem)``; origem é ``api`` ou ``curado``."""
    settings = settings or obter_settings()
    provedor = (provedor or "").strip().lower()

    if provedor == "fake":
        return list(MODELOS_CONHECIDOS["fake"]), ORIGEM_CURADA

    try:
        modelos = _consultar_listagem(provedor, settings, cliente=cliente)
    except (httpx.HTTPError, BancoAgilError, ValueError, KeyError):
        modelos = []

    if modelos:
        return sorted(set(modelos)), ORIGEM_API
    return list(
        MODELOS_CONHECIDOS.get(provedor, (modelo_padrao_do_provedor(provedor),))
    ), ORIGEM_CURADA


def testar_passo(
    passo: PassoDaCadeia,
    settings: Settings | None = None,
) -> ResultadoDoTeste:
    """Faz uma chamada mínima no slot e diz se ele responde."""
    settings = settings or obter_settings()
    inicio = time.monotonic()

    try:
        modelo = construir_modelo(settings, provedor=passo.provedor, modelo=passo.modelo)
        modelo.invoke([HumanMessage(content="responda apenas: ok")])
    except BaseException as erro:  # noqa: BLE001 - a tela precisa do motivo
        milissegundos = int((time.monotonic() - inicio) * 1000)
        return ResultadoDoTeste(
            passo=passo,
            ok=False,
            detalhe=_classificar_falha(erro),
            milissegundos=milissegundos,
        )

    return ResultadoDoTeste(
        passo=passo,
        ok=True,
        detalhe="conexão e credencial válidas",
        milissegundos=int((time.monotonic() - inicio) * 1000),
    )


def _classificar_falha(erro: BaseException) -> str:
    """Traduz o erro do provedor para algo acionável na tela de configuração."""
    nome = type(erro).__name__
    texto = str(erro)[:200]
    minusculo = texto.lower()
    codigo = getattr(erro, "status_code", None)

    if (
        codigo == 429
        or "quota" in minusculo
        or "ratelimit" in nome.lower()
        or "rate limit" in minusculo
    ):
        return f"limite de uso/quota atingido ({nome})"
    if codigo == 404 or "notfound" in nome.lower() or "not found" in minusculo:
        return f"modelo inexistente ou descontinuado ({nome})"
    if codigo in (401, 403) or "authentication" in nome.lower() or "api key" in minusculo:
        return f"chave inválida ou sem permissão ({nome})"
    if isinstance(erro, LLMProviderError):
        return f"configuração incompleta: {texto}"
    if e_falha_de_infra(erro):
        return f"falha temporária do provedor ({nome})"
    return f"erro de contrato: verifique o modelo informado ({nome})"


def _consultar_listagem(
    provedor: str, settings: Settings, *, cliente: httpx.Client | None = None
) -> list[str]:
    if provedor == "gemini":
        return _listar_gemini(settings, cliente=cliente)
    if provedor == "compativel":
        return _listar_compativel(settings, cliente=cliente)

    url = _ENDPOINTS.get(provedor)
    if url is None:
        return []

    cabecalhos: dict[str, str] = {}
    chave = settings.chave_do_provedor(provedor)
    if chave is not None and chave.get_secret_value().strip():
        cabecalhos["Authorization"] = f"Bearer {chave.get_secret_value()}"

    with _abrir(cliente) as http:
        resposta = http.get(url, headers=cabecalhos, timeout=_TEMPO_LIMITE)
        resposta.raise_for_status()
        dados = resposta.json()

    if provedor == "ollama":
        return [item["name"] for item in dados.get("models", [])]
    return [item["id"] for item in dados.get("data", [])]


def _listar_gemini(settings: Settings, *, cliente: httpx.Client | None = None) -> list[str]:
    chave = settings.chave_do_provedor("gemini")
    if chave is None:
        return []
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    with _abrir(cliente) as http:
        resposta = http.get(
            url, params={"key": chave.get_secret_value(), "pageSize": 100}, timeout=_TEMPO_LIMITE
        )
        resposta.raise_for_status()
        dados = resposta.json()
    return [
        item["name"].removeprefix("models/")
        for item in dados.get("models", [])
        if "generateContent" in item.get("supportedGenerationMethods", [])
    ]


def _listar_compativel(settings: Settings, *, cliente: httpx.Client | None = None) -> list[str]:
    base = (settings.llm_base_url or "").rstrip("/")
    if not base:
        return []
    with _abrir(cliente) as http:
        resposta = http.get(f"{base}/models", timeout=_TEMPO_LIMITE)
        resposta.raise_for_status()
        dados = resposta.json()
    return [item["id"] for item in dados.get("data", [])]


def _abrir(cliente: httpx.Client | None) -> AbstractContextManager[httpx.Client]:
    """Abre um cliente novo, ou empresta o informado sem fechá-lo."""
    if cliente is not None:
        return nullcontext(cliente)
    return httpx.Client()
