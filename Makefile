PY ?= .venv/bin/python
MODELS ?= gemini-2.5-flash-lite gemma-4-31b-it
export PYTHONPATH := .

.PHONY: setup test gold-check dictionary web-assets eval eval-offline report examples deploy all

setup:            ## venv + pinned deps
	uv venv -q -p 3.12 .venv && uv pip install -q -p .venv -r requirements.txt

test:             ## guard, comparator, agent loop, gold integrity, JS/Python parity
	$(PY) -m pytest -q

gold-check:       ## execute every gold query + independent checks + pinned values
	$(PY) -m askdata.gold

dictionary:       ## rebuild data/dictionary.json from the snapshot + upstream column comments
	$(PY) -m askdata.dictionary

web-assets:       ## export prompt assets + parquet for the web runtime
	$(PY) -m askdata.export_web

eval: gold-check  ## run the eval (free-tier Gemini; cached replies are reused)
	$(PY) -m askdata.evaluate --models $(MODELS)
	$(PY) -m askdata.report

eval-offline:     ## CI: re-score from the committed reply cache, fail on any cache miss
	$(PY) -m askdata.evaluate --models $(MODELS) --offline --out /tmp/askdata-eval-offline.json

report:           ## render web/public/accuracy.html and results/RESULTS.md
	$(PY) -m askdata.report

examples:         ## cached example answers for the site
	$(PY) scripts/build_examples.py

deploy: web-assets report  ## deploy to Cloudflare Pages (functions/ is a child of web/, the wrangler cwd)
	cd web && CLOUDFLARE_API_TOKEN="$$LEVELBROOK_CF_DEPLOY_TOKEN" CLOUDFLARE_ACCOUNT_ID=a67eceeb4b89d2d4171ed209e87c9456 \
	  npx wrangler pages deploy public --project-name=askdata --branch=main --commit-dirty=true
