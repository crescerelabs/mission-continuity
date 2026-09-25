PY := .venv/bin/python
MC := .venv/bin/mc

.PHONY: setup test spend demo replay

setup:
	python3.12 -m venv .venv
	.venv/bin/pip install -r requirements.lock
	.venv/bin/pip install --no-deps -e .

test:
	.venv/bin/pytest -q

spend:
	$(MC) spend

demo:
	.venv/bin/streamlit run app/streamlit_app.py --server.port 8501 --server.headless true

replay:
	MC_REPLAY_BUNDLE=$(BUNDLE) .venv/bin/streamlit run app/streamlit_app.py --server.port 8501 --server.headless true
