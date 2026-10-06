CLUSTER   ?= kubelantern
IMAGE     ?= kubelantern-agent:dev
GW_IMAGE  ?= kubelantern-gateway:dev
MODEL     ?= qwen2.5:1.5b
NS        ?= demo
AGENT_NS  ?= demo payments orders
AI_NS     ?= kubelantern-ai
HELM      ?= helm
# Set EGRESS=true to deploy agents with the default-deny-egress NetworkPolicy.
EGRESS    ?= false

.PHONY: help venv install test lint cluster cluster-down image load namespaces \
        deploy-agent deploy-agents logs test-crashloop test-oom test-imagepull \
        test-failures clean-failures test-rbac up run-local \
        test-scale test-cause-change test-recover \
        gateway-image ai-up ai-pull ai-status ai-logs ai-down test-gateway \
        ai-model eval onboard-runbooks runbooks-demo test-rag \
        chart-lint egress-lockdown egress-unlock migrate-to-helm incidents test-incidents

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n",$$1,$$2}'

# ---- local dev ---------------------------------------------------------
# Use the project venv when it exists, otherwise the system Python.
PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

venv: ## Create .venv and install dev deps
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip   # old pip backtracks through every ruff release
	.venv/bin/pip install -e '.[dev]'

test: ## Run unit tests
	$(PY) -m pytest

lint: ## Lint with ruff
	$(PY) -m ruff check .

run-local: ## Run agent on your laptop against current kubeconfig (NS=demo)
	python -m agent.main --namespace $(NS)

# ---- cluster -----------------------------------------------------------
cluster: ## Create kind cluster
	kind create cluster --config deployments/kind/kind-config.yaml

cluster-down: ## Delete kind cluster
	kind delete cluster --name $(CLUSTER)

image: ## Build agent image
	docker build -t $(IMAGE) .

gateway-image: ## Build gateway image
	docker build -f Dockerfile.gateway -t $(GW_IMAGE) .

load: image gateway-image ## Build and load agent + gateway images into kind
	kind load docker-image $(IMAGE) $(GW_IMAGE) --name $(CLUSTER)

namespaces: ## Create demo/payments/orders namespaces
	kubectl apply -f deployments/namespaces.yaml

deploy-agent: ## Install/upgrade the agent chart in one namespace (NS=demo [EGRESS=true])
	$(HELM) upgrade --install kubelantern-agent charts/kubelantern-agent -n $(NS) \
	  -f deployments/kind/values-agent.yaml \
	  --set networkPolicy.egress.enabled=$(EGRESS) --wait --timeout 120s

deploy-agents: namespaces ## Deploy an agent into every namespace in AGENT_NS
	@for ns in $(AGENT_NS); do $(MAKE) --no-print-directory deploy-agent NS=$$ns; done

up: cluster load deploy-agents ## Cluster + images + all agents, from zero (AI: make ai-up)

logs: ## Follow agent output (NS=demo)
	@kubectl -n $(NS) rollout status deploy/kubelantern-agent --timeout=120s >/dev/null
	kubectl -n $(NS) logs -f deploy/kubelantern-agent

# ---- failure scenarios ---------------------------------------------------
test-crashloop: ## Deploy CrashLoopBackOff workload
	kubectl apply -f tests/crashloop/

test-oom: ## Deploy OOMKilled workload
	kubectl apply -f tests/oom/

test-imagepull: ## Deploy ImagePullBackOff workload
	kubectl apply -f tests/imagepull/

test-failures: test-crashloop test-oom test-imagepull ## Deploy all failure scenarios

# ---- incident lifecycle (Stage 4) — run after test-crashloop -------------
test-scale: ## Scale broken-app to 3 replicas (expect: SCOPE CHANGED, same incident)
	kubectl -n demo scale deploy/broken-app --replicas=3

test-cause-change: ## Make broken-app OOM instead of exit 1 (expect: CAUSE CHANGED)
	kubectl -n demo patch deploy/broken-app --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["sh","-c","echo allocating; x=$$(yes | head -c 100000000); sleep 5"]}]'

test-recover: ## Fix broken-app (expect: RESOLVED after ~2 min healthy)
	kubectl -n demo patch deploy/broken-app --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["sh","-c","echo healthy; sleep 3600"]}]'

clean-failures: ## Remove failure scenarios
	kubectl delete -f tests/crashloop/ -f tests/oom/ -f tests/imagepull/ --ignore-not-found

# ---- AI platform: gateway + Ollama + Qdrant + CRDs (kubelantern-ai chart)
ai-up: ## Install/upgrade the kubelantern-ai chart (MODEL=qwen2.5:1.5b)
	@echo "First run downloads the Ollama image (~4 GB) and models (~1.3 GB); this can take a while."
	$(HELM) upgrade --install kubelantern-ai charts/kubelantern-ai -n $(AI_NS) --create-namespace \
	  -f deployments/kind/values-ai.yaml --set gateway.model=$(MODEL) \
	  --wait --timeout 30m

ai-pull: ## Pull a model manually (MODEL=...)
	kubectl -n $(AI_NS) exec deploy/ollama -- ollama pull $(MODEL)

ai-status: ## Show AI pods, models and gateway readiness
	kubectl -n $(AI_NS) get pods,svc,networkpolicy
	kubectl -n $(AI_NS) exec deploy/ollama -- ollama list

ai-logs: ## Follow gateway logs (audit + errors)
	kubectl -n $(AI_NS) logs -f deploy/kubelantern-gateway

ai-down: ## Remove the platform release and namespace (keeps agents and the Runbook CRD)
	-$(HELM) uninstall kubelantern-ai -n $(AI_NS)
	kubectl delete namespace $(AI_NS) --ignore-not-found

ai-model: ## Switch the gateway to another model (MODEL=qwen2.5:3b); pulls it if needed
	$(HELM) upgrade kubelantern-ai charts/kubelantern-ai -n $(AI_NS) \
	  --reuse-values --set gateway.model=$(MODEL) --wait --timeout 30m

eval: ## Score diagnosis quality on 9 known scenarios (runs from the demo agent pod)
	kubectl -n demo exec -i deploy/kubelantern-agent -- python - < tests/eval/run_eval.py

# ---- runbooks (Stage 7) ---------------------------------------------------
GROUP ?= $(NS)-developers
onboard-runbooks: ## Let a team manage Runbooks in its namespace (NS=payments GROUP=payments-devs)
	$(HELM) upgrade kubelantern-agent charts/kubelantern-agent -n $(NS) \
	  --reuse-values --set 'runbooks.editors.groups[0]=$(GROUP)' --wait --timeout 120s

runbooks-demo: ## Apply the example demo team runbook
	kubectl apply -f examples/runbooks/demo-broken-app.yaml

test-rag: ## Verify runbook isolation live (payments private runbook never reaches demo)
	bash tests/rag/verify-rag.sh

test-gateway: ## Verify gateway auth, namespace isolation and NetworkPolicy
	bash tests/gateway/verify-gateway.sh

# ---- incidents (Stage 8) -------------------------------------------------
incidents: ## List persisted incidents (NS=demo)
	kubectl -n $(NS) get incidents.kubelantern.io

test-incidents: ## Verify incidents survive an agent restart and resolve (NS=demo, ~8 min)
	bash tests/incidents/verify-incidents.sh $(NS)

# ---- security ------------------------------------------------------------
test-rbac: ## Verify namespace isolation (payments vs orders)
	bash tests/rbac/verify-isolation.sh payments orders

# ---- charts / GitOps (Stage 8) -------------------------------------------
chart-lint: ## helm lint + template both charts with every CI values file
	@for c in charts/kubelantern-agent charts/kubelantern-ai; do \
	  $(HELM) lint $$c --strict || exit 1; \
	  for v in $$c/ci/*.yaml; do \
	    $(HELM) lint $$c --strict -f $$v || exit 1; \
	    $(HELM) template t $$c -n lint -f $$v >/dev/null || exit 1; \
	  done; \
	done; echo "charts OK"

egress-lockdown: ## Simulate default-deny egress in NS and redeploy its agent with the egress policy
	kubectl -n $(NS) apply -f deployments/kind/default-deny-egress.yaml
	$(MAKE) --no-print-directory deploy-agent NS=$(NS) EGRESS=true

egress-unlock: ## Undo egress-lockdown
	kubectl -n $(NS) delete -f deployments/kind/default-deny-egress.yaml --ignore-not-found
	$(MAKE) --no-print-directory deploy-agent NS=$(NS) EGRESS=false

migrate-to-helm: ## One-off: hand objects deployed before Stage 8 over to the Helm releases
	bash deployments/kind/migrate-to-helm.sh $(AI_NS) $(AGENT_NS)
