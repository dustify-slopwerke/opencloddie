> **Reference deployment.** This page documents the maintainers' daily.sh /
> edge-nginx pipeline as an example. Registry names, hostnames, and `~/compose`
> / `~/scripts` paths are placeholders — substitute your own. `build-and-push.sh`
> defaults to the maintainers' registry; set `REGISTRY=...` to override it.

# CI/CD integration (daily.sh pipeline)

opencloddie plugs into the existing `~/scripts/daily.sh` / `push.sh` / `up.sh`
pipeline without changes to those scripts.

## Build and push

The repository carries two markers understood by `daily.sh` and `push.sh`:

| File | Purpose |
| --- | --- |
| `.dustify-custom` | tells the pipeline to run `./build-and-push.sh` |
| `build-and-push.sh` | tests, builds `deploy/Dockerfile`, tags and pushes |

`.dustify-auto` is not used because the Dockerfile lives at
`deploy/Dockerfile`, not the repository root.

`build-and-push.sh`:

1. runs `./scripts/test.sh` (all 65 tests; `SKIP_TESTS=1` to skip),
2. builds `deploy/Dockerfile` from the repository root,
3. tags `registry.example.com/opencloddie:<short-sha>` and
   `registry.example.com/opencloddie:latest`,
4. pushes both (the daily script has already logged in to the registry).

`DRY_RUN=1 ./build-and-push.sh` builds locally without pushing.

The daily flow is therefore:

```
daily.sh
  ├─ edge: compose up + cert.sh + reload.sh
  ├─ registry: compose up
  ├─ docker login registry.example.com
  ├─ for ~/src/*:
  │    opencloddie -> build-and-push.sh
  │       test -> build -> push :<sha> and :latest
  └─ for ~/compose/*:
       opencloddie -> docker compose up --pull always -d
```

## Runtime stack

The live stack lives in `~/compose/opencloddie/compose.yml` (a separate
directory, so `daily.sh` picks it up automatically):

- image `registry.example.com/opencloddie:latest`
- no published ports; joins the external `edge` network
- internal web `:8787`, public API `:8788`
- `opencloddie-data` volume at `/data` (config, SQLite, logs)
- read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`

DNS: `cloddie.example.com` -> the edge origin.  The existing wildcard certificate
(`*.example.com`) already covers it; no certbot change is needed.

## Edge routing

`~/compose/edge/assets/nginx/config/opencloddie.conf` defines one server block:

| Path | Upstream | Auth |
| --- | --- | --- |
| `/v1/` | `opencloddie:8788` | API key only (public) |
| `/agent/` | `opencloddie:8788` | API key only (WebSocket) |
| `/client/` | `opencloddie:8788` | API key only |
| `/install` | `opencloddie:8788` | none (public installer) |
| `/` | `opencloddie:8787` | nginx `auth_request` -> Authelia, injects `Remote-User` |

Reload after changes:

```bash
docker exec edge-nginx-1 nginx -t
docker exec edge-nginx-1 nginx -s reload
```

## Authelia

The web UI requires an access-control rule because `default_policy` is `deny`:

```yaml
    - domain: cloddie.example.com
      policy: two_factor
      subject:
        - ['group:god']
```

The Authelia config directory is root-owned.  Use the existing helper before
editing:

```bash
sudo ~/compose/authelia/reset-chown.sh
```

Then validate and restart:

```bash
python3 -c "import yaml; yaml.safe_load(open('$HOME/compose/authelia/assets/authelia/configuration.yml'))"
docker compose -f ~/compose/authelia/compose.yml restart authelia
```

## First deploy / update / rollback

First deploy:

1. Ensure DNS for `cloddie.example.com` points at the edge.
2. Ensure the Authelia rule is present and Authelia restarted.
3. Run `~/scripts/daily.sh`, or manually:
   ```bash
   cd ~/src/opencloddie
   DRY_RUN=0 ./build-and-push.sh
   docker compose -f ~/compose/opencloddie/compose.yml up -d --pull always
   ```
4. Open `https://cloddie.example.com/`, sign in through Authelia, add the OpenCode
   key, create an API key, install the client.

Update: push to `main`; the next daily run tests, builds, pushes, and recreates
the container.  Agents reconnect and sessions flip back online within seconds.

Rollback: build/tag a previous commit or change the compose image tag to a
specific short SHA, then `docker compose up -d`.  Keep the `opencloddie-data`
volume.

## Operations

```bash
docker compose -f ~/compose/opencloddie/compose.yml ps
docker compose -f ~/compose/opencloddie/compose.yml logs -f
docker exec opencloddie-opencloddie-1 opencloddie-server user list
docker exec opencloddie-opencloddie-1 opencloddie-server key list --user alice
```

Back up the volume (contains `secrets.master` in `config.json`):

```bash
docker run --rm -v opencloddie_opencloddie-data:/data -v "$PWD":/backup \
  alpine tar czf /backup/opencloddie-backup.tgz -C /data .
```

Never commit `config.json`, the volume, or the OpenCode key.  The repository's
`.dockerignore`/`.gitignore` exclude runtime state.
