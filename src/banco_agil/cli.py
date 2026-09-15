"""Chat de terminal — útil para demonstrar e depurar o atendimento.

Uso:
    uv run python -m banco_agil.cli                    # usa LLM_PROVIDER do .env
    uv run python -m banco_agil.cli --fake             # roteiro de mentira, sem rede
    uv run python -m banco_agil.cli --auditoria        # mostra a trilha interna
"""

from __future__ import annotations

import argparse
import sys

from banco_agil.config import obter_settings
from banco_agil.errors import BancoAgilError, LLMProviderError
from banco_agil.llm.factory import construir_cadeia, construir_modelo, passos_da_cadeia
from banco_agil.observability.log import configurar_logging
from banco_agil.orchestrator.atendimento import Atendimento
from banco_agil.servicos import construir_servicos

__all__ = ["main"]


def _imprimir_auditoria(
    atendimento: Atendimento, resposta_agente: str, chamadas: list[str], corrente: object = None
) -> None:
    estado = atendimento.estado
    print(
        f"      [auditoria] papel={resposta_agente} autenticado={estado.autenticado} "
        f"tentativas={estado.tentativas_autenticacao} ferramentas={chamadas or '-'}"
    )
    if corrente is not None:
        passos = getattr(corrente, "passos", None)
        rotulo = " → ".join(passo.rotulo for passo in passos) if passos else None
        print(
            f"      [cadeia] fallbacks={getattr(corrente, 'trocas', 0)}"
            + (f" | {rotulo}" if rotulo else "")
        )
        for passo, falha in getattr(corrente, "falhas", []):
            print(f"      [fallback] {passo.rotulo}: {falha[:120]}")
    for evento in estado.trilha[-3:]:
        print(f"      [trilha] {evento}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Atendimento do Banco Ágil no terminal.")
    parser.add_argument("--provider", default=None, help="Provedor de LLM (sobrescreve o .env).")
    parser.add_argument("--model", default=None, help="Modelo (sobrescreve o .env).")
    parser.add_argument("--fake", action="store_true", help="Usa o modelo de mentira, sem rede.")
    parser.add_argument("--auditoria", action="store_true", help="Mostra a trilha interna.")
    argumentos = parser.parse_args(argv)

    settings = obter_settings()
    configurar_logging(settings.caminho_erros, nivel=settings.log_level)

    provedor = "fake" if argumentos.fake else argumentos.provider
    if argumentos.fake or provedor is not None:
        try:
            modelo = construir_modelo(settings, provedor=provedor, modelo=argumentos.model)
        except LLMProviderError as erro:
            print(f"Não consegui inicializar o modelo: {erro}", file=sys.stderr)
            return 2
    else:
        try:
            modelo = construir_cadeia(settings)
        except LLMProviderError as erro:
            print(f"Não consegui inicializar a cadeia de modelos: {erro}", file=sys.stderr)
            return 2

    servicos = construir_servicos(settings)
    atendimento = Atendimento(modelo, servicos)

    passos = passos_da_cadeia(settings) if provedor is None else []
    print(f"Banco Ágil — atendimento (provedor: {provedor or settings.llm_provider})")
    if passos:
        print("Cadeia: " + " → ".join(passo.rotulo for passo in passos))
    print("Digite 'sair' para encerrar o processo.\n")

    try:
        abertura = atendimento.abrir()
    except BancoAgilError as erro:
        print(f"Falha ao abrir o atendimento: {erro}", file=sys.stderr)
        return 1
    print(f"Banco Ágil: {abertura.texto}\n")

    while True:
        try:
            entrada = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if entrada.lower() in {"sair", "exit", "quit"}:
            break
        if not entrada:
            continue

        resposta = atendimento.responder(entrada)
        print(f"Banco Ágil: {resposta.texto}\n")
        if argumentos.auditoria:
            _imprimir_auditoria(atendimento, resposta.agente, resposta.ferramentas_chamadas, modelo)

        if resposta.encerrado:
            print(f"[atendimento encerrado: {atendimento.estado.motivo_encerramento}]")
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
