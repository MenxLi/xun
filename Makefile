
sandbox-base:
	docker build -t xun-base:latest -f docker/base.Dockerfile .

sandbox:
	DOCKER_BUILDKIT=0 docker build -t xun:latest -f docker/xun.Dockerfile .