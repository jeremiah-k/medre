# Package changes rebuild an image. Configuration changes recreate a container.
PYTHON ?= python3
DOCKER_COMPOSE ?= docker compose
IMAGE ?= medre:local
ADAPTERS ?= all
SOURCE ?= git
# Extra build options, e.g. --sources /path/to/sources.yaml or --local matrix=../fork.
BUILD_OPTIONS ?=
export MEDRE_IMAGE := $(IMAGE)

.PHONY: help build build-nocache pull run stop logs shell check adapters verify clean
help:
	@echo 'build [ADAPTERS="matrix meshtastic" SOURCE=git|index BUILD_OPTIONS="..."]'
	@echo 'pull run stop logs shell check adapters verify clean (clean retains state)'
build:
	$(PYTHON) scripts/docker/build.py --tag "$(IMAGE)" --adapters "$(ADAPTERS)" --source "$(SOURCE)" $(BUILD_OPTIONS)
build-nocache:
	$(MAKE) build BUILD_OPTIONS="$(BUILD_OPTIONS) --no-cache"
pull:
	$(DOCKER_COMPOSE) pull
run:
	$(DOCKER_COMPOSE) up -d --force-recreate
stop:
	$(DOCKER_COMPOSE) stop
logs:
	$(DOCKER_COMPOSE) logs -f --tail=100
shell:
	$(DOCKER_COMPOSE) run --rm --no-deps --entrypoint sh medre
check:
	$(DOCKER_COMPOSE) run --rm --no-deps medre config check
adapters:
	$(DOCKER_COMPOSE) run --rm --no-deps medre adapters
verify:
	docker run --rm --network none --read-only --tmpfs /tmp --entrypoint python "$(IMAGE)" /opt/verify-image.py
clean:
	$(DOCKER_COMPOSE) down
