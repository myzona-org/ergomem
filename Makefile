# Ergo — convenience targets; this is just ergonomics so common actions are
# one word.
.DEFAULT_GOAL := help
.PHONY: help release release-dry test

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

release: ## Not included in this export (private deploy tooling)
	@echo "release is private deploy tooling, not included in the public export." >&2 && exit 1

release-dry: ## Not included in this export (private deploy tooling)
	@echo "release-dry is private deploy tooling, not included in the public export." >&2 && exit 1

test: ## Run the test suite (the release gate), same interpreter the release uses
	@.venv/bin/pytest tests/ -q
