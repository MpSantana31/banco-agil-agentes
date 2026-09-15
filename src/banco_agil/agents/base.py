"""Especificação de um agente: papel, prompt e ferramentas permitidas.

O `EspecificacaoAgente` é o contrato entre o orquestrador e cada agente. Quem
define o que um agente *pode* fazer é a lista de ferramentas — o Agente de
Câmbio não tem como aprovar crédito porque não recebe essa ferramenta. É uma
guarda estrutural, não uma frase no prompt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from banco_agil.errors import BancoAgilError

__all__ = ["EspecificacaoAgente", "RegistroDeAgentes"]


@dataclass(frozen=True, slots=True)
class EspecificacaoAgente:
    """Papel de um agente no atendimento."""

    nome: str
    titulo: str
    prompt: str
    ferramentas: tuple[str, ...] = ()
    descricao: str = ""
    pode_encerrar: bool = True


class RegistroDeAgentes:
    """Catálogo de agentes + seleção das ferramentas de cada um."""

    def __init__(self, especificacoes: Sequence[EspecificacaoAgente]) -> None:
        if not especificacoes:
            raise BancoAgilError("Registro de agentes vazio.")
        self._por_nome = {especificacao.nome: especificacao for especificacao in especificacoes}
        if len(self._por_nome) != len(especificacoes):
            raise BancoAgilError("Há agentes com o mesmo nome no registro.")

    def __contains__(self, nome: object) -> bool:
        return nome in self._por_nome

    def __len__(self) -> int:
        return len(self._por_nome)

    @property
    def nomes(self) -> tuple[str, ...]:
        return tuple(self._por_nome)

    def obter(self, nome: str) -> EspecificacaoAgente:
        try:
            return self._por_nome[nome]
        except KeyError as erro:
            raise BancoAgilError(
                f"Agente desconhecido: {nome!r}. Registrados: {', '.join(self.nomes)}."
            ) from erro

    def ferramentas_de(self, nome: str, ferramentas: Mapping[str, BaseTool]) -> list[BaseTool]:
        """Ferramentas disponíveis para o agente, na ordem da especificação."""
        especificacao = self.obter(nome)
        selecionadas: list[BaseTool] = []
        for nome_ferramenta in especificacao.ferramentas:
            if nome_ferramenta not in ferramentas:
                raise BancoAgilError(
                    f"Agente {nome!r} pede a ferramenta {nome_ferramenta!r}, "
                    "que não existe no conjunto da sessão."
                )
            selecionadas.append(ferramentas[nome_ferramenta])
        return selecionadas

    def descricao_para_o_modelo(self) -> str:
        """Lista de destinos válidos para handoff (vai no prompt do sistema)."""
        linhas = [
            f"- {especificacao.nome}: {especificacao.descricao}"
            for especificacao in self._por_nome.values()
        ]
        return "\n".join(linhas)
