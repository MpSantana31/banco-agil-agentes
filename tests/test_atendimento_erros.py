"""Tratamento de erro e limites do laço de conversa.

Requisito do enunciado: "cenários de erro devem ser tratados sem quebrar a
conversa". Aqui cada modo de falha tem um teste.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from langchain_core.messages import ToolMessage

from banco_agil.agents.especificacoes import AGENTE_CREDITO, AGENTE_TRIAGEM
from banco_agil.errors import ExternalAPIError
from banco_agil.orchestrator.atendimento import (
    MAX_PASSOS_DE_FERRAMENTA,
    MENSAGEM_AUTENTICACAO_EXCEDIDA,
    MENSAGEM_CONTORNO,
    Atendimento,
)
from banco_agil.orchestrator.estado import MOTIVO_AUTENTICACAO_EXCEDIDA
from banco_agil.servicos import Servicos
from tests.conftest import CPF_MARIA, NASCIMENTO_MARIA
from tests.fakes import ScriptedChatModel, chamada, texto


def _conteudo_das_ferramentas(atendimento: Atendimento) -> list[str]:
    return [
        str(mensagem.content)
        for mensagem in atendimento.estado.mensagens
        if isinstance(mensagem, ToolMessage)
    ]


class TestAutenticacaoComFalha:
    def test_cpf_valido_com_nascimento_errado_consome_tentativa(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento="01/01/1980"),
            texto("Não consegui confirmar seus dados. Pode repetir a data de nascimento?"),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("529.982.247-25")

        assert atendimento.estado.tentativas_autenticacao == 1
        assert "tentativa 1 de 3" in _conteudo_das_ferramentas(atendimento)[0]
        assert not resposta.encerrado
        assert "FALHA" not in resposta.texto  # o cliente não vê o código interno

    def test_dado_invalido_nao_consome_tentativa(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf="123", data_nascimento="12/05/1990"),
            texto("Esse CPF parece incompleto. Pode me passar os 11 dígitos?"),
        )
        atendimento = atendimento_factory(modelo)

        atendimento.responder("meu cpf é 123")

        assert atendimento.estado.tentativas_autenticacao == 0
        assert "FALHA_DADOS" in _conteudo_das_ferramentas(atendimento)[0]

    def test_tres_falhas_encerram_com_mensagem_fixa(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        """Segurança não depende do modelo: a 3ª falha encerra com texto fixo."""
        tentativa = chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento="01/01/1980")
        modelo = modelo_roteirizado(
            tentativa,
            texto("Tente novamente, por favor."),
            tentativa,
            texto("Tente novamente, por favor."),
            tentativa,
            texto("Vou seguir com o atendimento mesmo assim!"),  # ignorada de propósito
        )
        atendimento = atendimento_factory(modelo)

        atendimento.responder("52998224725")
        atendimento.responder("52998224725")
        terceira = atendimento.responder("52998224725")

        assert atendimento.estado.tentativas_autenticacao == 3
        assert terceira.encerrado
        assert terceira.fechamento_forcado
        assert terceira.texto == MENSAGEM_AUTENTICACAO_EXCEDIDA
        assert atendimento.estado.motivo_encerramento == MOTIVO_AUTENTICACAO_EXCEDIDA
        assert not atendimento.estado.autenticado

    def test_atendimento_encerrado_por_seguranca_nao_reabre(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        tentativa = chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento="01/01/1980")
        modelo = modelo_roteirizado(tentativa, texto("ok"), tentativa, texto("ok"), tentativa)
        atendimento = atendimento_factory(modelo)

        for _ in range(3):
            atendimento.responder("52998224725")
        depois = atendimento.responder("na verdade meu cpf é 52998224725")

        assert depois.encerrado
        assert "encerrado" in depois.texto.lower()


class TestFerramentasForaDoEscopo:
    def test_ferramenta_de_outro_papel_nao_executa(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        """O modelo pediu uma ferramenta que este papel não tem: nada acontece."""
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino=AGENTE_CREDITO, motivo="limite"),
            chamada("consultar_cotacao", moeda="dólar"),  # não existe no papel de crédito
            texto("Não consigo ver isso por aqui."),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("52998224725 12/05/1990")

        assert resposta.texto == "Não consigo ver isso por aqui."
        assert any(
            "ferramenta não disponível" in conteudo
            for conteudo in _conteudo_das_ferramentas(atendimento)
        )

    def test_destino_de_handoff_inexistente_nao_troca_nada(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino="investimentos", motivo="quer investir"),
            texto("Esse assunto não é comigo."),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("52998224725 12/05/1990")

        assert resposta.agente == AGENTE_TRIAGEM
        assert any("FALHA_DADOS" in conteudo for conteudo in _conteudo_das_ferramentas(atendimento))

    def test_argumento_invalido_na_ferramenta_nao_derruba(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA),  # falta data_nascimento
            texto("Pode repetir a data de nascimento?"),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("é 52998224725")

        assert "repetir" in resposta.texto
        assert any(
            "FALHA_INTERNA" in conteudo for conteudo in _conteudo_das_ferramentas(atendimento)
        )

    def test_excecao_inesperada_na_ferramenta_vira_falha_controlada(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
        servicos: Servicos,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explodir(*_: object, **__: object) -> None:
            raise RuntimeError("falha catastrófica simulada")

        monkeypatch.setattr(servicos.credito, "solicitar_aumento", explodir)
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino=AGENTE_CREDITO, motivo="limite"),
            chamada("solicitar_aumento_de_limite", novo_limite="8000"),
            texto("Desculpe, tive um problema. Pode tentar de novo em instantes?"),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("52998224725 12/05/1990")

        assert "Desculpe" in resposta.texto
        assert any(
            "FALHA_INTERNA" in conteudo for conteudo in _conteudo_das_ferramentas(atendimento)
        )


class TestFalhaDoProvedorExterno:
    def test_cambio_fora_do_ar_nao_derruba_o_atendimento(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
        servicos: Servicos,
        cambio_falso: Callable[..., object],
    ) -> None:
        servicos.cambio = cambio_falso(falha=ExternalAPIError("todos os provedores falharam"))  # type: ignore[assignment]
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino="cambio", motivo="quer dólar"),
            chamada("consultar_cotacao", moeda="dólar"),
            texto("Não consegui consultar a cotação agora. Tento de novo em instantes?"),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("52998224725 12/05/1990")

        assert "Não consegui" in resposta.texto
        assert any(
            "FALHA_EXTERNA" in conteudo for conteudo in _conteudo_das_ferramentas(atendimento)
        )

    def test_resposta_vazia_do_modelo_vira_mensagem_de_contorno(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        atendimento = atendimento_factory(modelo_roteirizado())  # roteiro vazio

        resposta = atendimento.responder("bom dia")

        assert resposta.texto == MENSAGEM_CONTORNO


class TestLimitesDoLaco:
    def test_pedidos_de_ferramenta_em_excesso_sao_limitados(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        pedido = chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA)
        modelo = modelo_roteirizado(*[pedido for _ in range(10)])
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("52998224725 12/05/1990")

        assert len(resposta.ferramentas_chamadas) <= MAX_PASSOS_DE_FERRAMENTA
        assert resposta.texto == MENSAGEM_CONTORNO

    def test_numero_de_trocas_de_papel_respeita_o_teto(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        """Um ciclo de trocas não pode virar laço infinito."""
        modelo = modelo_roteirizado(
            *[chamada("transferir_para", destino=AGENTE_CREDITO, motivo="ciclo") for _ in range(6)],
            texto("Vamos ao que interessa."),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("quero falar de limite")

        assert resposta.trocas_de_papel <= 3


class TestConsistenciaDoHistorico:
    def test_todo_pedido_de_ferramenta_tem_resposta_correspondente(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        """A API de chat exige um ToolMessage para cada tool_call do turno."""
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino=AGENTE_CREDITO, motivo="limite"),
            chamada("consultar_limite"),
            texto("Seu limite é R$ 3.000,00."),
        )
        atendimento = atendimento_factory(modelo)
        atendimento.responder("52998224725 12/05/1990")

        pendentes: set[str] = set()
        for mensagem in atendimento.estado.mensagens:
            for chamada_feita in getattr(mensagem, "tool_calls", []) or []:
                pendentes.add(str(chamada_feita["id"]))
            if isinstance(mensagem, ToolMessage):
                pendentes.discard(str(mensagem.tool_call_id))
        assert pendentes == set()

    def test_trilha_interna_registra_a_decisao(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino=AGENTE_CREDITO, motivo="quer 8 mil"),
            chamada("solicitar_aumento_de_limite", novo_limite="8000"),
            texto("Pronto!"),
        )
        atendimento = atendimento_factory(modelo)
        atendimento.responder("52998224725 12/05/1990")

        trilha = " | ".join(atendimento.estado.trilha)
        assert "autenticado Maria" in trilha
        assert "solicitacao 8000.00 -> aprovado" in trilha
