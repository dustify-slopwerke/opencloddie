#!/usr/bin/env bash
# .dustify-custom build hook used by ~/scripts/daily.sh and ~/scripts/push.sh.
#
# Builds the opencloddie image from deploy/Dockerfile (the Dockerfile is not at
# the repository root, which is why .dustify-auto does not fit), tags it with
# the short commit hash and latest, runs the test suite first, then pushes.
#
# Escape hatches:
#   SKIP_TESTS=1 ./build-and-push.sh
#   DRY_RUN=1    ./build-and-push.sh    # build only, no push
set -euo pipefail

NAME="${NAME:-$(basename "$PWD")}"
REGISTRY="${REGISTRY:-r.dustify.com}"
DOCKERFILE="${DOCKERFILE:-deploy/Dockerfile}"
IMAGE="$REGISTRY/$NAME"

COMMIT_HASH="unknown"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    COMMIT_HASH="$(git rev-parse --short HEAD)"
fi

echo "BUILD AND PUSH: $NAME ($COMMIT_HASH)"

if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
    echo ">> tests"
    ./scripts/test.sh
fi

echo ">> docker build"
docker build \
    -f "$DOCKERFILE" \
    -t "$IMAGE:$COMMIT_HASH" \
    -t "$IMAGE:latest" \
    .

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo ">> DRY_RUN=1: built $IMAGE:$COMMIT_HASH and $IMAGE:latest; not pushing"
    exit 0
fi

echo ">> docker push"
docker push "$IMAGE:$COMMIT_HASH"
docker push "$IMAGE:latest"

echo "done: $IMAGE:$COMMIT_HASH / $IMAGE:latest"
