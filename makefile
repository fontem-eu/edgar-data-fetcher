
all: build release deploy

build:
	docker pull python:3.12-slim
	docker build -t contribute.void42.internal/golden/edgar-data-fetcher:$(shell git rev-parse --short HEAD) .

release:
	docker push contribute.void42.internal/golden/edgar-data-fetcher:$(shell git rev-parse --short HEAD)

deploy:
	helm upgrade --install edgar-data-fetcher ./deployment --set-string version=$(shell git rev-parse --short HEAD)
