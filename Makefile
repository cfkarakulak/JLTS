.PHONY: tag default

GIT_BRANCH := $(shell git rev-parse --abbrev-ref HEAD)
GIT_COMMIT := $(shell git rev-parse --short HEAD)
GIT_LATEST_TAG := v$(GIT_COMMIT)@$$(date +%Y.%m.%d-%H.%M.%S)

default: tag

help:
	@echo 'Management commands for JL2:'
	@echo
	@echo 'Usage:'
	@echo '    make tag             Create and push a checkpoint tag to GitHub repository
	@echo

tag:
	@echo "Pushing tag $(GIT_LATEST_TAG) for $(GIT_BRANCH):$(GIT_COMMIT)"
	git tag $(GIT_LATEST_TAG)
	git push origin $(GIT_LATEST_TAG)
