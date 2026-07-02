.PHONY: test typecheck lint verify loadtest

test:
	python -m pytest

typecheck:
	python -m mypy .

lint:
	python -m ruff check .

verify: test typecheck lint

loadtest:
	python tools/load_test.py
