"""Guarda da própria suíte de evals: checagem que não pode falhar não é prova.

Três regressões reais, todas silenciosas até alguém ler o relatório com atenção:

1. ``ferramentas_do_papel`` era a UNIÃO das ferramentas de todos os papéis, então
   ``ferramentas_oferecidas_permitidas`` não tinha como acusar o vazamento de uma
   ferramenta de outro papel — a checagem existia e nunca podia falhar.
2. O caso ``g01`` de ``guardrails.yaml`` não tinha fala do cliente. Um passo
   ``modelo:`` só roteiriza a resposta DENTRO de um turno já aberto
   (``evals/runner.py``): sem cliente o roteiro nunca era consumido, o caso
   falhava e a guarda estrutural não era exercitada por nada.
3. O relatório gerado mandava rodar ``make eval`` — não existe Makefile no
   repositório.
"""

from __future__ import annotations

from banco_agil.agents.especificacoes import construir_registro
from banco_agil.orchestrator.estado import EstadoSessao
from evals import DIRETORIO_EVAL
from evals.casos import PassoCliente, PassoModelo, ler_casos
from evals.checks import Evidencia, _ferramentas_oferecidas_permitidas
from evals.runner import _fotografar_estado, executar_caso, montar_relatorio

GUARDRAILS = DIRETORIO_EVAL / "guardrails.yaml"


def _evidencia_do_papel(papel: str) -> Evidencia:
    registro = construir_registro()
    estado = EstadoSessao()
    estado.agente_ativo = papel
    evidencia = Evidencia()
    evidencia.estado = dict(_fotografar_estado(estado, registro))
    return evidencia


def test_ferramentas_do_papel_refletem_o_papel_ativo() -> None:
    registro = construir_registro()
    estado = EstadoSessao()
    uniao = sorted({nome for papel in registro.nomes for nome in registro.obter(papel).ferramentas})

    for papel in registro.nomes:
        estado.agente_ativo = papel
        snapshot = _fotografar_estado(estado, registro)
        assert snapshot["ferramentas_do_papel"] == sorted(registro.obter(papel).ferramentas)
        assert snapshot["ferramentas_do_papel"] != uniao, (
            "voltou a ser a união de todos os papéis: a checagem perde o dente"
        )


def test_checagem_reprova_ferramenta_oferecida_de_outro_papel() -> None:
    registro = construir_registro()
    legitima = list(registro.obter("triagem").ferramentas)

    limpa = _evidencia_do_papel("triagem")
    limpa.ferramentas_oferecidas = legitima
    assert _ferramentas_oferecidas_permitidas(limpa, True) is None

    vazada = _evidencia_do_papel("triagem")
    vazada.ferramentas_oferecidas = [*legitima, "consultar_limite"]
    motivo = _ferramentas_oferecidas_permitidas(vazada, True)
    assert motivo is not None
    assert "consultar_limite" in motivo


def test_todo_caso_roteirizado_dirige_o_roteiro_com_fala_do_cliente() -> None:
    casos = ler_casos(GUARDRAILS)
    assert casos
    for caso in casos:
        tem_roteiro = any(isinstance(passo, PassoModelo) for passo in caso.passos)
        tem_cliente = any(isinstance(passo, PassoCliente) for passo in caso.passos)
        if tem_roteiro:
            assert tem_cliente, (
                f"{caso.id}: sem passo de cliente o roteiro do modelo nunca roda e as "
                "checagens passam a não provar nada"
            )


def test_g01_exercita_a_guarda_estrutural() -> None:
    caso = next(c for c in ler_casos(GUARDRAILS) if c.id.startswith("g01_"))
    resultado = executar_caso(caso, sem_rede=True)

    assert resultado.situacao == "ok", resultado.falhas
    assert resultado.evidencia is not None
    evidencia = resultado.evidencia

    assert "consultar_limite" in evidencia.ferramentas_chamadas
    assert "ferramenta negada a triagem: consultar_limite" in evidencia.trilha
    devolvido = evidencia.resultados_ferramentas["consultar_limite"][0]
    assert devolvido.startswith("FALHA_INTERNA")
    # A triagem não pode nem receber a oferta da ferramenta de crédito.
    assert "consultar_limite" not in evidencia.ferramentas_oferecidas
    assert set(evidencia.ferramentas_oferecidas) <= set(evidencia.estado["ferramentas_do_papel"])


def test_relatorio_ensina_um_comando_que_existe() -> None:
    relatorio = montar_relatorio([], [])
    assert "evals.runner" in relatorio
    assert "make " not in relatorio
