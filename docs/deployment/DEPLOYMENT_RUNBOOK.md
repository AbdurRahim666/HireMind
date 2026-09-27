# PROD Deployment Runbook — job-agent / jobawn.com

> Keep prod working: **`docker cp` + `docker restart`**. Nothing else. Every incident below
> (readonly DB, 502 Bad Gateway) came from rebuilding/recreating the container instead.

## Topology (what really runs)

| Piece | Details | Notes |
|---|---|---|
| `job-agent` container | Code + **all runtime data** live in its writable layer. **NO volume mounts.** Runs as OS user `user` (uid 1000). Forwards `127.0.0.1:7860`. | `config.py`, `job_agent.db`, `resumes/`, `emails/`, `auto_apply/`, `cover_letters/` all live HERE, in the container. |
| `nginx` container | Reverse proxy: `https://jobawn.com` → `http://job-agent:7860`. Its config is a **bind-mount** from host `/home/ubuntu/job-agent/nginx.conf`. | On shared bridge network **`appnet`**. nginx resolves the name `job-agent` **once at start/reload** and caches the IP. |
| host staging | `/home/ubuntu/job-agent/` | extract bundles here, then `docker cp` into the container. |

Network on `appnet` (172.18.0.0/16): `job-agent` = 172.18.0.2, `nginx` = 172.18.0.3.

## Golden rules

1. **Deploy = copy files in + `docker restart`.** Never `docker rm`, `docker recreate`,
   `docker commit`, or `docker network disconnect` for a routine deploy. Recreating the
   container drops it off `appnet` → nginx 502.
2. **Never put these in a deploy bundle:** `backend/config.py`, `*.db`, `*.bak`,
   `backend/resume.txt`, `resumes/`, `emails/`, `auto_apply/`, `cover_letters/`, `data/`,
   `env/`, `backup/`, `__pycache__/`, `.pytest_cache/`.
3. **Copied files must be owned `1000:1000` (user).** The app runs as uid 1000 and SQLite
   WAL needs to write `-wal` files into `/app/backend`. Files copied in as another uid are
   read-only for the app → boot fails. Force ownership at extract time (below).

## Prerequisites

```powershell
$KEY  = "$env:USERPROFILE\.ssh\oracle.key"
$HOST = "ubuntu@130.210.34.176"
$R    = "/home/ubuntu/job-agent"
```

## Standard deploy (safe path)

### 1. Build the bundle locally (Windows `tar` = bsdtar)

From the repo root, `--exclude` must match the golden rules exactly relative to `backend/**`/`frontend/**`:

```powershell
tar -czf "$env:TEMP\jobagent_deploy.tar.gz" `
  backend --exclude="backend/config.py" --exclude="*.db" --exclude="*.bak" `
    --exclude="backend/resume.txt" --exclude="backend/resumes" --exclude="backend/emails" `
    --exclude="backend/auto_apply" --exclude="backend/cover_letters" --exclude="backend/data" `
    --exclude="backend/env" --exclude="backend/__pycache__" --exclude="backend/.pytest_cache" `
  frontend --exclude="frontend/backup"
```

### 2. Upload + extract **as ubuntu with forced ownership 1000:1000**

```powershell
scp -i $KEY "$env:TEMP\jobagent_deploy.tar.gz" $HOST:$R/jobagent_deploy.tar.gz
ssh -i $KEY $HOST "rm -rf $R/stage && mkdir -p $R/stage && cd $R/stage && \
  tar -xzf $R/jobagent_deploy.tar.gz --owner=1000 --group=1000"
```

`--owner=1000 --group=1000` is the single most important line: files then arrive in the
container owned by `user:user`, so the app can write. (Extracting as root reintroduces the
readonly-DB bug.)

### 3. Sanity-check the bundle BEFORE copying

```bash
# On host — must print nothing:
find "$R/stage" -name "config.py" -o -name "*.db" | grep . && echo "FORBIDDEN FILES" && exit 1
```

### 4. Copy into the container

```powershell
ssh -i $KEY $HOST "sudo docker cp $R/stage/backend job-agent:/app/"
ssh -i $KEY $HOST "sudo docker cp $R/stage/frontend job-agent:/app/"
```

### 5. Verify inside the container (before restart)

```powershell
ssh -i $KEY $HOST "sudo docker exec job-agent sh -c 'ls -l /app/backend/config.py /app/backend/job_agent.db; md5sum /app/backend/config.py'"
```

- `config.py` **must still be present** and unchanged (compare md5 with the value you
  captured before step 4).
- `job_agent.db` must still be ~225 MB and untouched.
- New files should show owner `user`.

### 6. Restart + smoke test

```powershell
ssh -i $KEY $HOST "sudo docker restart job-agent && sleep 10 && sudo docker logs job-agent --tail 30 2>&1"
```

Expected in logs: `Application startup complete`, `[PREWARM] Scheduler started`,
`[PROXY-POOL] always-on refresher started`. **No** `readonly database`/`OperationalError`.

Then verify end-to-end (Host → nginx → container):

```powershell
ssh -i $KEY $HOST "curl -sk -o /dev/null -w '%{http_code}' --resolve jobawn.com:443:127.0.0.1 https://jobawn.com/api/countries-used"
ssh -i $KEY $HOST "sudo docker logs nginx --since 30s 2>&1 | grep -c ' 502 '"
```

- HTTPS through nginx → `200`
- `grep -c ' 502 '` → `0` (any count > 0 = stale upstream, see incident below)

## Incidents

### A. App won't boot: `attempt to write a readonly database`

Cause: `/app/backend` (or `job_agent.db`) not writable by uid 1000 — usually files were
extracted/copied as another uid. Fix ownership, and **do not forget the network step**:

```bash
IMG=job-agent:deploy-ownfix-2026-09-27     # current image, correct ownership baked in
sudo docker stop job-agent 2>/dev/null || true
sudo docker commit job-agent job-agent:temp-$$                # keep a rollback point
sudo docker rm job-agent-fix 2>/dev/null || true
sudo docker run --name job-agent-fix --user root --entrypoint /bin/sh "$IMG" \
  -c "chown -R 1000:1000 /app/backend /app/frontend && echo FIXED"
sudo docker commit job-agent-fix job-agent:fixed
sudo docker rm job-agent-fix
sudo docker rm job-agent
sudo docker create --name job-agent --restart unless-stopped \
  -p 127.0.0.1:7860:7860 \
  --user user --workdir /app/backend --entrypoint "" \
  job-agent:fixed uvicorn api.main:app --host 0.0.0.0 --port 7860
sudo docker network connect appnet job-agent       # **MUST DO** — else nginx 502
sudo docker start job-agent
sudo docker exec nginx nginx -s reload             # refresh nginx's cached upstream IP
```

Note the manual `docker create` must restore the exact original metadata (USER/PORTS/
WORKDIR/CMD, `--entrypoint ""` to clear the fix container's entrypoint).

### B. Everything is up but site returns **502 Bad Gateway**

Cause: nginx proxy target `http://job-agent:7860` resolved once at start/reload to an IP
that no longer exists (container recreated without `appnet`). Symptom in nginx logs:
`connect() failed (113: Host is unreachable) ... upstream: "http://172.18.0.2:7860..."`.

```bash
sudo docker network connect appnet job-agent       # re-attach if missing (verify via docker inspect)
sudo docker inspect job-agent --format '{{range .NetworkSettings.Networks}}{{.NetworkID}} {{.IPAddress}} {{end}}'   # expect appnet=172.18.0.2
sudo docker exec nginx nginx -s reload
curl -sk -o /dev/null -w '%{http_code}\n' --resolve jobawn.com:443:127.0.0.1 https://jobawn.com/   # expect 200
```

## Storage & rollback map

| Data | Where it lives | Backups |
|---|---|---|
| `job_agent.db`, `resumes/`, `emails/`, `auto_apply/`, `cover_letters/` | inside `job-agent` container (writable layer), NOT on host | `sudo docker export job-agent -o $R/backup_<date>.tar`; current rollback images: `job-agent:deploy-ownfix-2026-09-27` (current), `job-agent:fix-live-2026-09-27` (pre-fix, 2026-09-27) |
| `config.py` | baked into image layer — never in bundle; only a reference copy on host `$R/backend/config.py` | copy is NOT mounted; editing it does nothing to the running app |
| `nginx.conf` | host file `$R/nginx.conf`, bind-mounted RO into nginx | edit on host, then `docker exec nginx nginx -s reload` |
| images | `job-agent:deploy-ownfix-2026-09-27` (current) · `job-agent:fix-live-2026-09-27` · `job-agent:snapshot-2026-09-07` · `job-agent:latest` | rollback by creating a container from a snapshot image **+ `docker network connect appnet`** |

## Reminders (why these rules exist)

- `config.py` is **baked**, not bind-mounted, on this container (verified 2026-09-27; no
  mounts on `job-agent`). Older docs/scripts claiming a bind-mount are stale.
- 502 on 27-Sep-2026 was caused by recreating the container (dropped off `appnet` while
  nginx kept its cached IP). Fix was `docker network connect appnet job-agent` + `nginx -s reload`.
- Readonly DB on 27-Sep-2026 was caused by files landing with the wrong ownership; root
  cause removed from the flow by `--owner=1000 --group=1000` at extraction.