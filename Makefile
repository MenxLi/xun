.PHONY: build-web build-docker test doc doc-dist doc-clean

DOC_PATH := src/xun/assets/docs

build-web:
	cd web && npm i && npm run build

build-docker: build-web
	docker build -t xun -f docker/Dockerfile .

doc:
	xunc . \
		--env "XUN_AUTO_CONFIRM=true" \
		--exec "xun --non-interactive '请按照docs/AGENTS.md的要求制作文档'" \
		--name "xun-doc" \
		--port ""

doc-dist:
	@if [ ! -f "site/index.html" ]; then \
		echo "Documentation not generated; skipping distribution."; \
	else \
		echo "Copying generated documentation to $(DOC_PATH)/"; \
		if [ ! -d "$(DOC_PATH)" ]; then \
			mkdir -p $(DOC_PATH); \
		fi; \
		rm -rf $(DOC_PATH)/*; \
		cp -r site/* $(DOC_PATH)/; \
	fi

doc-clean:
	if [ -d "site" ]; then \
		rm -rf site; \
	fi \
	&& rm -rf $(DOC_PATH) && git checkout -- $(DOC_PATH); \
	if [ -f "mkdocs.yml" ]; then \
		rm -f mkdocs.yml; \
	fi

test:
	uv run python -m unittest discover -s test -t . -v