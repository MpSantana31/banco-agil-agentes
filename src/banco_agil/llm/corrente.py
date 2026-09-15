"""Cadeia de modelos com fallback: primário → secundário → terciário.

O cliente não percebe a troca: o histórico já está no formato de mensagens, então
"trocar de modelo" é só trocar o objeto que responde e repetir o passo.

**Regra central:** fallback é para falha de *infraestrutura* (quota, indisponi-
bilidade, timeout, chave inválida, modelo descontinuado). Falha de *contrato*
— o modelo mandou um argumento que a ferramenta não aceita, ou um formato que não
existe — é bug nosso e **não** troca de modelo: isso esconderia o defeito e ainda
gastaria a quota do próximo provedor. Esses casos viram log e mensagem.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool

from banco_agil.errors import LLMProviderError

__all__ = [
    "PassoDaCadeia",
    "EstadoDaCadeia",
    "CorrenteDeModelos",
    "ModeloConversacional",
    "e_falha_de_infra",
    "montar_cadeia",
]

CODIGOS_DE_INFRA: frozenset[int] = frozenset(
    {401, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504, 529}
)

# Nome da exceção é mais estável que a mensagem entre as bibliotecas de provedor.
_TRECHOS_NO_NOME: tuple[str, ...] = (
    "RateLimit",
    "ResourceExhausted",
    "ServiceUnavailable",
    "InternalServer",
    "Overloaded",
    "Timeout",
    "Connect",
    "APIConnection",
    "Authentication",
    "PermissionDenied",
    "NotFound",
    "DeadlineExceeded",
    "Unavailable",
)

_TRECHOS_NA_MENSAGEM: tuple[str, ...] = (
    "resource_exhausted",
    "resource exhausted",
    "quota",
    "rate limit",
    "ratelimit",
    "too many requests",
    "overloaded",
    "timed out",
    "timeout",
    "connection reset",
    "temporarily unavailable",
    "service unavailable",
    "upstream error",
    "provider_unavailable",
    "try again",
    "bad gateway",
    "gateway timeout",
    "capacity",
)

_CODIGO_NA_MENSAGEM = re.compile(
    r"(?:code|status|status_code|http_status)['\"]?\s*[:=]\s*['\"]?(\d{3})"
)
# Contrato/validação: culpa nossa, não justifica trocar de modelo.
_CODIGOS_DE_CONTRATO = frozenset({400, 422})

# Soluço: vale nova tentativa no mesmo modelo (não em outro provedor).
CODIGOS_TRANSITORIOS = frozenset({408, 425, 500, 502, 503, 504, 529})

_TRECHOS_TRANSITORIOS: tuple[str, ...] = (
    "overloaded",
    "timed out",
    "timeout",
    "connection reset",
    "connection error",
    "temporarily unavailable",
    "service unavailable",
    "upstream error",
    "provider_unavailable",
    "bad gateway",
    "gateway timeout",
    "try again",
    "capacity",
)


def _codigo_na_mensagem(mensagem: str) -> int | None:
    achado = _CODIGO_NA_MENSAGEM.search(mensagem)
    return int(achado.group(1)) if achado else None


def e_falha_transitoria(erro: BaseException) -> bool:
    """Soluço passageiro que vale uma nova tentativa **no mesmo modelo**.

    Distinto de ``e_falha_de_infra``: quota estourada e chave inválida justificam trocar
    de modelo, mas não adianta repetir a chamada no mesmo provedor.
    """
    if isinstance(
        erro, (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError)
    ):
        return True

    for atributo in ("status_code", "http_status"):
        codigo = getattr(erro, atributo, None)
        if isinstance(codigo, int):
            return codigo in CODIGOS_TRANSITORIOS

    codigo = _codigo_na_mensagem(str(erro))
    if codigo is not None:
        return codigo in CODIGOS_TRANSITORIOS

    nome = type(erro).__name__.lower()
    if "timeout" in nome or "connection" in nome or "servererror" in nome:
        return True

    minuscula = str(erro).lower()
    return any(trecho in minuscula for trecho in _TRECHOS_TRANSITORIOS)


def e_falha_de_infra(erro: BaseException) -> bool:
    """Diz se a falha justifica tentar o próximo modelo da cadeia.

    Falha de infraestrutura (quota, indisponibilidade, timeout, modelo fora do ar)
    troca de modelo. Falha de contrato — argumento inválido, schema recusado — **não**
    troca: seria esconder um bug nosso atrás do fallback.
    """
    if isinstance(
        erro, (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError)
    ):
        return True

    for atributo in ("status_code", "http_status"):
        codigo = getattr(erro, atributo, None)
        if isinstance(codigo, int):
            return codigo in CODIGOS_DE_INFRA

    nome = type(erro).__name__
    if any(trecho in nome for trecho in _TRECHOS_NO_NOME):
        return True

    # Provedores OpenAI-like embrulham o status do upstream num ValueError genérico:
    # sem ler o código aqui, indisponibilidade real passa por bug de contrato.
    mensagem = str(erro)
    codigo = _codigo_na_mensagem(mensagem)
    if codigo is not None:
        if codigo in _CODIGOS_DE_CONTRATO:
            return False
        return codigo in CODIGOS_DE_INFRA

    minuscula = mensagem.lower()
    return any(trecho in minuscula for trecho in _TRECHOS_NA_MENSAGEM)


@dataclass(frozen=True, slots=True)
class PassoDaCadeia:
    """Um slot da cadeia: provedor + modelo, na ordem de tentativa."""

    provedor: str
    modelo: str

    @property
    def rotulo(self) -> str:
        return f"{self.provedor}:{self.modelo}"

    @classmethod
    def de_texto(cls, texto: str) -> PassoDaCadeia:
        """Lê ``provedor:modelo`` (o modelo pode conter ``:`` e ``/``)."""
        provedor, _, modelo = texto.partition(":")
        if not provedor.strip() or not modelo.strip():
            raise LLMProviderError(
                f"Slot de cadeia inválido: {texto!r}. Use o formato provedor:modelo."
            )
        return cls(provedor=provedor.strip(), modelo=modelo.strip())

    def para_texto(self) -> str:
        return self.rotulo


@dataclass
class EstadoDaCadeia:
    """Memória da cadeia, compartilhada entre os ``bind_tools`` de cada turno.

    Sem compartilhar, cada turno criaria um contador novo e o painel de auditoria
    mostraria "0 trocas" mesmo depois de três fallbacks.
    """

    indice_atual: int = 0
    trocas: int = 0
    ultimo_passo: PassoDaCadeia | None = None
    falhas: list[tuple[PassoDaCadeia, str]] = field(default_factory=list)

    def registrar_falha(self, passo: PassoDaCadeia, erro: BaseException) -> None:
        self.falhas.append((passo, f"{type(erro).__name__}: {erro}"))


@runtime_checkable
class ModeloConversacional(Protocol):
    """O que o orquestrador espera de um modelo: responder e receber ferramentas."""

    def bind_tools(
        self, ferramentas: Sequence[BaseTool], **kwargs: Any
    ) -> ModeloConversacional: ...

    def invoke(self, entrada: Any, **kwargs: Any) -> AIMessage: ...


class CorrenteDeModelos:
    """Tenta cada modelo da cadeia, na ordem, até um responder.

    A falha de infraestrutura de um slot é registrada e o próximo assume *no
    mesmo turno* — o cliente não vê erro nem percebe a troca de provedor. Depois
    de um fallback a cadeia fica no slot que funcionou (não insistimos no que
    acabou de estourar quota a cada turno).
    """

    def __init__(
        self,
        modelos: Sequence[BaseChatModel],
        passos: Sequence[PassoDaCadeia],
        *,
        estado: EstadoDaCadeia | None = None,
        ao_trocar: Callable[[int, PassoDaCadeia, BaseException], None] | None = None,
        tentativas_por_modelo: int = 2,
    ) -> None:
        if not modelos:
            raise LLMProviderError("Cadeia de modelos vazia.")
        if len(modelos) != len(passos):
            raise LLMProviderError("Cadeia com modelos e passos de tamanhos diferentes.")
        if tentativas_por_modelo < 1:
            raise LLMProviderError("tentativas_por_modelo precisa ser >= 1.")
        self._modelos = list(modelos)
        self._passos = list(passos)
        self._ao_trocar = ao_trocar
        self.tentativas_por_modelo = tentativas_por_modelo
        self.estado = estado if estado is not None else EstadoDaCadeia(ultimo_passo=passos[0])

    @property
    def passos(self) -> tuple[PassoDaCadeia, ...]:
        return tuple(self._passos)

    @property
    def trocas(self) -> int:
        return self.estado.trocas

    @property
    def falhas(self) -> list[tuple[PassoDaCadeia, str]]:
        return self.estado.falhas

    @property
    def rotulo_atual(self) -> str:
        passo = self.estado.ultimo_passo or self._passos[0]
        return passo.rotulo

    def bind_tools(self, ferramentas: Sequence[BaseTool], **kwargs: Any) -> CorrenteDeModelos:
        """Aplica o vínculo de ferramentas em **todos** os modelos da cadeia.

        Sem isso, o secundário assumiria sem ferramenta nenhuma e o agente ficaria
        mudo — é o detalhe que quebra a maioria dos fallbacks.
        """
        return CorrenteDeModelos(
            [modelo.bind_tools(ferramentas, **kwargs) for modelo in self._modelos],
            self._passos,
            estado=self.estado,
            ao_trocar=self._ao_trocar,
            tentativas_por_modelo=self.tentativas_por_modelo,
        )

    def invoke(self, entrada: Any, **kwargs: Any) -> AIMessage:
        inicio = self.estado.indice_atual
        ultimo_erro: BaseException | None = None
        houve_falha_antes = False

        for deslocamento in range(len(self._modelos) - inicio):
            indice = inicio + deslocamento
            passo = self._passos[indice]

            for tentativa in range(1, self.tentativas_por_modelo + 1):
                try:
                    resposta = self._modelos[indice].invoke(entrada, **kwargs)
                except BaseException as erro:  # noqa: BLE001 - classificamos abaixo
                    if not e_falha_de_infra(erro):
                        raise
                    ultimo_erro = erro
                    # Soluço (5xx/timeout) insiste no mesmo modelo; quota estoura e troca.
                    ultima_tentativa = tentativa == self.tentativas_por_modelo
                    if not ultima_tentativa and e_falha_transitoria(erro):
                        continue
                    houve_falha_antes = True
                    self.estado.registrar_falha(passo, erro)
                    if self._ao_trocar is not None:
                        self._ao_trocar(indice, passo, erro)
                    break
                else:
                    if houve_falha_antes:
                        self.estado.trocas += 1
                    self.estado.indice_atual = indice
                    self.estado.ultimo_passo = passo
                    return resposta

        if ultimo_erro is not None:
            raise LLMProviderError(
                "Nenhum modelo da cadeia respondeu. "
                f"Tentados: {', '.join(passo.rotulo for passo in self._passos[inicio:])}. "
                f"Última falha: {type(ultimo_erro).__name__}."
            ) from ultimo_erro
        raise LLMProviderError("Cadeia de modelos sem modelos a tentar.")


def montar_cadeia(
    modelos: Sequence[BaseChatModel],
    passos: Sequence[PassoDaCadeia],
    *,
    ao_trocar: Callable[[int, PassoDaCadeia, BaseException], None] | None = None,
    tentativas_por_modelo: int = 2,
) -> BaseChatModel | CorrenteDeModelos:
    """Devolve o próprio modelo quando há um só slot, ou a cadeia com fallback."""
    if len(modelos) == 1:
        return modelos[0]
    return CorrenteDeModelos(
        modelos, passos, ao_trocar=ao_trocar, tentativas_por_modelo=tentativas_por_modelo
    )
