"""Fluxos de conversa do atendimento (modelo roteirizado, sem LLM real)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from banco_agil.agents.especificacoes import (
    AGENTE_CAMBIO,
    AGENTE_CREDITO,
    AGENTE_ENTREVISTA,
    AGENTE_TRIAGEM,
)
from banco_agil.errors import BancoAgilError
from banco_agil.orchestrator.atendimento import Atendimento
from banco_agil.orchestrator.campos import CampoEsperado
from banco_agil.servicos import Servicos
from tests.conftest import CPF_MARIA, NASCIMENTO_MARIA
from tests.fakes import ScriptedChatModel, chamada, chamada_com_texto, texto

PALAVRAS_PROIBIDAS = ("transfer", "agente", "especialista", "setor", "handoff", "sistema interno")


def _sem_vazamento_interno(resposta_texto: str) -> None:
    """O cliente nunca vê a mecânica interna do atendimento."""
    minusculo = resposta_texto.lower()
    for palavra in PALAVRAS_PROIBIDAS:
        assert palavra not in minusculo, f"vazou termo interno: {palavra!r}"


class TestAbertura:
    def test_abertura_pede_o_cpf(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        atendimento = atendimento_factory(
            modelo_roteirizado(texto("Bom dia! Para começar, pode me informar o seu CPF?"))
        )

        resposta = atendimento.abrir()

        assert resposta.agente == AGENTE_TRIAGEM
        assert "CPF" in resposta.texto
        assert not resposta.encerrado
        _sem_vazamento_interno(resposta.texto)

    def test_abrir_duas_vezes_levanta(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        atendimento = atendimento_factory(modelo_roteirizado(texto("Olá!")))
        atendimento.abrir()
        with pytest.raises(BancoAgilError):
            atendimento.abrir()

    def test_mensagem_vazia_levanta(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        atendimento = atendimento_factory(modelo_roteirizado(texto("Olá!")))
        atendimento.abrir()
        with pytest.raises(BancoAgilError):
            atendimento.responder("   ")


class TestAutenticacaoEHandoff:
    def test_handoff_para_credito_e_invisivel(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada(
                "transferir_para", destino=AGENTE_CREDITO, motivo="cliente quer aumentar limite"
            ),
            texto("Certo, Maria! Seu limite hoje é de R$ 3.000,00. Quanto você gostaria?"),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("529.982.247-25 e 12/05/1990")

        assert resposta.trocas_de_papel == 1
        assert resposta.agente == AGENTE_CREDITO
        assert "3.000,00" in resposta.texto
        _sem_vazamento_interno(resposta.texto)
        assert atendimento.estado.autenticado
        assert atendimento.estado.cliente is not None
        assert atendimento.estado.cliente.primeiro_nome == "Maria"

    def test_o_cliente_nao_ve_a_fala_do_papel_anterior(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        """Se o papel que sai fala junto com o handoff, aquela fala é descartada."""
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada_com_texto(
                "Vou pedir para outro setor cuidar disso.",
                "transferir_para",
                destino=AGENTE_CREDITO,
                motivo="cliente quer tratar de limite",
            ),
            texto("Seu limite atual é de R$ 3.000,00."),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("é 52998224725, nasci em 12/05/1990")

        assert resposta.texto == "Seu limite atual é de R$ 3.000,00."
        assert resposta.agente == AGENTE_CREDITO
        _sem_vazamento_interno(resposta.texto)

    def test_fala_do_papel_anterior_nao_aparece_para_o_cliente(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("consultar_limite"),
            chamada("transferir_para", destino=AGENTE_CAMBIO, motivo="quer cotação do dólar"),
            texto("A cotação do dólar hoje é R$ 5,43."),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("quero saber o dólar")

        assert resposta.agente == AGENTE_CAMBIO
        assert "5,43" in resposta.texto

    def test_prompt_do_papel_ativo_traz_o_contexto_da_sessao(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino=AGENTE_CREDITO, motivo="limite"),
            texto("Ok!"),
        )
        atendimento = atendimento_factory(modelo)
        atendimento.responder("52998224725 / 12/05/1990")

        sistema_do_credito = modelo.ultimo_sistema
        assert "limites e crédito" in sistema_do_credito
        assert "Estado atual do atendimento" in sistema_do_credito
        assert "Maria" in sistema_do_credito
        assert "score 650" in sistema_do_credito


class TestFluxoDeCredito:
    def test_pedido_aprovado_grava_csv_e_informa_o_cliente(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
        servicos: Servicos,
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino=AGENTE_CREDITO, motivo="aumento de limite"),
            chamada("solicitar_aumento_de_limite", novo_limite="8000"),
            texto("Pronto, Maria: seu novo limite é de R$ 8.000,00."),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("quero aumentar meu limite para 8 mil")

        assert "8.000,00" in resposta.texto
        assert resposta.ferramentas_chamadas == [
            "autenticar_cliente",
            "transferir_para",
            "solicitar_aumento_de_limite",
        ]
        linhas = servicos.solicitacoes.ler_todos()
        assert len(linhas) == 1
        assert linhas[0]["status_pedido"] == "aprovado"
        assert servicos.credito.cliente(CPF_MARIA).limite_credito == pytest.approx(8000.0)

    def test_rejeicao_leva_a_entrevista_e_reanalise_aprova(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
        servicos: Servicos,
    ) -> None:
        """Fluxo completo do enunciado: rejeitado -> entrevista -> aprovado."""
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino=AGENTE_CREDITO, motivo="quer 20 mil de limite"),
            chamada("solicitar_aumento_de_limite", novo_limite="20000"),
            chamada("transferir_para", destino=AGENTE_ENTREVISTA, motivo="reajustar score"),
            texto("Seu score permite R$ 8.000,00. Posso atualizar seus dados para revisar isso?"),
            chamada(
                "registrar_entrevista",
                renda_mensal="8000",
                tipo_emprego="formal",
                despesas_fixas="1000",
                num_dependentes="0",
                tem_dividas="nao",
            ),
            chamada("transferir_para", destino=AGENTE_CREDITO, motivo="score atualizado"),
            texto("Atualizei seus dados. Vou refazer a análise do seu pedido de R$ 20.000,00."),
            chamada("solicitar_aumento_de_limite", novo_limite="20000"),
            texto("Aprovado, Maria! Seu novo limite é de R$ 20.000,00."),
        )
        atendimento = atendimento_factory(modelo)

        resposta_pedido = atendimento.responder("52998224725, 12/05/1990")
        assert resposta_pedido.agente == AGENTE_ENTREVISTA
        assert resposta_pedido.trocas_de_papel == 2
        assert "8.000,00" in resposta_pedido.texto

        resposta_entrevista = atendimento.responder("pode atualizar meus dados, vamos lá")
        assert resposta_entrevista.agente == AGENTE_CREDITO
        assert servicos.credito.cliente(CPF_MARIA).score == 1000

        resposta_final = atendimento.responder("e aí, aprovou?")

        assert resposta_final.agente == AGENTE_CREDITO
        assert "20.000,00" in resposta_final.texto
        _sem_vazamento_interno(resposta_final.texto)

        historico = servicos.credito.historico(CPF_MARIA)
        assert [solicitacao.status for solicitacao in historico] == ["aprovado", "rejeitado"]
        assert servicos.credito.cliente(CPF_MARIA).limite_credito == pytest.approx(20000.0)


class TestFluxoDeCambio:
    def test_apresenta_cotacao_e_encerra(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
        servicos: Servicos,
        cambio_falso: Callable[..., object],
    ) -> None:
        servicos.cambio = cambio_falso(valor=5.4321)  # type: ignore[assignment]
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino=AGENTE_CAMBIO, motivo="quer a cotação do dólar"),
            chamada("consultar_cotacao", moeda="dólar"),
            texto(
                "O dólar está em R$ 5,43 hoje (fonte: Banco Central). Precisa de mais alguma coisa?"
            ),
            chamada("encerrar_atendimento", motivo="assunto_concluido"),
            texto("Foi um prazer ajudar, Maria! Tenha um ótimo dia."),
        )
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("52998224725 12/05/1990")
        assert "5,43" in resposta.texto

        despedida = atendimento.responder("só isso, obrigado")

        assert despedida.encerrado
        assert "prazer" in despedida.texto
        assert servicos.cambio.chamadas == ["dólar"]  # type: ignore[attr-defined]

    def test_atendimento_encerrado_recusa_nova_mensagem(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("encerrar_atendimento", motivo="cliente_solicitou"),
            texto("Até logo!"),
        )
        atendimento = atendimento_factory(modelo)

        atendimento.responder("tchau")
        resposta = atendimento.responder("na verdade quero outra coisa")

        assert resposta.encerrado
        assert "encerrado" in resposta.texto.lower()


class TestCampoEsperadoNoFluxo:
    """O CPF que chega por texto livre move a interface mesmo sem a ferramenta.

    Verificado ao vivo: o modelo pode autenticar direto e nunca chamar `registrar_cpf`.
    A leitura determinística do CPF no texto garante que o campo dedicado avance.
    """

    def test_cpf_no_texto_livre_marca_o_campo(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(texto("Certo! Agora me diga a data de nascimento."))
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder("meu cpf é 529.982.247-25")

        assert atendimento.estado.cpf_informado == CPF_MARIA
        assert resposta.campo_esperado is CampoEsperado.DATA_NASCIMENTO

    @pytest.mark.parametrize(
        "mensagem",
        ["meu cpf é 111.111.111-11", "meu cpf é 52998224726", "nao tenho cpf"],
    )
    def test_cpf_invalido_no_texto_nao_marca_o_campo(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
        mensagem: str,
    ) -> None:
        modelo = modelo_roteirizado(texto("Pode repetir o CPF, por favor?"))
        atendimento = atendimento_factory(modelo)

        resposta = atendimento.responder(mensagem)

        assert atendimento.estado.cpf_informado is None
        assert resposta.campo_esperado is CampoEsperado.CPF

    def test_numero_maior_nao_e_lido_como_cpf(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        """Telefone (12 dígitos) na mesma frase não pode ser confundido com o CPF."""
        modelo = modelo_roteirizado(texto("Ok."))
        atendimento = atendimento_factory(modelo)

        atendimento.responder("meu telefone é 119876543210")

        assert atendimento.estado.cpf_informado is None


class TestRegistroDeAgentes:
    def test_cada_agente_recebe_apenas_suas_ferramentas(
        self,
        atendimento_factory: Callable[[ScriptedChatModel], Atendimento],
        modelo_roteirizado: Callable[..., ScriptedChatModel],
    ) -> None:
        modelo = modelo_roteirizado(
            chamada("autenticar_cliente", cpf=CPF_MARIA, data_nascimento=NASCIMENTO_MARIA),
            chamada("transferir_para", destino=AGENTE_CREDITO, motivo="limite"),
            texto("Ok!"),
        )
        atendimento = atendimento_factory(modelo)
        atendimento.responder("52998224725 12/05/1990")

        ferramentas_da_triagem, ferramentas_do_credito = modelo.ferramentas_oferecidas[:2]

        assert ferramentas_da_triagem == [
            "registrar_cpf",
            "autenticar_cliente",
            "transferir_para",
            "encerrar_atendimento",
        ]
        assert "solicitar_aumento_de_limite" in ferramentas_do_credito
        assert "autenticar_cliente" not in ferramentas_do_credito
        assert "consultar_cotacao" not in ferramentas_do_credito
