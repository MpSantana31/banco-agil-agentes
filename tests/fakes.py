"""Modelo de chat roteirizado: permite testar o laço de conversa sem LLM real.

Cada teste escreve o "roteiro" — a sequência exata de respostas que o modelo
daria — e o orquestrador é exercitado de verdade: ferramentas são executadas,
CSVs são escritos, handoffs acontecem. Determinístico, offline e instantâneo.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr


def texto(conteudo: str) -> AIMessage:
    """Resposta final em texto (o que o cliente veria)."""
    return AIMessage(content=conteudo)


def chamada(nome: str, identificador: str | None = None, **argumentos: object) -> AIMessage:
    """Resposta que pede a execução de uma ferramenta."""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": nome,
                "args": argumentos,
                "id": identificador or f"call_{nome}",
                "type": "tool_call",
            }
        ],
    )


def chamada_com_texto(conteudo: str, nome: str, **argumentos: object) -> AIMessage:
    """Resposta que fala algo ao cliente E pede uma ferramenta no mesmo turno."""
    return AIMessage(
        content=conteudo,
        tool_calls=[
            {
                "name": nome,
                "args": argumentos,
                "id": f"call_{nome}",
                "type": "tool_call",
            }
        ],
    )


def chamadas(*pedidos: tuple[str, dict[str, object]]) -> AIMessage:
    """Resposta que pede várias ferramentas de uma vez."""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": nome,
                "args": argumentos,
                "id": f"call_{indice}_{nome}",
                "type": "tool_call",
            }
            for indice, (nome, argumentos) in enumerate(pedidos)
        ],
    )


class ScriptedChatModel(BaseChatModel):
    """Devolve, em ordem, as respostas programadas; registra o que recebeu."""

    roteiro: list[AIMessage] = Field(default_factory=list)
    historicos: list[list[BaseMessage]] = Field(default_factory=list)
    ferramentas_oferecidas: list[list[str]] = Field(default_factory=list)

    _indice: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Sequence[str] | None = None,
        run_manager: object | None = None,
        **kwargs: object,
    ) -> ChatResult:
        self.historicos.append(list(messages))
        if self._indice >= len(self.roteiro):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
        mensagem = self.roteiro[self._indice]
        self._indice += 1
        return ChatResult(generations=[ChatGeneration(message=mensagem)])

    def bind_tools(self, tools: Iterable[object], **kwargs: object) -> ScriptedChatModel:
        self.ferramentas_oferecidas.append([getattr(t, "name", str(t)) for t in tools])
        return self

    @property
    def ultimo_historico(self) -> list[BaseMessage]:
        return self.historicos[-1] if self.historicos else []

    @property
    def ultimo_sistema(self) -> str:
        for mensagem in self.ultimo_historico:
            if mensagem.type == "system":
                return str(mensagem.content)
        return ""
