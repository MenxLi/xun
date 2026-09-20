.PHONY: build-web build-docker test

build-web:
	cd web && npm i && npm run build

build-docker: build-web
	docker build -t xun -f docker/Dockerfile .

test:
	uv run python -m unittest discover -s test -t . -v