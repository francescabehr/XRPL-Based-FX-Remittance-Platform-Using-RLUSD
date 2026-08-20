.PHONY: dev worker test migrate migration seed-admin install lint

install:
	pip install -r requirements.txt

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker:
	rq worker settlement --with-scheduler

migrate:
	alembic upgrade head

# Usage: make migration name="add_beneficiaries"
migration:
	alembic revision --autogenerate -m "$(name)"

seed-admin:
	python -m app.scripts.seed_admin

test:
	pytest tests/ -v

lint:
	python -m py_compile $$(find app tests -name "*.py")
