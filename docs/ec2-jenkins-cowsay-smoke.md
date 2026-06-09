# Real-Jenkins cowsay smoke (S17a + S17b — RAN GREEN 2026-06-09)

> **Status: PASSED — fake (S17a) AND live BYOK Haiku (S17b).**
>
> **S17a (fake agent):** a restored EC2 Jenkins built the cowsay `feature/modelmatch-smoke`
> branch; the `ModelMatch AI Review` stage ran the CI-Agent (`modelmatch-agent`,
> `LLM_CLIENT=fake`) on the diff vs `master` → `POST /projects/1/ci-runs` → **201** → run
> on the dashboard. Automated halves: Playwright happy path
> (`modelmatch-frontend/e2e/happy-path.spec.ts`, hermetic) + local real-stack ingest
> (`e2e/real-stack.smoke.spec.ts`).
>
> **S17b (LIVE, real money):** a **fresh CI-Agent (project id 2)** created via the UI,
> Haiku picked as the selected model. Anthropic key added as the Jenkins
> `modelmatch-model-api-key` Secret-text credential; the stage flipped to
> `LLM_CLIENT=anthropic` (`ANTHROPIC_API_KEY` bound from the credential, passed to docker
> **by name**). Build #7 → `api.anthropic.com 200`, **3406 in / 486 out** tokens →
> `POST /projects/2/ci-runs` → **201** (`id:4`, actual **$0.005836** vs Sonnet baseline
> **$0.017508** *computed*, **savings ~67%**, gate **fail** = 3 blocking, **5 findings**).
> ≈ **$0.012** Anthropic spend total. The **response-rating UI** then banked it: rating
> 4 accept / 1 reject → **80% = threshold** → Cumulative saved **$0 → $0.0117**.
>
> **AWS = real money (`ap-south-1`).** The two instances auto-stop at 1AM; they carry
> **Elastic IPs** (stable URLs) + `restart: unless-stopped` (auto-start on restart). See
> **§ As-built** for the live IDs/URLs and **§ Teardown** to destroy. **As of 2026-06-09 the
> env is KEPT RUNNING** for demo rehearsal; tear down when done.

> **TODO (S18 / pipeline design):** the **backend** CI pipeline needs a **Publish-to-ECR
> stage** (build → test → push `modelmatch-backend` to ECR) so deploys pull a real registry
> image instead of the hand-pushed `:smoke` tags used here. Same for the FE image. The
> S17a/S17b images were built locally (`docker buildx --platform linux/amd64 --push`) and
> pulled on the boxes — fine for a smoke, but the pipeline should own the publish.

## What this proves

```
 real PR on cowsay ──▶ Jenkins multibranch build ──▶ ModelMatch AI Review stage
   (a code change)        (the user's CI)            (agent reviews the diff, BYOK)
                                                              │
                                                              ▼
                                   POST /projects/{id}/ci-runs  (per-project X-CI-Token)
                                                              │
                                                              ▼
                                   ModelMatch dashboard: the run + savings + quality gate
```

- **Agent demo spectrum (locked in the umbrella CLAUDE.md):** runs on **Steve's own
  Anthropic key** — **Haiku live**, **Sonnet baseline computed** (`tokens × price`, not
  run). Gemini free-tier optional as a 3rd vendor; **only on throwaway/demo code** (it
  trains on inputs).
- The **review + pass/fail gate stay in CI**; the agent never edits the repo.

## As-built architecture (what actually ran)

Two **separate** EC2 instances — the ModelMatch app is an **independent unit** the user's
Jenkins reaches over the network (NOT co-located with Jenkins):

```
   ┌─────────────────────── VPC (ap-south-1, default) ───────────────────────┐
   │                                                                          │
   │  EC2 #1  Jenkins (restored from snapshot)        EC2 #2  ModelMatch app  │
   │  ─ jenkins-jenkins-1 (docker compose)            ─ docker compose up:    │
   │  ─ builds cowsay feature branch                    • frontend (nginx)    │
   │  ─ stage: docker run modelmatch-agent ──┐          • backend  (gunicorn) │
   │                                          │         • db       (postgres) │
   │   curl POST  ────────────────────────────┼────▶  http://<app-priv-ip>:8000/projects/1/ci-runs
   │   (X-CI-Token, over the VPC priv IP)      │                              │
   └──────────────────────────────────────────┼──────────────────────────────┘
                                               │
        browser ──▶ http://<app-EIP>:8080 (FE) ┘  config.js → http://<app-EIP>:8000 (BE)
```

### Three images, pushed to **ECR**, pulled on the app EC2

| Image | Built from | ECR repo (`<acct>.dkr.ecr.ap-south-1.amazonaws.com/…`) | Run as |
|---|---|---|---|
| **backend** | `modelmatch-backend/Dockerfile` (now **carries migrations** → self-contained) | `modelmatch-backend:smoke` | gunicorn + a one-off `migrate` step |
| **frontend** | `modelmatch-frontend/Dockerfile` (multi-stage → non-root nginx, `config.js` injected at start) | `modelmatch-frontend:smoke` | nginx :8080 |
| **agent** | `modelmatch-backend/agent/Dockerfile` (context = backend root) | `modelmatch-agent:smoke` (tagged `modelmatch-agent:latest` on the Jenkins host) | `docker run` inside the stage |

**db** is stock `postgres:16` from Docker Hub (not mirrored to ECR — it's a stock image).

Images are built **`--platform linux/amd64`** (the Mac is arm64) and pushed straight from
`docker buildx --push`. On the app EC2: `aws ecr get-login-password | docker login …`
(short-lived token, piped over SSH — no long-lived keys on the box), then
`docker compose pull && up -d`.

### `modelmatch-frontend/docker-compose.yaml` — the unified FE+BE+DB stack

One file (in the **frontend** repo, per the original S17 plan) brings the whole app up
from images: `db` → a one-off **`migrate`** service (the backend image running
`alembic upgrade head` + catalog seed, then exits) → `backend` (waits on migrate) →
`frontend`. Image refs + URLs come from a `.env` beside it:

```
BACKEND_IMAGE / FRONTEND_IMAGE   the ECR refs (default: locally-built names)
JWT_SECRET                       required (generated on the box)
PUBLIC_BASE_URL=http://<app-priv-ip>:8000   what the CI agent's ci-runs URL targets (VPC)
API_BASE_URL=http://<app-EIP>:8000          what the browser (SPA) calls
CORS_ALLOW_ORIGINS=http://<app-EIP>:8080    the SPA origin the backend allows
LLM_CLIENT=fake                  in-cluster LLM off for the smoke ($0)
```

> **Key split:** `PUBLIC_BASE_URL` is the app's **private** IP (Jenkins → backend over the
> VPC); `API_BASE_URL`/CORS use the app's **public/EIP** (browser → backend). They differ.

### As-built IDs / URLs (2026-06-09)

| Thing | Value |
|---|---|
| Jenkins EC2 / EIP / UI | `i-0f701eeb64a1d2bbd` · `15.206.12.120` · `http://15.206.12.120:8080` |
| App EC2 / EIP | `i-02a90abddaf87baa3` · `13.126.189.239` (FE `:8080`, BE `:8000`) |
| App private IP (ingest) | `172.31.8.53` → `http://172.31.8.53:8000/projects/1/ci-runs` |
| Restore snapshot | `snap-092629b3efa025887` → registered AMI `ami-03cfe8d7787b8eb3c` |
| SGs | Jenkins `sg-0d07eac0533f8e08f` · app `sg-0f7bf710e9c7b6345` (8000 from Jenkins SG + my IP; 22/8080 from my IP) |
| EIP allocs (release at teardown) | `eipalloc-0a69f97024b95cae6` (jenkins) · `eipalloc-06797243f74f53cbd` (app) |
| cowsay smoke branch | `feature/modelmatch-smoke` on `git@gitlab.com:stevelevit230-group/cowsay.git` |

### Bugs the smoke surfaced (all fixed in-branch)

1. **`jq` missing** on the Jenkins node — the stage needs it (documented prereq); installed.
2. **Migrations weren't in the backend image** — folded into the real `Dockerfile` (the
   image now self-migrates via the compose `migrate` step / a K8s Job).
3. **FE image: non-root nginx couldn't write `config.js`** → container failed to start;
   fixed by `chown 101 /usr/share/nginx/html` in the FE `Dockerfile`.
4. **Generated agent snippet uses `-v $PWD:/work`** — breaks when Jenkins runs in a
   container (the `$PWD` is a *container* path the sibling agent can't bind-mount). The
   robust form is **stdin** (`docker run -i … < pr.diff`); the snippet generator should
   prefer it.
5. **Diff target hardcoded `main`** — cowsay's default branch is `master`; the stage must
   fall back to the repo's real default.
6. **`jenkinsBuildId` `%2F`** — multibranch `BUILD_TAG` URL-encodes the branch slash, and
   the backend's `jenkinsBuildId` pattern rejects `%`; sanitize (`%2F → -`) or widen the
   pattern.
7. **(ops)** restoring Jenkins from a snapshot leaves a **stale root URL** → the dark-theme
   CSS loads from the old IP → blank UI. Restart Jenkins (re-reads the root URL) or disable
   the theme (IP-independent — what we did). The daily-stop IP churn is why we attached EIPs.

## The app under test — cowsay

`~/bootcamp/jenkins/task-2-cowsay-pipeline/cowsay-solution/` (a complete Flask cowsay
app: `Dockerfile` + `Jenkinsfile` + `src/` + `version.txt`). Multibranch/versioned
variants exist at `task-4-cowsay-multi-branch/cowsay/` and
`task-3-cowsay-versioned/cowsay/` if a PR/branch flow is preferred.

> The committed `Jenkinsfile` is GitLab-wired (`gitlabConnection`, a hardcoded
> `COWSAY_HOST`). For the smoke you only need **Build → Test → ModelMatch AI Review**;
> drop or stub the Publish/deploy stages and the GitLab trigger.

## Open item — RESOLVED: restore source

There is **no self-owned AMI** in `ap-south-1`, but there is a Jenkins **snapshot**:

| Field | Value |
|---|---|
| Snapshot ID | `snap-092629b3efa025887` |
| Description | `jenkins-ec2-backup-2026-04-14` |
| Size | 30 GiB |
| Region | `ap-south-1` |

So the restore is **snapshot → register AMI → launch** (not a ready AMI). Verify the
root device name before registering (Ubuntu shows `/dev/sda1`).

---

## Step 0 — preflight (free, read-only; safe to run now)

```bash
aws sts get-caller-identity --region ap-south-1
aws ec2 describe-snapshots --owner-ids self --region ap-south-1 \
  --query 'Snapshots[?SnapshotId==`snap-092629b3efa025887`]'
```

## Step 1 — restore Jenkins from the snapshot  ⚠️ COSTS MONEY — needs Steve's go

```bash
# 1a. Register an AMI from the Jenkins snapshot (verify RootDeviceName first).
aws ec2 register-image --region ap-south-1 \
  --name "jenkins-restore-$(date +%Y%m%d)" \
  --root-device-name /dev/sda1 \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"SnapshotId":"snap-092629b3efa025887","VolumeSize":30,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --architecture x86_64 --virtualization-type hvm --ena-support
# → AMI id, e.g. ami-xxxx

# 1b. Launch t3a.medium, 30 GiB gp3 (umbrella CLAUDE.md cross-cutting rules).
#     SG must allow inbound 22 (SSH) + 8080 (Jenkins UI). Port 8000 (the ModelMatch
#     backend) only needs an inbound rule if Jenkins reaches it over the EIP/public IP;
#     if the backend runs on THIS SAME host and the agent curls localhost/private IP
#     (Step 2, recommended), no inbound 8000 rule is needed.
aws ec2 run-instances --region ap-south-1 \
  --image-id ami-xxxx --instance-type t3a.medium \
  --key-name <your-keypair> \
  --security-group-ids <sg-allowing-22-8080-and-optionally-8000> \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=jenkins-modelmatch-smoke},{Key=project,Value=modelmatch}]'

# 1c. Attach an EIP (an EIP *attached* to Jenkins is fine; an *unattached* one is the orphan to avoid).
aws ec2 allocate-address --region ap-south-1 --domain vpc
aws ec2 associate-address --region ap-south-1 --instance-id <i-...> --allocation-id <eipalloc-...>
```

Alternative if `register-image` is fiddly: `aws ec2 create-volume --snapshot-id … `, launch
a stock Ubuntu instance, attach + mount the volume to recover Jenkins home.

## Step 2 — make the ModelMatch backend reachable from Jenkins (and VERIFY it)

The agent stage runs `curl -X POST <ci_runs_url>` **on the Jenkins node** (not inside the
agent container); `<ci_runs_url>` is baked from the backend's `PUBLIC_BASE_URL` when
`/ci-setup` generates the snippet. So `PUBLIC_BASE_URL` must be a URL the **Jenkins node
can actually reach**. Pick one — and note the SG implication:

- **Same host (recommended):** run the ModelMatch backend (compose) on the Jenkins EC2
  and set **`PUBLIC_BASE_URL=http://127.0.0.1:8000`** (or the instance's **private** IP).
  The node-local curl works with **no inbound 8000 rule** — only 22 + 8080 are exposed.
  ⚠️ Don't use `http://<eip>:8000` for the same-host case unless you also open inbound
  8000 (a public EIP:8000 round-trip needs the SG rule; that's the connectivity trap).
- **External/exposed backend:** point `PUBLIC_BASE_URL` at wherever the backend runs and
  **open inbound 8000** on the SG (or whatever port it listens on) from the Jenkins node.

**Verify reachability BEFORE running the pipeline** (SSH to the Jenkins node):

```bash
# from the Jenkins EC2 node — must return 200 before you trust the agent stage
curl -fsS "${PUBLIC_BASE_URL}/healthz" && echo OK
# (optional) confirm the per-project ingest route is routable (401 without the token is fine):
curl -s -o /dev/null -w '%{http_code}\n' -X POST "${PUBLIC_BASE_URL}/projects/<id>/ci-runs"
```

If the agent runs the build inside a **container/cloud agent** (not the controller host),
remember `127.0.0.1` is that container's loopback — use the backend's reachable host/IP
and verify with the same curl from inside that agent context.

## Step 3 — set up the cowsay job + the ModelMatch CI-Agent

1. In the ModelMatch UI: **Create a new CI-Agent** → recommend (ci_review) → pick (Haiku
   runnable; Sonnet baseline computed) → **Connect Jenkins** (the cowsay job URL) →
   **CI setup**: copy the **mint-once CI token** and the generated **stage snippet**.
2. In Jenkins, add two **"Secret text"** credentials (ids must match the snippet):
   - `modelmatch-ci-token` — the per-project ingest token from step 1.
   - `modelmatch-model-api-key` — **Steve's Anthropic API key** (BYOK; ModelMatch never
     sees it — the agent reads it at runtime).
3. Create a cowsay pipeline/multibranch job pointing at the cowsay repo. Reduce its
   `Jenkinsfile` to **Build → Test → (paste the ModelMatch AI Review stage)**. The
   snippet's defaults: `LLM_CLIENT=anthropic`, `AGENT_MODEL=<Haiku>`, per-run
   `AGENT_TOKEN_CEILING` (the agent-loop cost cap). It builds the PR diff via
   `CHANGE_TARGET` (multibranch PR) falling back to `main`.

## Step 4 — run the loop

1. Branch off `main` in cowsay, make a small reviewable change (e.g. tweak a route in
   `src/`), open a **PR**.
2. The multibranch PR build runs Build → Test → **ModelMatch AI Review**: the agent
   reviews the diff (security + style), prints findings JSON, POSTs to
   `/projects/{id}/ci-runs` with the token, and **propagates the gate as the build
   status** (review + gate stay in CI).
3. Open the ModelMatch dashboard for that CI-Agent → confirm the **run appears** with
   **savings vs the Sonnet baseline** and the **quality gate** outcome. Ask the grounded
   chat one question to round out the rehearsal.

## Step 5 — TEARDOWN  ⚠️ run after S17b (the env is kept up for the live run)

> The stack is intentionally **left running** (EIPs + `restart: unless-stopped`) so S17b's
> live-BYOK run + the rating-UI demo can use it. Tear down when S17b is done. (Note: an EIP
> on a **stopped** instance bills ~\$0.005/h overnight — minor; release them at teardown.)

```bash
# terminate both instances (terminating releases their EBS root volumes)
aws ec2 terminate-instances --region ap-south-1 --instance-ids i-0f701eeb64a1d2bbd i-02a90abddaf87baa3
# release the two EIPs (don't leave UNATTACHED EIPs — those bill)
aws ec2 release-address --region ap-south-1 --allocation-id eipalloc-0a69f97024b95cae6   # jenkins
aws ec2 release-address --region ap-south-1 --allocation-id eipalloc-06797243f74f53cbd   # app
# optional: drop the smoke SGs, the registered AMI, and the ECR images
aws ec2 delete-security-group --region ap-south-1 --group-id sg-0f7bf710e9c7b6345        # app SG (delete after instances gone)
aws ec2 deregister-image      --region ap-south-1 --image-id ami-03cfe8d7787b8eb3c       # the restore AMI
# orphan check: no stray EBS / unattached EIP / SG left behind
aws ec2 describe-volumes   --region ap-south-1 --filters Name=status,Values=available
aws ec2 describe-addresses --region ap-south-1 --query 'Addresses[?AssociationId==null]'
# delete the GitLab smoke branch when done: git push origin --delete feature/modelmatch-smoke
```

## Guardrails (umbrella CLAUDE.md)

- Region **`ap-south-1`**; **`t3a.medium`, 30 GiB gp3**; **destroy at day end** + orphan
  check. **Confirm with Steve before any spin-up** (real cost).
- **Never commit** the Anthropic key, the CI token, or any secret. They live only in
  Jenkins credentials / the EC2 env.
- Feed **Gemini free-tier only non-confidential/throwaway code** (it trains on inputs);
  cowsay demo fixtures are fine, real client code is not.
