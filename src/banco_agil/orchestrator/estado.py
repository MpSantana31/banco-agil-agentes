"""Estado de uma sessão de atendimento.

Todo o "cérebro oculto" do atendimento vive aqui: quem está autenticado, qual
agente está ativo, quantas tentativas já foram gastas e qual pedido está
aguardando nova análise. Nada disso o cliente vê.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.messages import BaseMessage

from banco_agil.domain.modelos import Cliente, SolicitacaoAumento

__all__ = ["EstadoSessao", "Handoff", "AGENTE_TRIAGEM"]

AGENTE_TRIAGEM = "triagem"

# Motivos de encerramento — usados pela UI/diagnóstico, nunca mostrados crus.
MOTIVO_AUTENTICACAO_EXCEDIDA = "autenticacao_falhou_3_tentativas"
MOTIVO_CLIENTE_PEDIU = "cliente_solicitou"
MOTIVO_ASSUNTO_CONCLUIDO = "assunto_concluido"
MOTIVO_ERRO_INTERNO = "erro_interno"


@dataclass(frozen=True, slots=True)
class Handoff:
    """Pedido de passagem de bastão: quem assume e por quê."""

    destino: str
    motivo: str


@dataclass(slots=True)
class EstadoSessao:
    """Estado mutável de um atendimento (uma sessão = uma conversa)."""

    agente_ativo: str = AGENTE_TRIAGEM
    cliente: Cliente | None = None
    autenticado: bool = False
    tentativas_autenticacao: int = 0
    encerrado: bool = False
    motivo_encerramento: str | None = None
    handoff: Handoff | None = None
    pedido_pendente: float | None = None
    cpf_informado: str | None = None
    """CPF já validado (dígito) e ainda sem autenticação concluída.

    Serve para a interface saber qual dado pedir em seguida — o campo esperado é
    decisão determinística, nunca do modelo.
    """
    ultima_solicitacao: SolicitacaoAumento | None = None
    dados_entrevista: dict[str, object] = field(default_factory=dict)
    mensagens: list[BaseMessage] = field(default_factory=list)
    # Trilha interna (auditoria na UI); não é enviada ao modelo.
    trilha: list[str] = field(default_factory=list)

    @property
    def primeiro_nome(self) -> str:
        return self.cliente.primeiro_nome if self.cliente else ""

    def registrar_trilha(self, evento: str) -> None:
        """Anota um evento interno (ex.: ``credito -> solicitou 20000``)."""
        self.trilha.append(evento)
        del self.trilha[:-50]

    def pedir_handoff(self, destino: str, motivo: str) -> None:
        self.handoff = Handoff(destino=destino, motivo=motivo)

    def consumir_handoff(self) -> Handoff | None:
        pedido, self.handoff = self.handoff, None
        return pedido

    def encerrar(self, motivo: str) -> None:
        self.encerrado = True
        self.motivo_encerramento = motivo
        self.registrar_trilha(f"encerrado: {motivo}")

    def reset(self) -> None:
        """Limpa o estado da conversa (mantém configuração de serviços)."""
        self.__init__()  # type: ignore[misc]
