"""Ferramentas que o modelo pode chamar — montadas por sessão.

Princípios:

1. **Estado por sessão, sem global.** Cada ferramenta é um *closure* sobre o
   ``EstadoSessao`` daquela conversa; duas sessões simultâneas na UI não se
   enxergam.
2. **Nenhuma decisão de negócio no prompt.** Aprovar limite, calcular score e
   autenticar são chamadas de domínio. A ferramenta devolve texto curto e o
   modelo apenas conversa.
3. **Falha nunca derruba o atendimento.** Toda ferramenta é embrulhada por
   ``_protegida``: exceção vira uma string ``FALHA_<TIPO>: ...`` que o prompt
   sabe interpretar — e uma linha de log técnico mascarado.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from banco_agil.agents.base import RegistroDeAgentes
from banco_agil.domain.modelos import formatar_moeda
from banco_agil.errors import (
    AuthenticationError,
    BancoAgilError,
    DataStoreError,
    ExternalAPIError,
    ValidationError,
)
from banco_agil.observability.log import registrar_falha
from banco_agil.orchestrator.estado import (
    MOTIVO_ASSUNTO_CONCLUIDO,
    MOTIVO_AUTENTICACAO_EXCEDIDA,
    MOTIVO_CLIENTE_PEDIU,
    EstadoSessao,
)
from banco_agil.servicos import Servicos
from banco_agil.tools.score import calcular_score
from banco_agil.tools.validators import mascarar_cpf, validar_cpf

# Nomes declarados aqui porque é este módulo que constrói as ferramentas.
F_AUTENTICAR = "autenticar_cliente"
F_REGISTRAR_CPF = "registrar_cpf"
F_TRANSFERIR = "transferir_para"
F_ENCERRAR = "encerrar_atendimento"
F_CONSULTAR_LIMITE = "consultar_limite"
F_SOLICITAR_AUMENTO = "solicitar_aumento_de_limite"
F_HISTORICO = "consultar_historico_de_solicitacoes"
F_REGISTRAR_ENTREVISTA = "registrar_entrevista"
F_CONSULTAR_COTACAO = "consultar_cotacao"

__all__ = [
    "construir_ferramentas",
    "FALHA_DADOS",
    "FALHA_AUTENTICACAO",
    "FALHA_INTERNA",
    "FALHA_EXTERNA",
    "FALHA_ESTADO",
]

FALHA_DADOS = "FALHA_DADOS"
FALHA_AUTENTICACAO = "FALHA_AUTENTICACAO"
FALHA_INTERNA = "FALHA_INTERNA"
FALHA_EXTERNA = "FALHA_EXTERNA"
FALHA_ESTADO = "FALHA_ESTADO"


class _SemArgumentos(BaseModel):
    """Ferramenta sem parâmetros (precisa de schema válido para o provedor)."""


class _ArgsAutenticar(BaseModel):
    cpf: str = Field(description="CPF informado pelo cliente (com ou sem pontuação).")
    data_nascimento: str = Field(description="Data de nascimento no formato dd/mm/aaaa.")


class _ArgsRegistrarCpf(BaseModel):
    cpf: str = Field(description="CPF informado pelo cliente (com ou sem pontuação).")


class _ArgsTransferir(BaseModel):
    destino: str = Field(
        description="Nome do destino, exatamente como aparece na lista de assuntos do prompt."
    )
    motivo: str = Field(description="Uma frase curta dizendo o que o cliente precisa.")


class _ArgsEncerrar(BaseModel):
    motivo: str = Field(
        default="cliente_solicitou",
        description="Motivo curto do encerramento (ex.: 'cliente_solicitou', 'assunto_concluido').",
    )


class _ArgsAumento(BaseModel):
    novo_limite: str = Field(description="Valor desejado em reais (ex.: '8000' ou '8.000,00').")


class _ArgsCotacao(BaseModel):
    moeda: str = Field(description="Moeda pedida pelo cliente (ex.: 'dólar', 'USD', 'euro').")


class _ArgsEntrevista(BaseModel):
    renda_mensal: str = Field(description="Renda mensal aproximada, em reais.")
    tipo_emprego: str = Field(description="formal, autonomo ou desempregado.")
    despesas_fixas: str = Field(description="Total de despesas fixas mensais, em reais.")
    num_dependentes: str = Field(description="Quantidade de dependentes (0, 1, 2, 3+).")
    tem_dividas: str = Field(description="'sim' ou 'nao' para dívidas ativas.")


def _protegida(nome: str, funcao: Callable[..., str]) -> Callable[..., str]:
    """Converte qualquer exceção em resposta que o prompt sabe interpretar."""

    def executar(**kwargs: Any) -> str:
        try:
            return funcao(**kwargs)
        except ValidationError as erro:
            return (
                f"{FALHA_DADOS}: {erro} "
                "(peça a informação novamente, sem culpar o cliente; a tentativa não foi consumida)"
            )
        except AuthenticationError as erro:
            return f"{FALHA_AUTENTICACAO}: {erro} (peça novamente com delicadeza)"
        except ExternalAPIError as erro:
            registrar_falha(nome, erro)
            return (
                f"{FALHA_EXTERNA}: consulta indisponível agora. "
                "(avise o cliente em linguagem simples e ofereça tentar "
                "novamente em instantes)"
            )
        except DataStoreError as erro:
            registrar_falha(nome, erro)
            return (
                f"{FALHA_INTERNA}: base de dados indisponível. "
                "(peça desculpas, não repita o detalhe técnico e ofereça tentar mais tarde)"
            )
        except BancoAgilError as erro:
            registrar_falha(nome, erro)
            return f"{FALHA_INTERNA}: operação não concluída. (peça desculpas e ofereça ajuda)"
        except Exception as erro:  # noqa: BLE001 - última barreira: nada derruba a conversa
            registrar_falha(nome, erro)
            return (
                f"{FALHA_INTERNA}: erro inesperado. "
                "(peça desculpas ao cliente e ofereça tentar novamente)"
            )

    return executar


def _exigir_autenticacao(estado: EstadoSessao) -> Any:
    """Guarda estrutural: ferramenta de negócio exige cliente autenticado."""
    if not estado.autenticado or estado.cliente is None:
        raise ValidationError(
            "o cliente ainda não está autenticado; peça CPF e data de nascimento primeiro"
        )
    return estado.cliente


def construir_ferramentas(
    estado: EstadoSessao,
    servicos: Servicos,
    registro: RegistroDeAgentes,
) -> dict[str, BaseTool]:
    """Monta o conjunto de ferramentas desta sessão."""
    max_tentativas = servicos.settings.max_tentativas_autenticacao

    def registrar_cpf(cpf: str) -> str:
        """Guarda o CPF assim que ele chega — é o que habilita o campo dedicado na UI.

        Valida o dígito verificador aqui (Python puro). CPF com dígito inválido
        levanta ``ValidationError`` e **não** consome tentativa de autenticação.
        """
        normalizado = validar_cpf(cpf)
        estado.cpf_informado = normalizado
        estado.registrar_trilha(f"cpf registrado {mascarar_cpf(normalizado)}")
        return (
            "OK: CPF válido e registrado. Peça agora a data de nascimento (dd/mm/aaaa) "
            "e, em seguida, chame autenticar_cliente com o CPF e a data."
        )

    def autenticar_cliente(cpf: str, data_nascimento: str) -> str:
        estado.registrar_trilha(f"autenticacao(cpf={mascarar_cpf(cpf)})")
        # Registro defensivo: o modelo pode autenticar sem ter chamado registrar_cpf.
        with contextlib.suppress(ValidationError):
            estado.cpf_informado = validar_cpf(cpf)
        try:
            cliente = servicos.autenticacao.autenticar(cpf, data_nascimento)
        except AuthenticationError:
            estado.tentativas_autenticacao += 1
            restantes = max(0, max_tentativas - estado.tentativas_autenticacao)
            if restantes == 0:
                estado.encerrar(MOTIVO_AUTENTICACAO_EXCEDIDA)
                return (
                    f"{FALHA_AUTENTICACAO}: dados não conferem e as {max_tentativas} tentativas "
                    "se esgotaram. (encerre o atendimento de forma educada, como orientado)"
                )
            return (
                f"{FALHA_AUTENTICACAO}: CPF e data de nascimento não conferem "
                f"(tentativa {estado.tentativas_autenticacao} de {max_tentativas}; "
                f"restam {restantes}). (peça novamente com delicadeza)"
            )

        estado.cliente = cliente
        estado.autenticado = True
        estado.registrar_trilha(f"autenticado {cliente.primeiro_nome}")
        return (
            f"OK: cliente autenticado. primeiro_nome={cliente.primeiro_nome}; "
            f"limite_atual={formatar_moeda(cliente.limite_credito)}; score={cliente.score}. "
            "Pergunte o que ele precisa e encaminhe pelo assunto."
        )

    def transferir_para(destino: str, motivo: str) -> str:
        alvo = str(destino or "").strip().lower()
        if alvo not in registro:
            raise ValidationError(
                f"destino {destino!r} não existe; use um destes: {', '.join(registro.nomes)}"
            )
        if alvo == estado.agente_ativo:
            return (
                f"OK: você já é o responsável por {alvo}; siga o atendimento sem trocar de assunto."
            )
        estado.pedir_handoff(alvo, motivo)
        estado.registrar_trilha(f"{estado.agente_ativo} -> {alvo}: {motivo}")
        return (
            "OK: quem conduz a conversa agora é você mesmo, no novo assunto. Não escreva despedida."
        )

    def encerrar_atendimento(motivo: str = "cliente_solicitou") -> str:
        mapa = {
            "cliente_solicitou": MOTIVO_CLIENTE_PEDIU,
            "assunto_concluido": MOTIVO_ASSUNTO_CONCLUIDO,
        }
        estado.encerrar(mapa.get(str(motivo).strip().lower(), str(motivo) or MOTIVO_CLIENTE_PEDIU))
        return "OK: atendimento encerrado. Escreva uma despedida curta e cordial."

    def consultar_limite() -> str:
        cliente = _exigir_autenticacao(estado)
        atual = servicos.credito.cliente(cliente.cpf)
        estado.cliente = atual
        maximo = servicos.credito.limite_maximo_para(atual.score)
        estado.registrar_trilha(f"consulta de limite: {atual.limite_credito:.2f}")
        return (
            f"OK: limite_atual={formatar_moeda(atual.limite_credito)}; score={atual.score} "
            f"(faixa {atual.faixa}); maior limite permitido hoje={formatar_moeda(maximo)}."
        )

    def solicitar_aumento_de_limite(novo_limite: str) -> str:
        cliente = _exigir_autenticacao(estado)
        solicitacao = servicos.credito.solicitar_aumento(cliente.cpf, novo_limite)
        estado.ultima_solicitacao = solicitacao
        estado.registrar_trilha(
            f"solicitacao {solicitacao.novo_limite:.2f} -> {solicitacao.status}"
        )

        if solicitacao.aprovada:
            estado.pedido_pendente = None
            estado.cliente = servicos.credito.cliente(cliente.cpf)
            return (
                f"OK: APROVADO. novo_limite={formatar_moeda(solicitacao.novo_limite)} "
                f"(anterior {formatar_moeda(solicitacao.limite_atual)}). "
                "Informe o cliente com objetividade."
            )

        estado.pedido_pendente = solicitacao.novo_limite
        permitido = servicos.credito.limite_maximo_para(solicitacao.score_no_pedido)
        return (
            f"OK: REJEITADO. Pedido de {formatar_moeda(solicitacao.novo_limite)} recusado; "
            f"score {solicitacao.score_no_pedido} permite até {formatar_moeda(permitido)}. "
            "(explique com transparência e ofereça atualizar os dados para "
            "tentar reajustar o score)"
        )

    def consultar_historico_de_solicitacoes() -> str:
        cliente = _exigir_autenticacao(estado)
        historico = servicos.credito.historico(cliente.cpf)
        if not historico:
            return "OK: o cliente não tem nenhuma solicitação de aumento registrada."
        linhas = [
            f"{registro_item.data_hora} | {formatar_moeda(registro_item.novo_limite)} "
            f"| {registro_item.status} | {registro_item.motivo}"
            for registro_item in historico[:5]
        ]
        return "OK: últimas solicitações (mais recente primeiro):\n" + "\n".join(linhas)

    def registrar_entrevista(
        renda_mensal: str,
        tipo_emprego: str,
        despesas_fixas: str,
        num_dependentes: str,
        tem_dividas: str,
    ) -> str:
        cliente = _exigir_autenticacao(estado)
        score_anterior = cliente.score

        novo_score = calcular_score(
            renda_mensal=renda_mensal,
            tipo_emprego=tipo_emprego,
            despesas_fixas=despesas_fixas,
            num_dependentes=num_dependentes,
            tem_dividas=tem_dividas,
        )

        estado.dados_entrevista = {
            "renda_mensal": renda_mensal,
            "tipo_emprego": tipo_emprego,
            "despesas_fixas": despesas_fixas,
            "num_dependentes": num_dependentes,
            "tem_dividas": tem_dividas,
        }
        servicos.clientes.atualizar(cliente.cpf, score=str(novo_score))
        atualizado = servicos.autenticacao.recarregar(cliente.cpf)
        estado.cliente = atualizado
        estado.registrar_trilha(f"score {score_anterior} -> {novo_score}")

        maximo = servicos.credito.limite_maximo_para(novo_score)
        pendente = (
            f" O cliente tem um pedido pendente de {formatar_moeda(estado.pedido_pendente)}."
            if estado.pedido_pendente
            else ""
        )
        return (
            f"OK: score atualizado de {score_anterior} para {novo_score}"
            f" (faixa {atualizado.faixa}); limite máximo permitido agora="
            f"{formatar_moeda(maximo)}.{pendente} "
            "Informe o cliente e volte para o atendimento de crédito."
        )

    def consultar_cotacao(moeda: str) -> str:
        cotacao = servicos.cambio.cotacao(moeda)
        estado.registrar_trilha(f"cotacao {cotacao.moeda}={cotacao.valor}")
        return (
            f"OK: {cotacao.nome} ({cotacao.moeda}) = {cotacao.valor_formatado()}; "
            f"fonte={cotacao.fonte}; atualizado_em={cotacao.atualizado_em or 'não informado'}. "
            "Apresente o valor ao cliente."
        )

    def _ferramenta(
        nome: str,
        descricao: str,
        funcao: Callable[..., str],
        schema: type[BaseModel],
    ) -> BaseTool:
        return StructuredTool.from_function(
            func=_protegida(nome, funcao),
            name=nome,
            description=descricao,
            args_schema=schema,
        )

    return {
        F_REGISTRAR_CPF: _ferramenta(
            F_REGISTRAR_CPF,
            "Registra o CPF informado pelo cliente e confere o dígito verificador. "
            "Chame assim que o cliente informar o CPF, antes de pedir a data de nascimento.",
            registrar_cpf,
            _ArgsRegistrarCpf,
        ),
        F_AUTENTICAR: _ferramenta(
            F_AUTENTICAR,
            "Valida CPF e data de nascimento do cliente na base. "
            "Chame só quando tiver os dois dados.",
            autenticar_cliente,
            _ArgsAutenticar,
        ),
        F_TRANSFERIR: _ferramenta(
            F_TRANSFERIR,
            "Encaminha o atendimento para o assunto adequado. Destinos: "
            + "; ".join(f"{nome} = {registro.obter(nome).descricao}" for nome in registro.nomes)
            + ". Não escreva despedida depois de chamar.",
            transferir_para,
            _ArgsTransferir,
        ),
        F_ENCERRAR: _ferramenta(
            F_ENCERRAR,
            "Encerra o atendimento. Use quando o cliente der o assunto por "
            "concluído ou pedir para encerrar.",
            encerrar_atendimento,
            _ArgsEncerrar,
        ),
        F_CONSULTAR_LIMITE: _ferramenta(
            F_CONSULTAR_LIMITE,
            "Devolve o limite atual, o score e o maior limite permitido "
            "para o cliente autenticado.",
            consultar_limite,
            _SemArgumentos,
        ),
        F_SOLICITAR_AUMENTO: _ferramenta(
            F_SOLICITAR_AUMENTO,
            "Registra o pedido de aumento de limite e devolve aprovado ou "
            "rejeitado conforme o score.",
            solicitar_aumento_de_limite,
            _ArgsAumento,
        ),
        F_HISTORICO: _ferramenta(
            F_HISTORICO,
            "Lista as solicitações de aumento de limite já feitas pelo cliente autenticado.",
            consultar_historico_de_solicitacoes,
            _SemArgumentos,
        ),
        F_REGISTRAR_ENTREVISTA: _ferramenta(
            F_REGISTRAR_ENTREVISTA,
            "Grava os cinco dados da entrevista, recalcula o score do cliente "
            "e devolve o novo valor.",
            registrar_entrevista,
            _ArgsEntrevista,
        ),
        F_CONSULTAR_COTACAO: _ferramenta(
            F_CONSULTAR_COTACAO,
            "Consulta a cotação atual de uma moeda em relação ao real (BRL).",
            consultar_cotacao,
            _ArgsCotacao,
        ),
    }
