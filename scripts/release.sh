#!/usr/bin/env bash
# Create the annotated release tag after running the full test suite.
#
#   ./scripts/release.sh            # create the tag locally
#   ./scripts/release.sh --push     # create and push it (triggers GitHub release)
set -euo pipefail
cd "$(dirname "$0")/.."

version="$(python3 -c 'import opencloddie; print(opencloddie.__version__)')"
tag="v${version}"
REMOTE="${REMOTE:-origin}"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "error: working tree is not clean; commit or stash changes first" >&2
    git status --short >&2
    exit 1
fi
if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
    echo "error: tag ${tag} already exists" >&2
    exit 1
fi
for required in LICENSE CHANGELOG.md pyproject.toml; do
    [[ -f "$required" ]] || { echo "error: missing $required" >&2; exit 1; }
done
if ! grep -q "^## \[${version}\]" CHANGELOG.md; then
    echo "error: CHANGELOG.md has no ## [${version}] entry" >&2
    exit 1
fi

echo "== tests =="
./scripts/test.sh

git tag -a "$tag" -m "opencloddie ${version}"
echo "created ${tag}"

if [[ "${1:-}" == "--push" ]]; then
    git push "$REMOTE" "$tag"
    echo "pushed ${tag} to ${REMOTE}; the GitHub release workflow will build GHCR + wheel + client"
else
    echo "push it with: git push ${REMOTE} ${tag}"
fi
