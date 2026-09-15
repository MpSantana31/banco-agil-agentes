"""Fábrica de modelos de chat — o ponto onde a troca de provedor acontece.

O resto do sistema depende apenas de ``BaseChatModel`` (langchain-core). Trocar
Gemini por OpenAI, OpenRouter, Groq, Ollama ou qualquer endpoint compatível com
OpenAI é mudar ``LLM_PROVIDER`` no `.env` — zero alteração em agente, prompt ou
ferramenta. Adicionar um provedor novo = uma entrada em ``_CONSTRUTORES``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from banco_agil.config import PROVEDORES_SUPORTADOS, Settings, obter_settings
from banco_agil.errors import LLMProviderError
from banco_agil.llm.corrente import (
    CorrenteDeModelos,
    PassoDaCadeia,
    montar_cadeia,
)

__all__ = ["construir_modelo", "modelo_padrao_do_provedor", "PROVEDORES_SUPORTADOS"]

MODELOS_PADRAO: dict[str, str] = {
    "gemini": "gemini-3.6-flash",
    "openai": "gpt-4o-mini",
    "openrouter": "google/gemini-3.6-flash",
    "groq": "llama-3.3-70b-versatile",
    "togetherai": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "ollama": "llama3.1",
    "compativel": "gpt-4o-mini",
}

_BASE_URLS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "togetherai": "https://api.together.xyz/v1",
    "ollama": "http://localhost:11434/v1",
}


def modelo_padrao_do_provedor(provedor: str) -> str:
    """Modelo default do provedor (usado quando ``LLM_MODEL`` está vazio)."""
    if provedor not in PROVEDORES_SUPORTADOS:
        raise LLMProviderError(
            f"Provedor desconhecido: {provedor!r}. "
            f"Disponíveis: {', '.join(sorted(PROVEDORES_SUPORTADOS))}."
        )
    return MODELOS_PADRAO.get(provedor, "gpt-4o-mini")


def _exigir_chave(settings: Settings, provedor: str) -> str:
    chave = settings.chave_do_provedor(provedor)
    if chave is None or not chave.get_secret_value().strip():
        raise LLMProviderError(
            f"Provedor {provedor!r} selecionado, mas nenhuma chave configurada. "
            f"Defina a variável de ambiente correspondente no `.env` "
            f"(veja `.env.example`)."
        )
    return chave.get_secret_value()


def _construir_gemini(modelo: str, settings: Settings, **kwargs: Any) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=modelo,
        google_api_key=_exigir_chave(settings, "gemini"),
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
        **kwargs,
    )


def _construir_openai(modelo: str, settings: Settings, **kwargs: Any) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=modelo,
        api_key=_exigir_chave(settings, "openai"),
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
        **kwargs,
    )


def _construtor_compativel(nome_provedor: str) -> Callable[..., BaseChatModel]:
    """Cria o construtor de um provedor que fala o protocolo da OpenAI."""

    def construir(modelo: str, settings: Settings, **kwargs: Any) -> BaseChatModel:
        from langchain_openai import ChatOpenAI

        base_url = _BASE_URLS.get(nome_provedor) or settings.llm_base_url
        if not base_url:
            raise LLMProviderError(
                f"Provedor {nome_provedor!r} exige uma URL de endpoint. "
                "Defina LLM_BASE_URL no `.env`."
            )
        chave = settings.chave_do_provedor(nome_provedor)
        if chave is None:
            if nome_provedor != "ollama":  # Ollama local não usa chave
                raise LLMProviderError(
                    f"Provedor {nome_provedor!r} precisa de chave de API (veja `.env.example`)."
                )
            chave = None

        return ChatOpenAI(
            model=modelo,
            api_key=chave.get_secret_value() if chave else "not-needed",
            base_url=base_url,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
            max_retries=settings.llm_max_retries,
            **kwargs,
        )

    return construir


class ModeloFake(BaseChatModel):
    """Modelo determinístico, sem rede — usado na demo sem chave e nos testes de UI."""

    resposta: str = (
        "Modo demonstração sem provedor externo: aqui eu respondo sempre a mesma "
        "frase. Configure um provedor na aba Configurações para conversar de verdade."
    )

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.resposta))])

    def bind_tools(self, tools: Any, **kwargs: Any) -> ModeloFake:  # type: ignore[override]
        """Aceita ferramentas e ignora: sem provedor real não há chamada de ferramenta."""
        return self


def _construir_fake(modelo: str, settings: Settings, **kwargs: Any) -> BaseChatModel:
    """Modelo determinístico, sem rede — usado em testes e na demo sem chave."""
    return ModeloFake()


_CONSTRUTORES: dict[str, Callable[..., BaseChatModel]] = {
    "gemini": _construir_gemini,
    "openai": _construir_openai,
    "openrouter": _construtor_compativel("openrouter"),
    "groq": _construtor_compativel("groq"),
    "togetherai": _construtor_compativel("togetherai"),
    "ollama": _construtor_compativel("ollama"),
    "compativel": _construtor_compativel("compativel"),
    "fake": _construir_fake,
}


def passos_da_cadeia(settings: Settings | None = None) -> list[PassoDaCadeia]:
    """Lê a cadeia do `.env`: primário (``LLM_PROVIDER``/``LLM_MODEL``) + fallbacks."""
    settings = settings or obter_settings()
    provedor_primario = (settings.llm_provider or "").strip().lower()
    modelo_primario = (settings.llm_model or "").strip() or modelo_padrao_do_provedor(
        provedor_primario
    )

    passos = [PassoDaCadeia(provedor=provedor_primario, modelo=modelo_primario)]
    for bruto in (settings.llm_fallback_1, settings.llm_fallback_2):
        if bruto.strip():
            passos.append(PassoDaCadeia.de_texto(bruto))
    return passos


def construir_cadeia(
    settings: Settings | None = None,
    *,
    passos: Sequence[PassoDaCadeia] | None = None,
    ao_trocar: Callable[[int, PassoDaCadeia, BaseException], None] | None = None,
) -> BaseChatModel | CorrenteDeModelos:
    """Monta o modelo (ou a cadeia com fallback) a partir da configuração.

    Cada provedor da cadeia usa a sua própria chave, então um problema de quota
    em um deles não derruba o atendimento.
    """
    settings = settings or obter_settings()
    passos = list(passos or passos_da_cadeia(settings))

    modelos: list[BaseChatModel] = []
    for passo in passos:
        modelos.append(construir_modelo(settings, provedor=passo.provedor, modelo=passo.modelo))
    return montar_cadeia(
        modelos,
        passos,
        ao_trocar=ao_trocar,
        tentativas_por_modelo=settings.llm_tentativas_por_modelo,
    )


def construir_modelo(
    settings: Settings | None = None,
    *,
    provedor: str | None = None,
    modelo: str | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """Devolve um ``BaseChatModel`` pronto para uso.

    Levanta ``LLMProviderError`` quando o provedor não existe ou quando falta
    credencial — falha cedo, na inicialização, e não no meio de um atendimento.
    """
    settings = settings or obter_settings()
    provedor = (provedor or settings.llm_provider).strip().lower()

    construtor = _CONSTRUTORES.get(provedor)
    if construtor is None:
        raise LLMProviderError(
            f"Provedor desconhecido: {provedor!r}. Disponíveis: {', '.join(sorted(_CONSTRUTORES))}."
        )

    modelo = (modelo or settings.llm_model or modelo_padrao_do_provedor(provedor)).strip()
    return construtor(modelo, settings, **kwargs)
