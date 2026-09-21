.PHONY: build-web build-docker test doc doc-dist doc-clean

build-web:
	cd web && npm i && npm run build

build-docker: build-web
	docker build -t xun -f docker/Dockerfile .

doc:
	xunc . \
		--env "XUN_AUTO_CONFIRM=true" \
		--exec "xun --non-interactive '请按照docs/AGENTS.md的要求制作文档'" \
		--name "xun-doc"

doc-dist:
	@if [ ! -f "site/index.html" ]; then \
		echo "Documentation not generated; skipping distribution."; \
	else \
		echo "Copying generated documentation to src/xun/assets/docs/"; \
		rm -rf src/xun/assets/docs/*; \
		cp -r site/* src/xun/assets/docs/; \
	fi

doc-clean:
	if [ -d "site" ]; then \
		rm -rf site; \
	fi \
	&& rm -rf docs/ && git checkout -- docs/ && \
	if [ -f "mkdocs.yml" ]; then \
		rm -f mkdocs.yml; \
	fi

test:
	uv run python -m unittest discover -s test -t . -v