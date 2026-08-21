# Deploying the beta

One Lightsail instance running the whole stack — Postgres, Redis, the API and
Caddy — behind an automatically renewed TLS certificate.

**$7.00/month, flat.** No metered lines, no surprises.

## Why this shape

The obvious AWS answer is ECS. Priced out, it is six times more for a beta:

| | This | ECS/Fargate |
| --- | --- | --- |
| Monthly | **$7.00** | $44–60 |
| Postgres + Redis | on the box | $25.68 managed |
| TLS | Caddy, automatic | ALB + ACM ($16.43) |
| Rolling deploys | ~40s of downtime | zero |
| Survives instance loss | **no** | yes |

Fargate has no persistent local disk, so the database cannot sit next to the
app and you are pushed onto RDS and ElastiCache. That, plus a load balancer, is
where the money goes — not ECS itself, which is free.

Lightsail rather than EC2 for the same reason in miniature: its price includes
the public IPv4 address (**$3.65/month on EC2**, charged whether the instance
is running or stopped) and 2 TB of transfer. The equivalent EC2 machine is
$10.58 for less disk.

**Move to ECS when an instance reboot becomes unacceptable to real customers,**
not before. `basivo-auth` already generates working ECS Terraform with RDS
Proxy and Secrets Manager, so that path is written and CI-validated.

## What you are accepting at $7

Stated plainly, because these are real:

- **One instance.** If it dies, the site is down until it is rebuilt.
- **No automated backups.** Postgres lives on the instance volume. Lightsail
  snapshots are $0.05/GB-month — see below.
- **Deploys have a gap.** `docker compose up -d` restarts containers; expect
  roughly 40 seconds.
- **Builds happen on the box.** Slow (several minutes), but it keeps
  the deployment to one moving part and needs no registry.

## `./deploy.sh`

One entry point for everything:

```bash
./deploy.sh up          # provision (first time)
./deploy.sh deploy      # ship current main to the instance
./deploy.sh status      # instance + containers + a live health check
./deploy.sh logs api    # follow logs
./deploy.sh ssh         # shell on the box
./deploy.sh smoke       # 37 assertions against the running site
./deploy.sh backup      # database dump to ./backups/
./deploy.sh destroy     # tear it all down
```

`deploy` warns if you have uncommitted or unpushed commits, because the
instance builds from GitHub — anything not pushed is not deployed, and finding
that out an hour later is avoidable.

`destroy` takes a backup, prints every resource it will remove, and requires
you to type `destroy basivo-beta`. Not `[y/N]`: it releases the static IP (so
the DNS record breaks and a rebuild gets a different address) and deletes the
database, which is too much to hang on one keystroke of muscle memory.

## First deploy

```bash
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in both values
cd ../.. && ./deploy.sh up
```

Then add the records it prints. **Caddy cannot obtain a certificate until the
A record resolves** — it will keep retrying, so the order does not matter, but
nothing serves HTTPS until DNS is live.

Watch the first boot (it clones, builds and starts, which takes a while):

```bash
ssh ubuntu@$(terraform output -raw static_ip) 'sudo tail -f /var/log/basivo-bootstrap.log'
```

## Email

Registration sends a verification link, so mail has to work or nobody can
finish signing up.

SES needs the three DKIM CNAMEs from `dns_records_to_add`. Once they resolve,
verification takes a few minutes, then:

```bash
./deploy/configure-email.sh          # writes the SMTP settings and restarts the api
```

**A new SES account is in the sandbox**: it will only deliver to addresses you
have verified individually, which is fine for you and a handful of testers.
Request production access in the SES console to lift it — usually granted
within a day. Until then, public signups will not receive their email.

## Verifying a deploy

```bash
python deploy/smoke-test.py
```

37 assertions against the running site: TLS, single-origin routing, both run
modes, SSE arriving progressively rather than buffered, attaching to a run
mid-flight, tenant isolation, key revocation and rate limiting. It reads the
hostname from Terraform, so it follows the deployment rather than hard-coding
an address.

Run it after every deploy. Both deployment defects found so far — Caddy not
routing bare collection paths like `POST /orgs`, and the bootstrap silently
running under `dash` — were invisible locally and obvious on the first real
request.

## Operating it

```bash
ssh ubuntu@<ip>
cd /opt/basivo/deploy

docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml restart api
```

Deploy a new version:

```bash
cd /opt/basivo && git pull
cd deploy && docker compose -f docker-compose.prod.yml up -d --build
```

Migrations run automatically when the API container starts.

### Backups, if you want them

```bash
# manual snapshot, ~$2/month for 40 GB
aws lightsail create-instance-snapshot \
  --instance-name basivo-beta \
  --instance-snapshot-name "basivo-$(date +%Y%m%d)"

# or just the data, which is what actually matters
ssh ubuntu@<ip> 'docker exec basivo-postgres-1 pg_dump -U basivo basivo_orch | gzip' > backup.sql.gz
```

The second is free and the one to automate first.

### Saving money

Stopping the instance saves the compute but **not** the address — Lightsail
bundles them, so a stopped instance still bills. To actually stop paying,
destroy it and restore from a snapshot later.

## Tearing it down

```bash
cd deploy/terraform
terraform destroy
```

Removes the instance, its static IP, the SES identity and the SMTP user.
**The database goes with it.** Take a dump first if the data matters.

## Security notes

- SSH is restricted to the single address in `ssh_allowed_cidr`. Update it when
  your IP changes; do not widen it to `0.0.0.0/0`.
- Postgres and Redis are not published to the host. There is no route to them
  from the internet — only the API container reaches them over the Compose
  network.
- Secrets are generated on the instance at first boot into a `0600` file. They
  are not in Terraform state, not in user data, and not in this repository.
- `terraform.tfstate` **does** contain the SES SMTP password. It is gitignored;
  keep it somewhere private.
- Unattended security upgrades are enabled. An unpatched box is the likeliest
  way this gets compromised.


## Sizing: the stack takes half the machine on purpose

On a 4 vCPU / 16 GB box the limits in `docker-compose.prod.yml` add up to
**~7.9 GB — under half**. That is not timidity, it is three specific things:

- **Rendering is spiky.** A video render is a browser holding hundreds of
  captured frames. Sized to "whatever is free", that spike is absorbed by the
  page cache one day and by the OOM-killer the next.
- **Postgres wants the rest as filesystem cache.** `shared_buffers` is only
  part of its memory story; the kernel caching the data files is what keeps
  queries off the disk.
- **Headroom is what makes a bad day survivable.** A leak, a runaway
  composition, an accidental 4K render — with half the box free those are an
  alert instead of an outage.

### What keeps it there

| Guard | Where | What it prevents |
|---|---|---|
| One heavy node per worker process | `BASIVO_HEAVY_CONCURRENCY=1` | Four Chromium captures competing for four cores — slower than doing them in turn, and how peak memory doubles |
| Two runs per worker, two workers | `BASIVO_MAX_CONCURRENT_RUNS=2`, `replicas: 2` | Unpredictable peaks; and a worker recycled for memory takes half the capacity with it, not all of it |
| Voice model dropped when idle | `BASIVO_SPEECH_IDLE_UNLOAD=600` | ~400 MB held for 23 hours by a workspace that speaks twice a day |
| Worker recycles itself when bloated | `BASIVO_WORKER_RSS_LIMIT_MB=1800` | Being OOM-killed mid-run. It exits only when idle, so nothing is abandoned |
| Render refuses to start without space | `BASIVO_MIN_FREE_DISK_GB=5` | A full disk. That does not fail a render, it stops Postgres accepting writes |
| Artifacts expire | `BASIVO_ARTIFACT_RETENTION_DAYS=30` | Megabyte rows accumulating in the database and in every backup |
| Orphaned scratch directories swept | housekeeping loop | Frame directories a SIGKILLed render left behind |
| `shm_size: 512m` on the worker | compose | Chromium crashing on Docker's 64 MB `/dev/shm` with an error that never mentions shared memory |

Raise the worker `cpus` and `BASIVO_RENDER_WORKERS` together if you would
rather have faster renders than headroom. Everything else should stay put until
there is a measurement saying otherwise.
