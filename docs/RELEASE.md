# Releasing

opencloddie uses [Semantic Versioning](https://semver.org/) and GitHub Releases.
The current release version is **0.2.0**.

## Version sources

The version lives in exactly one place — `opencloddie/__init__.py`:

```python
__version__ = "0.2.0"
```

- `pyproject.toml` reads it dynamically.
- `opencloddie/server.py` and `opencloddie/httpd.py` import it.
- `tests/test_version.py` checks that the standalone client's `VERSION` matches.
- The release workflow refuses a tag unless `GITHUB_REF_NAME == f"v{__version__}"`.

To bump:

1. Update `opencloddie/__init__.py`.
2. Update `VERSION` in `opencloddie/client/opencloddie`.
3. Add a `CHANGELOG.md` entry (Keep a Changelog format).
4. Run `./scripts/test.sh` (the version test catches mismatches).
5. Commit.

## Pre-release checklist

- [ ] `./scripts/test.sh` passes (65 tests).
- [ ] `python3 -m build` produces a wheel and sdist.
- [ ] `python3 -m pip install --force-reinstall dist/*.whl && opencloddie-server --version`
- [ ] `docker build -f deploy/Dockerfile -t opencloddie:test .`
- [ ] `docker run ... opencloddie:test` is healthy and `/install/opencloddie` returns the client.
- [ ] `CHANGELOG.md` has the release date and entries.
- [ ] No secrets, private hostnames, or personal paths in the diff.
- [ ] `git status` is clean and the commit is pushed.

## Tag and release

Use the helper after the checklist:

```bash
./scripts/release.sh          # tests + local annotated tag
./scripts/release.sh --push   # push the tag to origin (triggers the release workflow)
REMOTE=github ./scripts/release.sh --push   # or target the GitHub remote
```

```bash
git tag -a v0.2.0 -m "opencloddie 0.2.0"
git push origin v0.2.0
```

`.github/workflows/release.yml` then:

1. verifies the tag matches the package version,
2. runs the tests,
3. builds the wheel, sdist, and a standalone `opencloddie` client,
4. builds a multi-arch (`linux/amd64`, `linux/arm64`) image and pushes it to
   GHCR,
5. attests the image build provenance,
6. creates the GitHub Release with generated notes and the artifacts.

## Published artifacts

| Artifact | Location |
| --- | --- |
| Container image | `ghcr.io/dustify-slopwerke/opencloddie:0.2.0`, `:0.2`, `:latest` |
| Wheel | `opencloddie-0.2.0-py3-none-any.whl` on the release |
| Source distribution | `opencloddie-0.2.0.tar.gz` on the release |
| Source archives | GitHub's automatic `zip`/`tar.gz` |
| Client script | `opencloddie` on the release (also served by every server at `/install/opencloddie`) |

GHCR packages start **private**.  After the first release, open the package
settings in GitHub and set its visibility to public if the world should pull it.

## Install the release

```bash
docker pull ghcr.io/dustify-slopwerke/opencloddie:0.2.0
docker run -d --name opencloddie \
  -p 127.0.0.1:8787:8787 -p 127.0.0.1:8788:8788 \
  -v opencloddie-data:/data \
  ghcr.io/dustify-slopwerke/opencloddie:0.2.0
```

Or install the wheel attached to the release:

```bash
python3 -m pip install \
  https://github.com/dustify-slopwerke/opencloddie/releases/download/v0.2.0/opencloddie-0.2.0-py3-none-any.whl
opencloddie-server init
opencloddie-server run
```

The client is installed from a running server, not PyPI:

```bash
curl -fsSL https://cloddie.example.com/install/opencloddie -o ~/.local/bin/opencloddie
chmod +x ~/.local/bin/opencloddie
```

## Verify

```bash
# image tags and platforms
docker buildx imagetools inspect ghcr.io/dustify-slopwerke/opencloddie:0.2.0

# provenance (GitHub CLI)
gh attestation verify oci://ghcr.io/dustify-slopwerke/opencloddie:0.2.0 --owner dustify-slopwerke

# image health
docker run --rm --entrypoint opencloddie-server ghcr.io/dustify-slopwerke/opencloddie:0.2.0 --version

# release assets
gh release view v0.2.0 --repo dustify-slopwerke/opencloddie
```

## Rollback

- Pin deployments to an immutable tag (`:0.2.0` or a digest), not `:latest`.
- To roll back, redeploy the previous tag and keep the same `/data` volume.
- Do not delete a published release/tag if users may have pinned it; publish a
  patch instead.

## GitHub repository settings

Recommended once, after creating the repository:

- Description: “Anthropic-compatible gateway and tabbed terminal relay for
  Claude Code, with per-user OpenCode Go keys.”
- Topics: `claude-code`, `anthropic`, `opencode`, `tmux`, `terminal`, `gateway`,
  `python`, `docker`.
- Enable **Private vulnerability reporting** (Security → Advisories).
- Enable Dependabot alerts/security updates (`.github/dependabot.yml`).
- Require CI on `main`; disallow force pushes.
- Enable branch protection and require the `CI / test` check.
- Set the GHCR package visibility to public after the first release.
- When forking, replace `dustify-slopwerke` in the README badges/install
  commands, `pyproject.toml`, `SECURITY.md`, `CHANGELOG.md`,
  `.github/ISSUE_TEMPLATE/config.yml`, `docs/RELEASE.md`, and
  `deploy/Dockerfile` with your GitHub owner.

## Compatibility policy

While pre-1.0, the HTTP gateway tracks the upstream Anthropic Messages API and
the public `/v1`, `/client`, and `/agent` contracts may still change in minor
releases.  The agent and terminal WebSocket protocols are internal and should
be upgraded client/server together.  Document breaking changes in
`CHANGELOG.md`.
