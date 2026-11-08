PY ?= python3

.PHONY: help compile test init run client-install docker-build clean

help:
	@echo "opencloddie targets:"
	@echo "  make compile         byte-compile the Python sources"
	@echo "  make test            run the full automated suite (needs tmux)"
	@echo "  make init            create ~/.config/opencloddie/config.json"
	@echo "  make run             run the local web + API listeners"
	@echo "  make client-install  install the client (SERVER=https://...)"
	@echo "  make docker-build    build the deployment image"

compile:
	$(PY) -m py_compile opencloddie/*.py bin/opencloddie bin/opencloddie-server

test:
	./scripts/test.sh

init:
	$(PY) bin/opencloddie-server init

run:
	$(PY) bin/opencloddie-server run

client-install:
	./scripts/install-client.sh "$(SERVER)"

docker-build:
	docker build -f deploy/Dockerfile -t opencloddie:latest .

clean:
	rm -rf __pycache__ opencloddie/__pycache__ tests/__pycache__
	find . -name '*.pyc' -delete
