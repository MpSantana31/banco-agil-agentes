"""Leitura e validação dos arquivos de caso (``golden.yaml`` / ``guardrails.yaml``).

O caso é dado, não código: quem revisa o repositório deve conseguir ler o que se
espera do atendimento sem abrir o Python. Aqui só se lê e se valida o formato —
execução fica no ``runner``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evals import DIRETORIO_EVAL
from evals.checks import nome_desconhecido

__all__ = [
    "Caso",
    "PassoAbertura",
    "PassoCliente",
    "PassoModelo",
    "Passo",
    "Modelo",
    "ARQUIVO_GOLDEN",
    "ARQUIVO_GUARDRAILS",
    "ler_casos",
    "ler_modelos",
    "validar",
]

ARQUIVO_GOLDEN = DIRETORIO_EVAL / "golden.yaml"
ARQUIVO_GUARDRAILS = DIRETORIO_EVAL / "guardrails.yaml"

TIPOS = ("qualidade", "guardrail")


@dataclass(frozen=True, slots=True)
class PassoAbertura:
    """Primeiro turno: o atendente se apresenta (``Atendimento.abrir``)."""


@dataclass(frozen=True, slots=True)
class PassoCliente:
    """Uma mensagem escrita pelo cliente."""

    texto: str


@dataclass(frozen=True, slots=True)
class PassoModelo:
    """Resposta roteirizada do modelo (só em casos de guardrail)."""

    texto: str = ""
    ferramenta: str | None = None
    argumentos: dict[str, Any] = field(default_factory=dict)


Passo = PassoAbertura | PassoCliente | PassoModelo


@dataclass(frozen=True, slots=True)
class Caso:
    """Um caso de avaliação."""

    id: str
    descricao: str
    requisito: str
    tipo: str
    passos: tuple[Passo, ...]
    checagens: dict[str, Any]
    cambio: str = "falso"

    @property
    def roteirizado(self) -> bool:
        """Casos roteirizados não chamam modelo real (guardrails são offline)."""
        return any(isinstance(passo, PassoModelo) for passo in self.passos)

    @property
    def precisa_de_rede(self) -> bool:
        return self.cambio == "real"


@dataclass(frozen=True, slots=True)
class Modelo:
    """Um braço da comparação: um modelo, ou uma cadeia com fallback."""

    id: str
    rotulo: str
    passos: tuple[str, ...]


def _passo(bruto: Any, *, indice: int, caso_id: str) -> Passo:
    if isinstance(bruto, str):
        if bruto.strip().lower() == "abertura":
            return PassoAbertura()
        return PassoCliente(texto=bruto)

    if not isinstance(bruto, dict):
        raise ValueError(f"{caso_id}: passo {indice} inválido: {bruto!r}")

    if "abertura" in bruto:
        return PassoAbertura()
    if "cliente" in bruto:
        return PassoCliente(texto=str(bruto["cliente"]))
    if "modelo" in bruto:
        corpo = bruto["modelo"] or {}
        if not isinstance(corpo, dict):
            raise ValueError(f"{caso_id}: passo {indice}: 'modelo' precisa ser um mapa")
        return PassoModelo(
            texto=str(corpo.get("texto", "")),
            ferramenta=corpo.get("ferramenta"),
            argumentos=dict(corpo.get("argumentos") or {}),
        )
    raise ValueError(f"{caso_id}: passo {indice} sem 'abertura', 'cliente' nem 'modelo'")


def _caso(bruto: dict[str, Any]) -> Caso:
    caso_id = str(bruto["id"])
    passos = tuple(
        _passo(passo, indice=indice, caso_id=caso_id)
        for indice, passo in enumerate(bruto.get("passos") or [], start=1)
    )
    return Caso(
        id=caso_id,
        descricao=str(bruto.get("descricao", "")).strip(),
        requisito=str(bruto.get("requisito", "")).strip(),
        tipo=str(bruto.get("tipo", "qualidade")),
        passos=passos,
        checagens=dict(bruto.get("checagens") or {}),
        cambio=str(bruto.get("cambio", "falso")),
    )


def ler_casos(caminho: Path) -> list[Caso]:
    """Lê os casos de um YAML; levanta ``ValueError`` se o arquivo estiver inválido."""
    dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    casos = [_caso(bruto) for bruto in dados.get("casos") or []]
    validar(casos)
    return casos


def ler_modelos(caminho: Path | None = None) -> list[Modelo]:
    """Lê a lista de modelos do ``golden.yaml`` (vazia se o arquivo não declarar)."""
    dados = yaml.safe_load((caminho or ARQUIVO_GOLDEN).read_text(encoding="utf-8")) or {}
    modelos: list[Modelo] = []
    for bruto in dados.get("modelos") or []:
        passos = bruto.get("passos") or ([bruto["passo"]] if bruto.get("passo") else [])
        modelos.append(
            Modelo(
                id=str(bruto["id"]),
                rotulo=str(bruto.get("rotulo") or bruto["id"]),
                passos=tuple(str(passo) for passo in passos),
            )
        )
    return modelos


def validar(casos: list[Caso]) -> None:
    """Regras do formato: id único e não vazio, tipo conhecido, checagem existente."""
    vistos: set[str] = set()
    problemas: list[str] = []
    for caso in casos:
        if not caso.id:
            problemas.append("caso sem id")
        if caso.id in vistos:
            problemas.append(f"id repetido: {caso.id}")
        vistos.add(caso.id)
        if caso.tipo not in TIPOS:
            problemas.append(f"{caso.id}: tipo {caso.tipo!r} fora de {TIPOS}")
        if not caso.passos:
            problemas.append(f"{caso.id}: sem passos")
        if not caso.checagens:
            problemas.append(f"{caso.id}: sem checagens")
        if caso.cambio not in ("real", "falso"):
            problemas.append(f"{caso.id}: cambio {caso.cambio!r} fora de ('real', 'falso')")
        desconhecida = nome_desconhecido(caso.checagens)
        if desconhecida:
            problemas.append(f"{caso.id}: checagem desconhecida {desconhecida!r}")
        for indice, passo in enumerate(caso.passos):
            if isinstance(passo, PassoAbertura) and indice != 0:
                problemas.append(f"{caso.id}: 'abertura' só pode ser o primeiro passo")
            if isinstance(passo, PassoModelo) and not passo.ferramenta and not passo.texto:
                problemas.append(f"{caso.id}: passo {indice + 1} do modelo está vazio")
        if caso.tipo == "guardrail" and not caso.roteirizado:
            problemas.append(
                f"{caso.id}: guardrail precisa de roteiro (passos 'modelo') e não pode usar rede"
            )
    if problemas:
        raise ValueError("casos inválidos:\n- " + "\n- ".join(problemas))
