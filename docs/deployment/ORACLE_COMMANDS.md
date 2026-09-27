# Oracle Cloud Deployment Commands

> **Use `DEPLOYMENT_RUNBOOK.md` for real deploys.** This file keeps old one-off commands for reference.
> Two things here are STALE on the current prod container (see runbook):
> - `config.py` is **NOT bind-mounted** anymore — it lives in the image layer. Never overwrite it.
> - Never `docker rm`/recreate `job-agent` — it drops off `appnet` and nginx 502s.

## Prerequisites
- SSH key at `%USERPROFILE%\.ssh\oracle.key`
- Instance IP: `130.210.34.176`
- User: `ubuntu`

## One-liner Variables (copy into terminal first)

```powershell
$KEY = "$env:USERPROFILE\.ssh\oracle.key"
$HOST = "ubuntu@130.210.34.176"
```

## SSH into instance

```powershell
ssh -i $KEY $HOST
```

## SCP files to instance

```powershell
# Single file
scp -i $KEY backend/match_engine/resume_data.py $HOST:/home/ubuntu/job-agent/backend/match_engine/resume_data.py

# NOTE: config.py is BAKED into the image on prod (not bind-mounted). If you scp a copy to the
# host it does NOT affect the running app; do not docker cp it over the container config.
scp -i $KEY backend/config.py $HOST:/home/ubuntu/job-agent/backend/config.py
```

## Copy files into running container

> All app code is baked into the image layer. Update files with `docker cp`, then `docker restart`.
> The host copy under `/home/ubuntu/job-agent/backend/` is a staging/reference copy only.

```powershell
# Copy file into container
ssh -i $KEY $HOST "sudo docker cp /home/ubuntu/job-agent/backend/match_engine/resume_data.py job-agent:/app/backend/match_engine/resume_data.py"

# Restart container to pick up changes
ssh -i $KEY $HOST "sudo docker restart job-agent"
```

## Restart container

```powershell
ssh -i $KEY $HOST "sudo docker restart job-agent"
```

## Check container logs

```powershell
# Last 100 lines
ssh -i $KEY $HOST "sudo docker logs job-agent --tail 100 2>&1"

# Filter for prewarm
ssh -i $KEY $HOST "sudo docker logs job-agent --tail 100 2>&1 | grep PREWARM"

# Filter for errors
ssh -i $KEY $HOST "sudo docker logs job-agent --tail 100 2>&1 | grep -i 'failed\|error\|resume'"
```

## Verify Python imports inside container

```powershell
# Check resume_data loads without error
ssh -i $KEY $HOST "echo 'import match_engine.resume_data; print(repr(match_engine.resume_data.RESUME_TEXT))' | sudo docker exec -i job-agent python"

# Check relevance_engine import chain
ssh -i $KEY $HOST "echo 'from match_engine.relevance_engine import role_match_count; print(role_match_count(chr(112)+chr(121)+chr(116)+chr(104)+chr(111)+chr(110), [chr(100)+chr(97)+chr(116)+chr(97)]))' | sudo docker exec -i job-agent python"
```

## Edit config on server (sed)

```powershell
# Enable scheduler
ssh -i $KEY $HOST "sudo docker exec job-agent sed -i 's/SCHEDULER_ENABLED = False/SCHEDULER_ENABLED = True/' /app/backend/config.py"
```

## Check what's bind-mounted

```powershell
ssh -i $KEY $HOST "sudo docker inspect job-agent --format '{{json .Mounts}}'"
```

## Full rebuild + deploy flow

```powershell
# 1. Copy all backend files
scp -i $KEY -r backend/* $HOST:/home/ubuntu/job-agent/backend/

# 2. Copy files into running container (for baked files)
ssh -i $KEY $HOST "sudo docker cp /home/ubuntu/job-agent/backend/match_engine/resume_data.py job-agent:/app/backend/match_engine/resume_data.py"

# 3. Restart
ssh -i $KEY $HOST "sudo docker restart job-agent"

# 4. Verify
ssh -i $KEY $HOST "sudo docker logs job-agent --tail 20 2>&1"
```

## Notes
- `config.py` is **baked into the image** on the current prod container — a host copy at
  `/home/ubuntu/job-agent/backend/config.py` is a reference only and is NOT mounted. Never
  `docker cp` config over it; protect it like a secret.
- `job_agent.db` and data dirs (`resumes/`, `emails/`, `auto_apply/`, `cover_letters/`) live
  inside the container layer — never overwrite them in a deploy.
- `nginx.conf` IS bind-mounted from `/home/ubuntu/job-agent/nginx.conf` → edit + `nginx -s reload`.
- Container must stay attached to `appnet` — `docker restart` is safe; `docker rm`/recreate is not.
- Idle prevention cron runs `curl -s http://localhost:7860/health` every 5 min to prevent Oracle from reclaiming the instance
