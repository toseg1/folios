.PHONY: setup check

setup:
	pip install -r requirements.txt

check:
	ruff check .
	pytest --cov=folios
