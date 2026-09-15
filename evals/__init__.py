"""Avaliação do atendimento: casos de qualidade (ao vivo) e guardrails (offline).

O pacote é ferramenta de desenvolvimento, não parte do produto: nada em
``src/banco_agil`` importa daqui.

- ``evals/golden.yaml``: casos de **qualidade**, rodados contra modelos reais, que
  produzem a tabela comparativa de modelos.
- ``evals/guardrails.yaml``: casos de **guardrail** (red team), roteirizados e
  offline, que provam que o desenho segura o que promete — inclusive quando o
  modelo do outro lado obedece a uma injeção de prompt.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["RAIZ", "DIRETORIO_EVAL"]

# `python -m evals.runner` precisa achar `banco_agil` (layout src/), sem PYTHONPATH.
RAIZ = Path(__file__).resolve().parents[1]
_SRC = RAIZ / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

DIRETORIO_EVAL = Path(__file__).resolve().parent
