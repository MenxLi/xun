.PHONY: build-web build-docker test doc doc-clean

build-web:
	cd web && npm i && npm run build

build-docker: build-web
	docker build -t xun -f docker/Dockerfile .

doc:
	xunc . \
		--env "XUN_AUTO_CONFIRM=true" \
		--exec "xun --non-interactive '请按照docs/AGENTS.md的要求制作文档'" \
		--name "xun-doc"

doc-clean:
	rm -rf site && rm -rf docs/ && git checkout -- docs/ && rm -rf mkdocs.yml

test:
	uv run python -m unittest discover -s test -t . -v