# Atalhos do Banco Ágil. `make` sozinho lista os alvos.
# Requer o `uv` no PATH: https://docs.astral.sh/uv/

.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help install dados start test lint format check eval eval-offline ui cli limpar

help: ## lista os alvos disponíveis
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

install: ## instala as dependências (uv sync)
	uv sync

dados: ## gera a base fictícia (CPFs com dígito verificador válido; não sobrescreve)
	uv run python scripts/gerar_dados_iniciais.py

start: install dados ## do zero ao navegador: dependências + base + interface web
	@echo "→ interface em http://localhost:8501  (Ctrl+C encerra)"
	uv run streamlit run app.py

test: ## suíte completa: 344 testes, sem rede e sem chave
	uv run pytest

lint: ## ruff check + format --check
	uv run ruff check .
	uv run ruff format --check .

format: ## aplica a formatação do ruff
	uv run ruff format .

eval-offline: ## 13 guardrails (red team): sem rede, sem token
	uv run python -m evals.runner --offline

eval: ## guardrails + qualidade contra modelos reais (gasta token)
	uv run python -m evals.runner

check: lint test eval-offline ## o que tem de passar antes de entregar

ui: ## interface web (Streamlit)
	uv run streamlit run app.py

cli: ## chat de terminal com a trilha de auditoria
	uv run python -m banco_agil.cli --auditoria

limpar: ## remove caches locais (pytest, ruff, __pycache__)
	rm -rf .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
