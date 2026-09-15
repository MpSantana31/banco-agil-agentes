"""Laço de conversa: o coração do atendimento.

Responsabilidades:

- manter o histórico no formato que a API de chat exige (``AIMessage`` com
  ``tool_calls`` seguido dos ``ToolMessage`` correspondentes);
- **esconder o handoff**: quando um papel passa o bastão, a fala do papel
  anterior não vai para o cliente — só a do novo papel, já no assunto novo;
- garantir que nada estoure: ferramenta faltando, resposta vazia ou troca de
  papel em excesso caem em mensagem de contorno;
- aplicar as decisões que **não** podem depender do modelo (esgotar as 3
  tentativas de autenticação encerra o atendimento com mensagem fixa).
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from banco_agil.agents.base import RegistroDeAgentes
from banco_agil.agents.especificacoes import construir_registro
from banco_agil.agents.ferramentas import FALHA_INTERNA, construir_ferramentas
from banco_agil.agents.prompts import nota_de_handoff
from banco_agil.domain.modelos import formatar_moeda
from banco_agil.errors import BancoAgilError, ValidationError
from banco_agil.observability.log import registrar_falha
from banco_agil.orchestrator.campos import CampoEsperado, campo_esperado
from banco_agil.orchestrator.estado import (
    MOTIVO_AUTENTICACAO_EXCEDIDA,
    EstadoSessao,
)
from banco_agil.servicos import Servicos
from banco_agil.tools.validators import mascarar_cpf, validar_cpf

__all__ = ["Atendimento", "Resposta"]

# CPF no texto do cliente: 11 dígitos com separadores opcionais (bordas de palavra).
_CANDIDATOS_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")

MAX_PASSOS_DE_FERRAMENTA = 4
MAX_TROCAS_DE_PAPEL = 3

MENSAGEM_JA_ENCERRADO = (
    "Este atendimento já foi encerrado. Se precisar de algo mais, inicie uma nova "
    "conversa — será um prazer ajudar."
)
MENSAGEM_CONTORNO = (
    "Desculpe, tive um problema para concluir isso agora. Pode tentar novamente em instantes?"
)
MENSAGEM_DESPEDIDA = "Foi um prazer ajudar. Se precisar de algo mais, é só chamar."
MENSAGEM_AUTENTICACAO_EXCEDIDA = (
    "Para sua segurança, não conseguimos confirmar seus dados e não podemos seguir com o "
    "atendimento por aqui. Por favor, procure uma agência do Banco Ágil levando um documento "
    "de identificação. Foi um prazer falar com você."
)
NOTA_ABERTURA = (
    "[contexto interno] O cliente acabou de iniciar a conversa. Cumprimente-o de forma cordial "
    "e peça o CPF para começar a autenticação."
)


@dataclass(slots=True)
class Resposta:
    """Resultado de um turno: o que o cliente vê e o que a auditoria registra."""

    texto: str
    agente: str
    encerrado: bool = False
    trocas_de_papel: int = 0
    ferramentas_chamadas: list[str] = field(default_factory=list)
    fechamento_forcado: bool = False
    campo_esperado: CampoEsperado | None = None
    """Dado que a interface deve destacar agora (CPF, data de nascimento) ou ``None``.

    Preenchido pela máquina de estados — a UI nunca deduz isso do texto do modelo.
    """


def _texto_da_mensagem(mensagem: BaseMessage | None) -> str:
    """Extrai texto de um ``AIMessage`` (string ou lista de blocos)."""
    if mensagem is None:
        return ""
    conteudo = mensagem.content
    if isinstance(conteudo, str):
        return conteudo.strip()
    partes: list[str] = []
    for bloco in conteudo if isinstance(conteudo, list) else []:
        if isinstance(bloco, str):
            partes.append(bloco)
        elif isinstance(bloco, dict) and bloco.get("type") == "text":
            partes.append(str(bloco.get("text", "")))
    return "\n".join(partes).strip()


class Atendimento:
    """Conduz uma conversa entre o cliente e os quatro papéis do Banco Ágil."""

    def __init__(
        self,
        modelo: BaseChatModel,
        servicos: Servicos,
        *,
        registro: RegistroDeAgentes | None = None,
        estado: EstadoSessao | None = None,
    ) -> None:
        self._modelo = modelo
        self._servicos = servicos
        self._registro = registro or construir_registro(servicos.settings)
        self.estado = estado or EstadoSessao()
        self._ferramentas = construir_ferramentas(self.estado, servicos, self._registro)

    @property
    def registro(self) -> RegistroDeAgentes:
        return self._registro

    def abrir(self) -> Resposta:
        """Primeiro turno: o atendente se apresenta e pede o CPF."""
        if self.estado.encerrado:
            return self._resposta_final(MENSAGEM_JA_ENCERRADO)
        if self.estado.mensagens:
            raise BancoAgilError("O atendimento já foi aberto.")
        self.estado.mensagens.append(SystemMessage(NOTA_ABERTURA))
        resposta = self._conduzir_turno()
        self._remover_nota_interna(NOTA_ABERTURA)
        return resposta

    def _registrar_cpf_do_texto(self, mensagem: str) -> None:
        """Registra um CPF válido que veio no texto, sem depender do modelo.

        Por que existe: o campo dedicado da interface só troca de CPF para calendário
        quando o estado sabe que o CPF já chegou. Se isso dependesse de o modelo chamar
        ``registrar_cpf``, uma vez esquecida a chamada a tela ficaria travada pedindo o
        CPF de novo. Aqui a leitura é determinística (Python puro), como o resto da
        regra de negócio.
        """
        if self.estado.autenticado or self.estado.cpf_informado:
            return
        for candidato in _CANDIDATOS_CPF.findall(mensagem):
            with contextlib.suppress(ValidationError):
                self.estado.cpf_informado = validar_cpf(candidato)
                self.estado.registrar_trilha(
                    f"cpf lido do texto {mascarar_cpf(self.estado.cpf_informado)}"
                )
                return

    def responder(self, mensagem_do_cliente: str) -> Resposta:
        """Processa uma mensagem do cliente e devolve o que deve ser exibido."""
        if self.estado.encerrado:
            return self._resposta_final(MENSAGEM_JA_ENCERRADO)

        self._registrar_cpf_do_texto(mensagem_do_cliente)

        texto = (mensagem_do_cliente or "").strip()
        if not texto:
            raise BancoAgilError("Mensagem vazia.")

        self.estado.mensagens.append(HumanMessage(texto))
        return self._conduzir_turno()

    def _conduzir_turno(self) -> Resposta:
        trocas = 0
        chamadas: list[str] = []
        texto = ""
        fechamento_forcado = False
        agente = self.estado.agente_ativo

        while True:
            resposta_modelo, chamadas_do_passo = self._rodar_papel_atual(chamadas)
            agente = self.estado.agente_ativo
            texto = _texto_da_mensagem(resposta_modelo)

            if self.estado.motivo_encerramento == MOTIVO_AUTENTICACAO_EXCEDIDA:
                # Decisão de segurança: não passa pelo modelo.
                texto = MENSAGEM_AUTENTICACAO_EXCEDIDA
                fechamento_forcado = True
                break

            pedido = self.estado.consumir_handoff()
            if pedido is None or trocas >= MAX_TROCAS_DE_PAPEL:
                break

            trocas += 1
            self.estado.agente_ativo = pedido.destino
            self.estado.mensagens.append(
                SystemMessage(nota_de_handoff(pedido.motivo, self._contexto_interno(completo=True)))
            )
            texto = ""  # a fala do papel anterior não é exibida: o cliente não vê a troca

        if not texto:
            texto = MENSAGEM_DESPEDIDA if self.estado.encerrado else MENSAGEM_CONTORNO

        return Resposta(
            texto=texto,
            agente=agente,
            encerrado=self.estado.encerrado,
            trocas_de_papel=trocas,
            ferramentas_chamadas=chamadas,
            fechamento_forcado=fechamento_forcado,
            campo_esperado=campo_esperado(self.estado),
        )

    def _rodar_papel_atual(self, chamadas: list[str]) -> tuple[AIMessage | None, list[str]]:
        """Executa o papel ativo até ele parar de pedir ferramentas."""
        especificacao = self._registro.obter(self.estado.agente_ativo)
        ferramentas = self._registro.ferramentas_de(self.estado.agente_ativo, self._ferramentas)
        try:
            modelo = self._modelo.bind_tools(ferramentas) if ferramentas else self._modelo
        except Exception as erro:  # noqa: BLE001 - provedor pode recusar o schema das ferramentas
            registrar_falha("bind_tools", erro)
            return AIMessage(content=""), []

        mensagens: list[BaseMessage] = [
            SystemMessage(self._prompt_do_papel(especificacao.prompt)),
            *self.estado.mensagens,
        ]
        if not any(isinstance(mensagem, HumanMessage) for mensagem in self.estado.mensagens):
            # Gemini recusa turno sem conteúdo de usuário; o primeiro não tem fala do cliente.
            mensagens.append(HumanMessage("[cliente iniciou o atendimento]"))

        resposta: AIMessage | None = None
        for _ in range(MAX_PASSOS_DE_FERRAMENTA):
            resposta = self._invocar(modelo, mensagens)
            self.estado.mensagens.append(resposta)
            if not resposta.tool_calls:
                break

            mensagens.append(resposta)
            for chamada in resposta.tool_calls:
                nome = str(chamada.get("name", ""))
                argumentos = chamada.get("args") or {}
                chamadas.append(nome)
                resultado = self._executar_ferramenta(nome, argumentos)
                mensagem_ferramenta = ToolMessage(
                    content=resultado,
                    tool_call_id=str(chamada.get("id") or nome),
                    name=nome,
                )
                self.estado.mensagens.append(mensagem_ferramenta)
                mensagens.append(mensagem_ferramenta)

            if self.estado.handoff or self.estado.encerrado:
                break

        return resposta, chamadas

    def _invocar(self, modelo: object, mensagens: list[BaseMessage]) -> AIMessage:
        """Chama o modelo tratando falha de provedor como erro de contorno."""
        try:
            resposta = modelo.invoke(mensagens)  # type: ignore[attr-defined]
        except Exception as erro:  # noqa: BLE001 - provedor fora do ar não pode derrubar a UI
            registrar_falha("llm", erro)
            return AIMessage(content="")
        if isinstance(resposta, AIMessage):
            return resposta
        return AIMessage(content=_texto_da_mensagem(resposta))  # type: ignore[arg-type]

    def _executar_ferramenta(self, nome: str, argumentos: dict[str, object]) -> str:
        # Guarda estrutural: só executa ferramenta do papel ATIVO — filtrar na oferta
        # não basta, o modelo pode emitir tool_call de outro papel.
        permitidas = self._registro.obter(self.estado.agente_ativo).ferramentas
        if nome not in permitidas:
            self.estado.registrar_trilha(f"ferramenta negada a {self.estado.agente_ativo}: {nome}")
            return (
                f"{FALHA_INTERNA}: ferramenta não disponível para o assunto atual. "
                "(não repita a tentativa; siga com o que você pode fazer)"
            )

        ferramenta = self._ferramentas.get(nome)
        if ferramenta is None:
            self.estado.registrar_trilha(f"ferramenta indisponível: {nome}")
            return f"{FALHA_INTERNA}: ferramenta não disponível. (não repita a tentativa)"

        try:
            return str(ferramenta.invoke(argumentos))
        except Exception as erro:  # noqa: BLE001 - argumento inválido vindo do modelo
            registrar_falha(f"ferramenta:{nome}", erro)
            return (
                f"{FALHA_INTERNA}: não foi possível executar a operação com esses dados. "
                "(peça a informação de outra forma)"
            )

    def _prompt_do_papel(self, prompt_do_papel: str) -> str:
        return f"{prompt_do_papel}\n\n{self._contexto_interno()}"

    def _contexto_interno(self, *, completo: bool = False) -> str:
        """Bloco de estado anexado ao prompt (o cliente não vê)."""
        linhas = ["[contexto interno] Estado atual do atendimento:"]
        if self.estado.autenticado and self.estado.cliente:
            cliente = self.estado.cliente
            linhas.append(
                f"- cliente autenticado: {cliente.primeiro_nome} "
                f"(limite {formatar_moeda(cliente.limite_credito)}, score {cliente.score}, "
                f"faixa {cliente.faixa})"
            )
        else:
            linhas.append("- nenhum cliente autenticado ainda")
            linhas.append(
                f"- tentativas de autenticação usadas: {self.estado.tentativas_autenticacao}"
            )
        if self.estado.pedido_pendente is not None:
            linhas.append(
                f"- pedido de aumento pendente de reanálise: "
                f"{formatar_moeda(self.estado.pedido_pendente)}"
            )
        if completo:
            linhas.append(
                "- você tem as ferramentas necessárias para agir; nunca invente dado que "
                "a ferramenta não devolveu"
            )
        return "\n".join(linhas)

    def _remover_nota_interna(self, texto: str) -> None:
        self.estado.mensagens = [
            mensagem
            for mensagem in self.estado.mensagens
            if not (isinstance(mensagem, SystemMessage) and mensagem.content == texto)
        ]

    def _resposta_final(self, texto: str) -> Resposta:
        return Resposta(
            texto=texto,
            agente=self.estado.agente_ativo,
            encerrado=True,
            campo_esperado=campo_esperado(self.estado),
        )
