"""Repositório CSV com escrita atômica e lock por arquivo.

Os três CSVs do desafio são **estado mutável** (a entrevista reescreve o score
do cliente), então leitura/escrita passam por aqui: nada de ``open(..., "w")``
espalhado pelo código.

Garantias:
- escrita atômica (arquivo temporário + ``os.replace``) — nunca deixa CSV pela
  metade, nem se o processo morrer no meio;
- lock por caminho resolvido (``threading``), então a UI do Streamlit pode
  atender duas sessões sem perder linha em anexo concorrente;
- contrato de schema explícito: cabeçalho divergente ou coluna desconhecida
  levantam ``DataStoreError`` em vez de gravar lixo silenciosamente.
"""

from __future__ import annotations

import csv
import os
import tempfile
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from banco_agil.errors import DataStoreError

__all__ = ["CsvRepository"]

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_do_caminho(caminho: Path) -> threading.RLock:
    """Um lock por arquivo, compartilhado entre instâncias e threads."""
    chave = str(caminho.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(chave, threading.RLock())


class CsvRepository:
    """Acesso a um CSV com cabeçalho fixo e chave de busca opcional."""

    def __init__(
        self,
        caminho: str | Path,
        *,
        colunas: Sequence[str],
        chave: str | None = None,
        delimitador: str = ",",
    ) -> None:
        if not colunas:
            raise DataStoreError("Repositório CSV precisa de pelo menos uma coluna.")
        if chave is not None and chave not in colunas:
            raise DataStoreError(f"Coluna-chave {chave!r} não existe em {list(colunas)}.")

        self.caminho = Path(caminho)
        self.colunas = list(colunas)
        self.chave = chave
        self.delimitador = delimitador
        self._lock = _lock_do_caminho(self.caminho)

    def garantir_arquivo(self) -> None:
        """Cria o CSV com o cabeçalho canônico se ele ainda não existir."""
        with self._lock:
            if self.caminho.exists():
                return
            self.caminho.parent.mkdir(parents=True, exist_ok=True)
            self._escrever([])

    @contextmanager
    def travar(self) -> Iterator[None]:
        """Segura o lock do arquivo durante uma sequência de operações.

        Quem cria e resolve o mesmo registro em duas escritas precisa disto: as
        chaves de negócio (``cpf`` + ``data_hora``, com precisão de segundo) não
        são únicas, então sem o lock a resolução de uma sessão pode casar com a
        linha criada por outra no mesmo segundo.
        """
        with self._lock:
            yield

    def ler_todos(self) -> list[dict[str, str]]:
        """Devolve todas as linhas como dicionários (ordem do arquivo)."""
        with self._lock:
            self.garantir_arquivo()
            try:
                with self.caminho.open(encoding="utf-8", newline="") as arquivo:
                    leitor = csv.DictReader(arquivo, delimiter=self.delimitador)
                    if leitor.fieldnames is None:
                        raise DataStoreError(f"{self.caminho.name} está vazio: sem cabeçalho.")
                    if leitor.fieldnames != self.colunas:
                        raise DataStoreError(
                            f"Cabeçalho divergente em {self.caminho.name}: "
                            f"esperado {self.colunas}, encontrado {leitor.fieldnames}."
                        )
                    return [
                        {coluna: (linha.get(coluna) or "") for coluna in self.colunas}
                        for linha in leitor
                    ]
            except OSError as erro:
                raise DataStoreError(f"Falha ao ler {self.caminho.name}: {erro}.") from erro

    def buscar(self, valor_chave: object) -> dict[str, str] | None:
        """Busca a primeira linha cuja chave bate (comparação como texto)."""
        self._exigir_chave("buscar")
        alvo = str(valor_chave)
        for linha in self.ler_todos():
            if linha[self.chave or ""] == alvo:
                return linha
        return None

    def filtrar(self, **campos: object) -> list[dict[str, str]]:
        """Devolve as linhas que casam com todos os campos informados."""
        self._validar_colunas(campos)
        alvos = {coluna: str(valor) for coluna, valor in campos.items()}
        return [
            linha
            for linha in self.ler_todos()
            if all(linha[coluna] == valor for coluna, valor in alvos.items())
        ]

    def anexar(self, linha: dict[str, object]) -> dict[str, str]:
        """Acrescenta uma linha (colunas ausentes viram string vazia)."""
        self._validar_colunas(linha)
        nova = {coluna: self._texto(linha.get(coluna)) for coluna in self.colunas}
        with self._lock:
            linhas = self.ler_todos()
            linhas.append(nova)
            self._escrever(linhas)
        return nova

    def anexar_varias(self, linhas: Iterable[dict[str, object]]) -> int:
        """Acrescenta várias linhas em uma única escrita atômica."""
        novas = []
        for linha in linhas:
            self._validar_colunas(linha)
            novas.append({coluna: self._texto(linha.get(coluna)) for coluna in self.colunas})
        if not novas:
            return 0
        with self._lock:
            atuais = self.ler_todos()
            atuais.extend(novas)
            self._escrever(atuais)
        return len(novas)

    def atualizar(self, valor_chave: object, **campos: object) -> dict[str, str]:
        """Atualiza os campos informados da linha identificada pela chave."""
        self._exigir_chave("atualizar")
        self._validar_colunas(campos)
        chave = self.chave or ""
        alvo = str(valor_chave)

        with self._lock:
            linhas = self.ler_todos()
            for linha in linhas:
                if linha[chave] == alvo:
                    for coluna, valor in campos.items():
                        linha[coluna] = self._texto(valor)
                    self._escrever(linhas)
                    return dict(linha)

        raise DataStoreError(
            f"Registro com {chave}={alvo!r} não encontrado em {self.caminho.name}."
        )

    def atualizar_ultima_ocorrencia(
        self, filtros: Mapping[str, object], **campos: object
    ) -> dict[str, str]:
        """Atualiza a **última** linha que casa com todos os filtros.

        Usado no pedido de aumento: o registro nasce ``pendente`` e é resolvido
        na mesma operação, identificado por ``cpf_cliente`` +
        ``data_hora_solicitacao`` (o mesmo cliente pode ter vários pedidos).
        """
        self._validar_colunas(campos)
        self._validar_colunas(filtros)
        if not filtros:
            raise DataStoreError("Atualização por filtro exige ao menos um campo.")

        with self._lock:
            linhas = self.ler_todos()
            for linha in reversed(linhas):
                if all(linha[coluna] == self._texto(valor) for coluna, valor in filtros.items()):
                    for coluna, valor in campos.items():
                        linha[coluna] = self._texto(valor)
                    self._escrever(linhas)
                    return dict(linha)

        raise DataStoreError(f"Nenhum registro em {self.caminho.name} casa com {dict(filtros)!r}.")

    def _escrever(self, linhas: Sequence[dict[str, str]]) -> None:
        """Escreve o CSV inteiro de forma atômica (tmp + replace)."""
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        descritor, temporario = tempfile.mkstemp(
            dir=str(self.caminho.parent), prefix=f".{self.caminho.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descritor, "w", encoding="utf-8", newline="") as arquivo:
                escritor = csv.DictWriter(
                    arquivo,
                    fieldnames=self.colunas,
                    delimiter=self.delimitador,
                    lineterminator="\n",
                )
                escritor.writeheader()
                escritor.writerows(linhas)
                arquivo.flush()
                os.fsync(arquivo.fileno())
            os.replace(temporario, self.caminho)
        except OSError as erro:
            raise DataStoreError(f"Falha ao gravar {self.caminho.name}: {erro}.") from erro
        finally:
            if os.path.exists(temporario):
                os.unlink(temporario)

    def _exigir_chave(self, operacao: str) -> None:
        if self.chave is None:
            raise DataStoreError(f"Operação {operacao!r} exige uma coluna-chave definida.")

    def _validar_colunas(self, campos: dict[str, object]) -> None:
        desconhecidas = sorted(set(campos) - set(self.colunas))
        if desconhecidas:
            raise DataStoreError(
                f"Coluna(s) inexistente(s) em {self.caminho.name}: {desconhecidas}. "
                f"Colunas válidas: {self.colunas}."
            )

    @staticmethod
    def _texto(valor: object) -> str:
        if valor is None:
            return ""
        if isinstance(valor, bool):
            return "sim" if valor else "nao"
        return str(valor)
