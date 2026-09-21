.PHONY: dev worker test migrate migration seed-admin install lint perf-seed perf-worker perf-run perf-report

install:
	pip install -r requirements.txt

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# SimpleWorker runs jobs in-process: macOS kills forked RQ work-horses (objc fork safety).
worker:
	rq worker settlement --with-scheduler --worker-class rq.worker.SimpleWorker

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

# --- Phase 9 performance testing (see perf/REPORT.md) ---
# perf-worker uses a SIMULATED ledger: never load-test with the real `make worker`.

perf-seed:
	python perf/seed_data.py 200

perf-worker:
	python perf/perf_worker.py

# Usage: make perf-run [users=50] [time=3m]
perf-run:
	mkdir -p perf/results
	locust -f perf/locustfile.py --headless -u $(or $(users),50) -r 5 -t $(or $(time),3m) \
		-H http://127.0.0.1:8000 --csv perf/results/run

perf-report:
	python perf/analyze.py
