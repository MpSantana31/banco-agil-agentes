"""Runner de avaliação: roda os casos contra um ou mais modelos e monta o relatório.

Dois tipos de caso, com naturezas diferentes:

- **guardrail** (``guardrails.yaml``): offline, com modelo roteirizado. Testa o
  *arcabouço* — o que acontece quando o modelo obedece a uma injeção, chama a
  ferramenta de outro papel, inventa um limite ou erra a autenticação três vezes.
  Não depende de provedor: roda em ``python -m evals.runner --offline`` (``make
  eval-offline``), sem token — e o pytest cobre o caso ``g01`` para proteger a
  própria checagem.
- **qualidade** (``golden.yaml``): ao vivo, contra modelos reais. Compara
  fornecedores na mesma conversa e no mesmo gabarito de checagens.

O ponto que costuma passar batido nos relatórios de avaliação de agente: falha de
**infraestrutura** (quota, ``429``, timeout) é reportada separada de falha de
**qualidade**. Sem isso, um dia de quota estourada aparece como "modelo ruim".

Uso:
    uv run python -m evals.runner --lista
    uv run python -m evals.runner --offline
    uv run python -m evals.runner --modelos gemini-flash
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import shutil
import sys
import tempfile
import time
import traceback
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from banco_agil.config import Settings
from banco_agil.domain.modelos import Cotacao
from banco_agil.llm.corrente import CorrenteDeModelos, PassoDaCadeia
from banco_agil.llm.factory import construir_cadeia
from banco_agil.orchestrator.atendimento import Atendimento
from banco_agil.servicos import construir_servicos
from evals import DIRETORIO_EVAL, RAIZ
from evals.casos import (
    ARQUIVO_GOLDEN,
    ARQUIVO_GUARDRAILS,
    Caso,
    Modelo,
    PassoAbertura,
    PassoCliente,
    PassoModelo,
    ler_casos,
    ler_modelos,
)
from evals.checks import Evidencia, FalhaDeChecagem, Turno, executar_checagens

__all__ = ["ResultadoCaso", "executar_caso", "rodar", "main"]

DIRETORIO_SAIDA = DIRETORIO_EVAL / "resultados"
SITUACOES = ("ok", "falha", "infra")


@dataclass(slots=True)
class ResultadoCaso:
    """O que um caso produziu para um modelo."""

    caso_id: str
    tipo: str
    requisito: str
    descricao: str
    modelo: str
    rodada: int
    situacao: str
    segundos: float
    chamadas: int
    tokens_entrada: int | None
    tokens_saida: int | None
    falhas: list[FalhaDeChecagem] = field(default_factory=list)
    infra: list[str] = field(default_factory=list)
    erro_fatal: str | None = None
    evidencia: Evidencia | None = None


class _CapturaDeAvisos(logging.Handler):
    """Coleta o que o sistema registra em nível de aviso durante o caso.

    O orquestrador não deixa a falha de provedor escapar: ela vira mensagem de
    contorno e uma linha de log. Sem ler o log, o runner não teria como separar
    "o modelo respondeu mal" de "o provedor estourou a quota".
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.avisos: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.avisos.append(str(record.getMessage()))


class _CambioFalso:
    """Substituto do serviço de câmbio: mantém os casos offline e determinísticos."""

    def __init__(self, valor: float = 5.43, moeda: str = "USD") -> None:
        self._cotacao = Cotacao(
            moeda=moeda,
            nome="Dólar americano",
            valor=valor,
            atualizado_em="2026-09-14 12:00:00",
            fonte="AwesomeAPI (Banco Central)",
        )

    def cotacao(self, moeda: object) -> Cotacao:
        return self._cotacao


def _settings_de_dados(destino: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=destino / "data",
        log_dir=destino / "logs",
        llm_provider="fake",
        fx_cache_ttl_segundos=0,
    )


def _semear_dados(destino: Path) -> None:
    """Copia as bases de exemplo; ``solicitacoes`` começa vazia de propósito.

    Assim o gabarito pode exigir "exatamente N solicitações gravadas" sem herdar
    linhas de execuções anteriores.
    """
    origem = RAIZ / "data"
    destino.mkdir(parents=True, exist_ok=True)
    for nome in ("clientes.csv", "score_limite.csv"):
        shutil.copy(origem / nome, destino / nome)
    (destino / "solicitacoes_aumento_limite.csv").write_text(
        "cpf_cliente,data_hora_solicitacao,limite_atual,novo_limite_solicitado,"
        "status_pedido,score_no_pedido,motivo\n",
        encoding="utf-8",
    )


def _montar_servicos(caso: Caso, destino: Path, *, sem_rede: bool):
    settings = _settings_de_dados(destino)
    _semear_dados(settings.data_dir)
    servicos = construir_servicos(settings)
    if sem_rede or caso.tipo == "guardrail" or not caso.precisa_de_rede:
        servicos.cambio = _CambioFalso()  # type: ignore[assignment]
    return settings, servicos


def _mensagem_roteirizada(passo: PassoModelo) -> AIMessage:
    if passo.ferramenta:
        return AIMessage(
            content=passo.texto,
            tool_calls=[
                {
                    "name": passo.ferramenta,
                    "args": dict(passo.argumentos),
                    "id": f"call_{passo.ferramenta}",
                    "type": "tool_call",
                }
            ],
        )
    return AIMessage(content=passo.texto)


def _modelo_roteirizado(caso: Caso) -> Any:
    from tests.fakes import ScriptedChatModel

    return ScriptedChatModel(
        roteiro=[
            _mensagem_roteirizada(passo) for passo in caso.passos if isinstance(passo, PassoModelo)
        ]
    )


def _modelo_ao_vivo(modelo: Modelo, settings_llm: Settings) -> Any:
    return construir_cadeia(
        settings_llm,
        passos=[PassoDaCadeia.de_texto(passo) for passo in modelo.passos],
    )


def _prompts_enviados(modelo: object) -> list[str]:
    historicos = getattr(modelo, "historicos", None)
    if not historicos:
        return []
    vistos: set[str] = set()
    prompts: list[str] = []
    for mensagens in historicos:
        for mensagem in mensagens:
            conteudo = str(getattr(mensagem, "content", ""))
            if getattr(mensagem, "type", "") == "system" and conteudo not in vistos:
                vistos.add(conteudo)
                prompts.append(conteudo)
    return prompts


def _ferramentas_oferecidas(modelo: object) -> list[str]:
    oferecidas = getattr(modelo, "ferramentas_oferecidas", None)
    if not oferecidas:
        return []
    return sorted({nome for rodada in oferecidas for nome in rodada})


def _saidas_de_ferramenta(mensagens: Sequence[BaseMessage]) -> dict[str, list[str]]:
    nomes_por_id: dict[str, str] = {}
    saidas: dict[str, list[str]] = {}
    for mensagem in mensagens:
        if isinstance(mensagem, AIMessage):
            for chamada in mensagem.tool_calls or []:
                identificador = str(chamada.get("id") or chamada.get("name") or "")
                nomes_por_id[identificador] = str(chamada.get("name") or "")
        elif isinstance(mensagem, ToolMessage):
            nome = str(mensagem.name or nomes_por_id.get(mensagem.tool_call_id, ""))
            saidas.setdefault(nome, []).append(str(mensagem.content))
    return saidas


def _consumo(mensagens: Sequence[BaseMessage]) -> tuple[int, int | None, int | None]:
    """Chamadas ao modelo e consumo de tokens (quando o provedor informa)."""
    chamadas = 0
    entrada = 0
    saida = 0
    informou = False
    for mensagem in mensagens:
        if not isinstance(mensagem, AIMessage):
            continue
        chamadas += 1
        uso = getattr(mensagem, "usage_metadata", None) or {}
        if uso:
            informou = True
            entrada += int(uso.get("input_tokens") or 0)
            saida += int(uso.get("output_tokens") or 0)
    return chamadas, (entrada if informou else None), (saida if informou else None)


def _linhas_csv(caminho: Path) -> list[dict[str, str]]:
    if not caminho.exists():
        return []
    with caminho.open(encoding="utf-8", newline="") as arquivo:
        return [dict(linha) for linha in csv.DictReader(arquivo)]


def _fotografar_estado(estado: Any, registro: Any) -> dict[str, Any]:
    cliente = estado.cliente
    # Ferramentas do papel ATIVO: a checagem compara a oferta com esta lista, e a
    # união de todos os papéis nunca acusaria vazamento de ferramenta de outro papel.
    permitidas: set[str] = set(registro.obter(estado.agente_ativo).ferramentas)
    return {
        "autenticado": estado.autenticado,
        "encerrado": estado.encerrado,
        "motivo_encerramento": estado.motivo_encerramento,
        "agente_ativo": estado.agente_ativo,
        "tentativas_autenticacao": estado.tentativas_autenticacao,
        "cpf_informado": bool(estado.cpf_informado),
        "pedido_pendente": estado.pedido_pendente,
        "limite_credito": float(cliente.limite_credito) if cliente else None,
        "score": cliente.score if cliente else None,
        "campo_esperado": None,
        "ferramentas_do_papel": sorted(permitidas),
    }


def executar_caso(
    caso: Caso,
    *,
    modelo: Any | None = None,
    rotulo: str = "roteirizado",
    settings_llm: Settings | None = None,
    sem_rede: bool = False,
    rodada: int = 1,
) -> ResultadoCaso:
    """Roda um caso do começo ao fim e devolve a evidência + checagens."""
    modelo = modelo if modelo is not None else _modelo_roteirizado(caso)
    falhas_antes = len(getattr(modelo, "falhas", []) or [])

    captura = _CapturaDeAvisos()
    logger = logging.getLogger("banco_agil")
    logger.addHandler(captura)
    destino = Path(tempfile.mkdtemp(prefix=f"banco-agil-eval-{caso.id}-"))
    turnos: list[Turno] = []
    evidencia = Evidencia()
    erro_fatal: str | None = None
    inicio = time.perf_counter()
    estado = None
    try:
        _, servicos = _montar_servicos(caso, destino, sem_rede=sem_rede)
        atendimento = Atendimento(modelo, servicos)
        estado = atendimento.estado

        for passo in caso.passos:
            if isinstance(passo, PassoAbertura):
                entrada = "[abertura]"
            elif isinstance(passo, PassoCliente):
                entrada = passo.texto
            else:
                continue  # passo do modelo: o roteiro já respondeu

            marco = time.perf_counter()
            try:
                resposta = (
                    atendimento.abrir()
                    if isinstance(passo, PassoAbertura)
                    else atendimento.responder(passo.texto)
                )
            except Exception as erro:  # noqa: BLE001 - o caso termina, mas fica registrado
                erro_fatal = f"{type(erro).__name__}: {erro}"
                turnos.append(
                    Turno(
                        entrada=entrada,
                        texto="",
                        agente=estado.agente_ativo,
                        segundos=time.perf_counter() - marco,
                        erro=erro_fatal,
                    )
                )
                break

            turnos.append(
                Turno(
                    entrada=entrada,
                    texto=resposta.texto,
                    agente=resposta.agente,
                    encerrado=resposta.encerrado,
                    trocas_de_papel=resposta.trocas_de_papel,
                    ferramentas_chamadas=list(resposta.ferramentas_chamadas),
                    campo_esperado=(
                        str(resposta.campo_esperado) if resposta.campo_esperado else None
                    ),
                    fechamento_forcado=resposta.fechamento_forcado,
                    segundos=time.perf_counter() - marco,
                )
            )

        chamadas, tokens_entrada, tokens_saida = _consumo(estado.mensagens)
        evidencia = Evidencia(
            turnos=turnos,
            estado=_fotografar_estado(estado, atendimento.registro),
            trilha=list(estado.trilha),
            resultados_ferramentas=_saidas_de_ferramenta(estado.mensagens),
            ferramentas_oferecidas=_ferramentas_oferecidas(modelo),
            prompts=_prompts_enviados(modelo),
            solicitacoes_novas=_linhas_csv(servicos.settings.caminho_solicitacoes),
            score_no_csv={
                linha["cpf"]: linha.get("score", "")
                for linha in _linhas_csv(servicos.settings.caminho_clientes)
            },
            falhas_de_provedor=[
                aviso for aviso in captura.avisos if aviso.startswith(("llm |", "bind_tools |"))
            ],
        )
        evidencia.excecoes = [erro_fatal] if erro_fatal else []
        if isinstance(modelo, CorrenteDeModelos):
            evidencia.falhas_de_provedor.extend(
                f"{passo.rotulo} | {erro}" for passo, erro in modelo.falhas[falhas_antes:]
            )
    except Exception as erro:  # noqa: BLE001 - o caso falhou antes de produzir evidência
        erro_fatal = f"{type(erro).__name__}: {erro}"
        evidencia.excecoes = [erro_fatal, traceback.format_exc(limit=3)]
    finally:
        logger.removeHandler(captura)
        shutil.rmtree(destino, ignore_errors=True)

    if estado is not None:
        evidencia.estado["campo_esperado"] = turnos[-1].campo_esperado if turnos else None

    duracao = time.perf_counter() - inicio
    falhas = executar_checagens(evidencia, caso.checagens)
    bloqueantes = [falha for falha in falhas if falha.checagem != "sem_falha_de_provedor"]
    if erro_fatal is None and not bloqueantes:
        situacao = "ok"
    elif erro_fatal is not None or bloqueantes:
        infra = bool(evidencia.falhas_de_provedor) or bool(
            erro_fatal and "Error" in erro_fatal.split(":")[0]
        )
        situacao = "infra" if infra and not bloqueantes else "falha"
    else:  # pragma: no cover - defensivo
        situacao = "falha"
    if evidencia.falhas_de_provedor and situacao == "ok":
        situacao = "infra"

    return ResultadoCaso(
        caso_id=caso.id,
        tipo=caso.tipo,
        requisito=caso.requisito,
        descricao=caso.descricao,
        modelo=rotulo,
        rodada=rodada,
        situacao=situacao,
        segundos=duracao,
        chamadas=chamadas,
        tokens_entrada=tokens_entrada,
        tokens_saida=tokens_saida,
        falhas=falhas,
        infra=list(evidencia.falhas_de_provedor),
        erro_fatal=erro_fatal,
        evidencia=evidencia,
    )


def rodar(
    casos: Sequence[Caso],
    *,
    modelos: Sequence[Modelo] = (),
    modelos_ids: Sequence[str] = (),
    casos_ids: Sequence[str] = (),
    offline: bool = False,
    sem_rede: bool = False,
    repetir: int = 1,
    pausa: float = 0.0,
    progresso: Any = print,
) -> list[ResultadoCaso]:
    """Executa guardrails (offline) e, quando pedido, os casos de qualidade ao vivo."""
    selecionados = [caso for caso in casos if not casos_ids or caso.id in casos_ids]
    guardrails = [caso for caso in selecionados if caso.tipo == "guardrail"]
    qualidade = [] if offline else [caso for caso in selecionados if caso.tipo == "qualidade"]

    resultados: list[ResultadoCaso] = []

    for caso in guardrails:
        for rodada in range(1, repetir + 1):
            resultado = executar_caso(caso, sem_rede=True, rodada=rodada)
            resultados.append(resultado)
            progresso(f"  {_marca(resultado.situacao)} guardrail {caso.id}")

    if not qualidade:
        return resultados

    settings_llm = Settings(_env_file=RAIZ / ".env")
    braços = [m for m in modelos if not modelos_ids or m.id in modelos_ids]
    if not braços:
        progresso("  ! nenhum modelo selecionado: os casos de qualidade foram pulados")
        return resultados

    for modelo in braços:
        for caso in qualidade:
            for rodada in range(1, repetir + 1):
                if pausa:
                    time.sleep(pausa)
                try:
                    vivo = _modelo_ao_vivo(modelo, settings_llm)
                except Exception as erro:  # noqa: BLE001 - falta de chave é resultado, não crash
                    resultados.append(
                        ResultadoCaso(
                            caso_id=caso.id,
                            tipo=caso.tipo,
                            requisito=caso.requisito,
                            descricao=caso.descricao,
                            modelo=modelo.id,
                            rodada=rodada,
                            situacao="infra",
                            segundos=0.0,
                            chamadas=0,
                            tokens_entrada=None,
                            tokens_saida=None,
                            erro_fatal=f"{type(erro).__name__}: {erro}",
                        )
                    )
                    progresso(f"  ⚠️ {modelo.id} indisponível: {erro}")
                    break
                resultado = executar_caso(
                    caso,
                    modelo=vivo,
                    rotulo=modelo.id,
                    settings_llm=settings_llm,
                    sem_rede=sem_rede,
                    rodada=rodada,
                )
                resultados.append(resultado)
                progresso(f"  {_marca(resultado.situacao)} {modelo.id} · {caso.id}")
    return resultados


def _marca(situacao: str) -> str:
    return {"ok": "✅", "falha": "❌", "infra": "⚠️"}.get(situacao, "?")


def _tabela(cabecalho: Sequence[str], linhas: Iterable[Sequence[Any]]) -> str:
    linhas = [[str(celula) for celula in linha] for linha in linhas]
    larguras = [
        max(len(str(cabecalho[indice])), *(len(linha[indice]) for linha in linhas), 3)
        for indice in range(len(cabecalho))
    ]

    def formatar(linha: Sequence[str]) -> str:
        return (
            "| "
            + " | ".join(celula.ljust(larguras[indice]) for indice, celula in enumerate(linha))
            + " |"
        )

    separador = "|" + "|".join("-" * (largura + 2) for largura in larguras) + "|"
    corpo = [formatar(linha) for linha in linhas]
    return "\n".join([formatar(cabecalho), separador, *corpo])


def montar_relatorio(resultados: Sequence[ResultadoCaso], modelos: Sequence[Modelo]) -> str:
    """Relatório em markdown: resumo por modelo, guardrails, matriz e falhas."""
    quando = datetime.now().strftime("%Y-%m-%d %H:%M")
    guardrails = [r for r in resultados if r.tipo == "guardrail"]
    qualidade = [r for r in resultados if r.tipo == "qualidade"]
    braços = sorted({r.modelo for r in qualidade}) or [m.id for m in modelos]
    linhas: list[str] = [
        "# Avaliação do Banco Ágil",
        "",
        f"- Quando: {quando}",
        f"- Casos: {len(guardrails)} guardrails (offline) + "
        f"{len({r.caso_id for r in qualidade})} de qualidade × {len(braços)} modelo(s)",
        f"- Modelos: {', '.join(braços) if braços else '— (nenhum modelo ao vivo)'}",
        f"- Duração: {sum(r.segundos for r in resultados):.1f}s",
        "",
        "Legenda: ✅ passou · ❌ falhou no comportamento · ⚠️ falha de infraestrutura "
        "(quota, timeout, chave) — não é juízo sobre o modelo.",
        "",
    ]

    linhas += ["## Resumo por modelo (casos de qualidade)", ""]
    if qualidade:
        corpo = []
        for braço in braços:
            do_braço = [r for r in qualidade if r.modelo == braço]
            ok = sum(1 for r in do_braço if r.situacao == "ok")
            infra = sum(1 for r in do_braço if r.situacao == "infra")
            falha = sum(1 for r in do_braço if r.situacao == "falha")
            latencia = sum(r.segundos for r in do_braço) / max(len(do_braço), 1)
            tokens = sum(r.tokens_saida or 0 for r in do_braço)
            corpo.append(
                [
                    braço,
                    len(do_braço),
                    f"{ok}",
                    f"{falha}",
                    f"{infra}",
                    f"{latencia:.1f}s",
                    f"{tokens}",
                ]
            )
        linhas += [
            _tabela(
                ["modelo", "casos", "✅", "❌", "⚠️", "latência média", "tokens de saída"],
                corpo,
            ),
            "",
        ]
    else:
        linhas += ["Nenhum caso de qualidade foi executado (rode sem `--offline`).", ""]

    linhas += ["## Guardrails (offline, sem rede e sem modelo real)", ""]
    if guardrails:
        corpo = []
        for resultado in guardrails:
            detalhe = (
                resultado.erro_fatal
                or "; ".join(str(falha) for falha in resultado.falhas)
                or (resultado.evidencia.trecho(90) if resultado.evidencia else "")
            )
            corpo.append(
                [
                    resultado.caso_id,
                    resultado.requisito,
                    _marca(resultado.situacao),
                    detalhe.replace("|", "/"),
                ]
            )
        linhas += [_tabela(["caso", "requisito", "resultado", "evidência"], corpo), ""]
    else:
        linhas += ["Nenhum guardrail executado.", ""]

    if qualidade:
        linhas += ["## Matriz caso × modelo", ""]
        casos_ids = list(dict.fromkeys(r.caso_id for r in qualidade))
        corpo = []
        for caso_id in casos_ids:
            linha = [caso_id]
            for braço in braços:
                do_caso = [r for r in qualidade if r.caso_id == caso_id and r.modelo == braço]
                linha.append(" ".join(_marca(r.situacao) for r in do_caso) or "—")
            corpo.append(linha)
        linhas += [_tabela(["caso", *braços], corpo), ""]

        falhas = [r for r in qualidade if r.situacao == "falha"]
        linhas += ["## Falhas de qualidade", ""]
        if falhas:
            for resultado in falhas:
                for falha in resultado.falhas:
                    if falha.checagem == "sem_falha_de_provedor":
                        continue
                    linhas.append(
                        f"- `{resultado.caso_id}` · {resultado.modelo} · **{falha.checagem}** "
                        f"→ {falha.motivo}"
                    )
        else:
            linhas.append("Nenhuma: todos os modelos passaram nos casos executados.")
        linhas.append("")

    infra = [r for r in resultados if r.infra or r.situacao == "infra"]
    linhas += ["## Falhas de infraestrutura", ""]
    if infra:
        for resultado in infra:
            motivo = "; ".join(resultado.infra) or resultado.erro_fatal or ""
            linhas.append(f"- `{resultado.caso_id}` · {resultado.modelo} → {motivo}")
    else:
        linhas.append("Nenhuma.")
    linhas += [
        "",
        "## Como reproduzir",
        "",
        "```bash",
        "uv run python -m evals.runner --offline   # guardrails, sem rede",
        "uv run python -m evals.runner             # guardrails + qualidade ao vivo",
        "```",
        "",
        "JSON com turnos, trilha e checagens de cada caso fica ao lado deste arquivo.",
        "",
    ]
    return "\n".join(linhas)


def _json(resultado: ResultadoCaso) -> dict[str, Any]:
    dados = asdict(resultado)
    evidencia = dados.pop("evidencia", None)
    if evidencia:
        dados["evidencia"] = {
            "turnos": evidencia["turnos"],
            "estado": evidencia["estado"],
            "trilha": evidencia["trilha"],
            "resultados_ferramentas": evidencia["resultados_ferramentas"],
            "ferramentas_oferecidas": evidencia["ferramentas_oferecidas"],
            "solicitacoes_novas": evidencia["solicitacoes_novas"],
            "falhas_de_provedor": evidencia["falhas_de_provedor"],
            "excecoes": evidencia["excecoes"],
            "ultimas_falas": evidencia["turnos"][-1]["texto"] if evidencia["turnos"] else "",
        }
    return dados


def escrever_saida(
    resultados: Sequence[ResultadoCaso], modelos: Sequence[Modelo], destino: Path | None = None
) -> tuple[Path, Path]:
    destino = destino or DIRETORIO_SAIDA
    destino.mkdir(parents=True, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    caminho_md = destino / f"{marca}.md"
    caminho_json = destino / f"{marca}.json"
    caminho_md.write_text(montar_relatorio(resultados, modelos), encoding="utf-8")
    caminho_json.write_text(
        json.dumps([_json(r) for r in resultados], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return caminho_md, caminho_json


def _argumentos(argv: Sequence[str] | None = None) -> argparse.Namespace:
    analisador = argparse.ArgumentParser(
        prog="evals.runner",
        description="Roda os casos de guardrail (offline) e os de qualidade (ao vivo).",
    )
    analisador.add_argument("--offline", action="store_true", help="só guardrails, sem rede")
    analisador.add_argument("--sem-rede", action="store_true", help="força o câmbio falso")
    analisador.add_argument("--modelos", default="", help="ids de modelos (vírgula)")
    analisador.add_argument("--casos", default="", help="ids de casos (vírgula)")
    analisador.add_argument("--repetir", type=int, default=1, help="rodadas por caso")
    analisador.add_argument("--pausa", type=float, default=0.0, help="segundos entre casos")
    analisador.add_argument("--saida", default="", help="diretório do relatório")
    analisador.add_argument("--lista", action="store_true", help="lista casos e modelos")
    analisador.add_argument("--silencioso", action="store_true", help="sem progresso no stderr")
    return analisador.parse_args(argv)


def _separar(texto: str) -> list[str]:
    return [item.strip() for item in texto.split(",") if item.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    opcoes = _argumentos(argv)
    casos = ler_casos(ARQUIVO_GUARDRAILS) + ler_casos(ARQUIVO_GOLDEN)
    modelos = ler_modelos()

    if opcoes.lista:
        print("Casos:")
        for caso in casos:
            print(f"  [{caso.tipo:9}] {caso.id}  ({caso.requisito}) {caso.descricao}")
        print("\nModelos:")
        for modelo in modelos:
            print(f"  {modelo.id:18} {' → '.join(modelo.passos)}")
        return 0

    progresso = (lambda *_a, **_k: None) if opcoes.silencioso else print
    resultados = rodar(
        casos,
        modelos=modelos,
        modelos_ids=_separar(opcoes.modelos),
        casos_ids=_separar(opcoes.casos),
        offline=opcoes.offline,
        sem_rede=opcoes.sem_rede,
        repetir=max(opcoes.repetir, 1),
        pausa=opcoes.pausa,
        progresso=progresso,
    )

    caminho_md, caminho_json = escrever_saida(
        resultados, modelos, Path(opcoes.saida) if opcoes.saida else None
    )
    falhas = [r for r in resultados if r.situacao == "falha"]
    infra = [r for r in resultados if r.situacao == "infra"]
    print(f"\nRelatório: {caminho_md}")
    print(f"Dados:     {caminho_json}")
    aprovados = len(resultados) - len(falhas) - len(infra)
    print(f"{aprovados} ok · {len(falhas)} falha(s) · {len(infra)} infra")
    if falhas:
        for resultado in falhas:
            for falha in resultado.falhas:
                print(f"  ❌ {resultado.caso_id} · {resultado.modelo} · {falha}")
    return 1 if falhas else 0


if __name__ == "__main__":  # pragma: no cover - entrada de linha de comando
    sys.exit(main())
