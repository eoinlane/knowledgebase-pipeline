UBUNTU = eoin@nvidiaubuntubox
PIPELINE_REMOTE = ~/knowledgebase-pipeline
SHELL := /bin/bash

.PHONY: test test-all deploy-ubuntu deploy-mac clean-ubuntu help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

test: ## Run fast tests (no SSH, no LLM)
	pytest tests/ --ignore=tests/test_integration.py

test-all: ## Run all tests including Ubuntu SSH + LLM
	pytest tests/ --run-slow

deploy-ubuntu: ## Deploy Ubuntu scripts + shared module to nvidiaubuntubox
	rsync -az --delete ubuntu/ $(UBUNTU):$(PIPELINE_REMOTE)/ubuntu/
	rsync -az --delete shared/ $(UBUNTU):$(PIPELINE_REMOTE)/shared/
	@echo "Symlinking scripts to ~/..."
	ssh $(UBUNTU) 'bash -c "for f in $(PIPELINE_REMOTE)/ubuntu/*.py; do ln -sf \$$f ~/; done && for f in $(PIPELINE_REMOTE)/ubuntu/*.sh; do ln -sf \$$f ~/; done && echo Deployed"'

deploy-mac: ## Symlink Mac scripts to ~/
	ln -sf $(PWD)/mac/build_knowledge_base.py ~/build_knowledge_base.py
	ln -sf $(PWD)/mac/build_contacts_db.py ~/build_contacts_db.py
	ln -sf $(PWD)/mac/apply_kb_corrections.py ~/apply_kb_corrections.py
	ln -sf $(PWD)/mac/entity_resolution.py ~/entity_resolution.py
	ln -sf $(PWD)/mac/upload_knowledge_base_incremental.py ~/upload_knowledge_base_incremental.py
	@for f in mac/launchd/*.sh; do ln -sf $(PWD)/$$f ~/.local/bin/$$(basename $$f); done
	@echo "Mac scripts symlinked"

clean-ubuntu: ## Remove dead scripts from Ubuntu ~/
	ssh $(UBUNTU) 'rm -f ~/final4.py ~/final_batch2.py ~/run_faster_whisper.py \
		~/run_transcribe_only.py ~/run_whisperx_gpu.py ~/transcribe_new_diarize.py \
		~/transcribe_new.py && echo "Dead scripts removed"'

build-kb: ## Run KB build on Mac
	cd ~ && python3 build_knowledge_base.py

benchmark: ## Run benchmark (usage: make benchmark MODEL=qwen2.5:14b)
	python3 tools/benchmark_models.py --model $(MODEL)
