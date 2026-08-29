# Deploying to AWS

Two constraints drive every choice below. Read them before picking a service.

> ### 1. Exactly one instance. Never autoscale.
> APScheduler runs **inside the web process**. A second task means a second
> scheduler: every source scraped twice, every gauge scored twice, two writers
> on one SQLite file, and double the load on DHM's and BIPAD's servers. Set
> desired count to 1 and leave autoscaling off. This is a correctness limit,
> not a cost saving.
>
> ### 2. The container needs persistent storage.
> `flood.db` holds the reading history that the forecast and the impoundment
> baseline are computed from, and `data/tiles` holds the map cache. Lose them on
> every deploy and the system restarts blind: no trend, no rise rate, no
> outburst detection until enough cycles have run again.

That second point rules out the obvious easy answer, so:

| Option | Persistent storage | Cost/mo (approx) | Verdict |
|---|---|---|---|
| **Lightsail container / EC2 + Docker volume** | Yes, local disk | **~$10–15** | **Recommended.** Simplest thing that is correct |
| **ECS Fargate + EFS** | Yes, EFS mount | ~$35–45 | Right answer if you already run ECS |
| App Runner | **No** | ~$25 | Avoid — storage is ephemeral, history resets each deploy |
| Lambda | No, and 15-min cap | — | Wrong shape entirely |


---

## 0. Rehearse locally with Floci first

[Floci](https://floci.io) emulates AWS on your own machine (LocalStack-style,
API on `:4566`). The whole ECR → ECS path below was validated against it before
any real AWS resource was created, and it is worth doing again after any change
to the image.

Start the emulator and point the CLI at it:

```bash
docker start floci
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
export EP="--endpoint-url=http://localhost:4566"
```

Push the image. **Use the registry host directly** — the ECR API advertises a
subdomain-style URI (`000000000000.dkr.ecr.us-east-1.localhost:5100`) that does
not resolve on Windows or macOS:

```bash
aws $EP ecr create-repository --repository-name nepal-flood-watch
docker tag nepal-flood-watch:latest localhost:5100/nepal-flood-watch:latest
docker push localhost:5100/nepal-flood-watch:latest
```

Register the task and run exactly one:

```bash
aws $EP ecs create-cluster --cluster-name nfw
aws $EP ecs register-task-definition --cli-input-json file://taskdef.json
aws $EP ecs run-task --cluster nfw --task-definition nepal-flood-watch --count 1
```

Floci launches a real container through the mounted Docker socket, so this
genuinely exercises the image, the healthcheck and the port mapping. Confirm the
app completed a cycle rather than merely started:

```bash
curl -s http://127.0.0.1:8200/api/summary | jq '{total_stations, bands, last_cycle: .last_cycle.sources}'
```

A good result looks like 309 stations with `dhm_river`, `bipad`, `rainfall`,
`quake` and `news` all `ok`. `fire` fails until a FIRMS key is set.

Tear down when finished:

```bash
aws $EP ecs stop-task --cluster nfw --task $(aws $EP ecs list-tasks --cluster nfw --query 'taskArns[0]' --output text)
```

### What Floci cannot tell you

| Emulated | Not emulated |
|---|---|
| ECR, ECS, EC2, IAM, SSM, CloudWatch Logs, Lightsail, CloudFormation | **EFS**, **ELBv2/ALB**, App Runner |

Because EFS is not emulated, **the persistent-volume mount is the one part of
this deployment that a Floci run does not prove**. Verify it on real AWS by
redeploying once and checking that `/api/health` still reports gauge history
afterwards.

### Git Bash on Windows

MSYS rewrites arguments that look like Unix paths, so `/nfw/firms-key` becomes
`C:/Program Files/Git/nfw/firms-key` and the parameter is silently created under
the wrong name. Prefix such commands:

```bash
MSYS_NO_PATHCONV=1 aws $EP ssm put-parameter --name /nfw/firms-key --type SecureString --value "KEY"
```

---

## 1. Build and push the image to ECR

The `Dockerfile` is local-only and not tracked in git, so run this from your
working copy.

Set your account and region once:

```bash
export AWS_REGION=ap-south-1
export AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export ECR=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/nepal-flood-watch
```

Create the repository (once):

```bash
aws ecr create-repository --repository-name nepal-flood-watch --region $AWS_REGION
```

Authenticate Docker to ECR:

```bash
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
```

Build for **linux/amd64** — this matters if you are building on an Apple Silicon
Mac, where the default arch will not run on Fargate or a standard EC2 instance:

```bash
docker buildx build --platform linux/amd64 -t $ECR:latest --push .
```

On an x86 machine the plain build works too:

```bash
docker build -t $ECR:latest . && docker push $ECR:latest
```

---

## 2a. Lightsail — recommended

Cheapest correct option. One container, one persistent disk, HTTPS included.

Create the instance (Amazon Linux 2023, `small_2_0` is ample):

```bash
aws lightsail create-instances --instance-names nfw --availability-zone ${AWS_REGION}a --blueprint-id amazon_linux_2023 --bundle-id small_2_0
```

Then SSH in and run:

```bash
sudo dnf install -y docker && sudo systemctl enable --now docker
aws ecr get-login-password --region $AWS_REGION | sudo docker login --username AWS --password-stdin $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
sudo docker volume create nfw-data && sudo docker volume create nfw-logs
sudo docker run -d --name nfw --restart unless-stopped -p 80:8000 -v nfw-data:/app/data -v nfw-logs:/app/logs -e FIRMS_MAP_KEY=$FIRMS_MAP_KEY $ECR:latest
```

`--restart unless-stopped` is what makes it survive a reboot. Open port 80 in
the Lightsail firewall, and the console is live.

To update later:

```bash
sudo docker pull $ECR:latest && sudo docker stop nfw && sudo docker rm nfw
```

then re-run the `docker run` above. The named volumes survive, so the reading
history and tile cache carry over.

---

## 2b. ECS Fargate + EFS

Use this if you already run ECS. The EFS mount is the part people skip, and
skipping it is what silently destroys the gauge history.

Create the file system and a mount target per subnet:

```bash
aws efs create-file-system --creation-token nfw-data --tags Key=Name,Value=nfw-data
```

In the task definition, the essentials:

```json
{
  "family": "nepal-flood-watch",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "volumes": [{
    "name": "data",
    "efsVolumeConfiguration": { "fileSystemId": "fs-XXXX", "rootDirectory": "/data", "transitEncryption": "ENABLED" }
  }],
  "containerDefinitions": [{
    "name": "app",
    "image": "ACCOUNT.dkr.ecr.REGION.amazonaws.com/nepal-flood-watch:latest",
    "portMappings": [{ "containerPort": 8000, "protocol": "tcp" }],
    "mountPoints": [{ "sourceVolume": "data", "containerPath": "/app/data" }],
    "environment": [{ "name": "CYCLE_MINUTES", "value": "12" }],
    "secrets": [{ "name": "FIRMS_MAP_KEY", "valueFrom": "arn:aws:ssm:REGION:ACCOUNT:parameter/nfw/firms-key" }],
    "healthCheck": {
      "command": ["CMD-SHELL", "curl -fsS http://127.0.0.1:8000/api/health || exit 1"],
      "interval": 60, "timeout": 10, "retries": 3, "startPeriod": 60
    },
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": { "awslogs-group": "/ecs/nepal-flood-watch", "awslogs-region": "REGION", "awslogs-stream-prefix": "app" }
    }
  }]
}
```

Create the service with **one** task and no scaling policy:

```bash
aws ecs create-service --cluster nfw --service-name nepal-flood-watch --task-definition nepal-flood-watch --desired-count 1 --launch-type FARGATE --network-configuration "awsvpcConfiguration={subnets=[subnet-XXXX],securityGroups=[sg-XXXX],assignPublicIp=ENABLED}"
```

If you put an ALB in front, point the target group health check at
`/api/health` and raise the deregistration delay — a cycle in flight should be
allowed to finish.

---

## 3. Secrets

The only secret is the optional FIRMS key. Do not bake it into the image:

```bash
aws ssm put-parameter --name /nfw/firms-key --type SecureString --value "YOUR_KEY"
```

Everything else has a working default. Without the FIRMS key the fire layer is
simply disabled and every other source keeps running.

---

## 4. After deploying

Warm the tile cache once, so the map renders without hitting the provider on
every pan:

```bash
curl -X POST https://YOUR_HOST/api/tiles/prefetch?style=dark
```

That pulls ~16,600 tiles (~195 MB) into the persistent volume. Run it once per
deployment target, not per release.

Then confirm the system is actually healthy rather than merely running:

```bash
curl -s https://YOUR_HOST/api/health | jq '{stations, sources, quality}'
```

`sources` should show `ok` for `dhm_river`, `bipad`, `rainfall`, `quake` and
`news`. `fire` reports an error until the FIRMS key is set — that is expected
and non-fatal.

---

## 5. Costs and courtesy

Roughly 6 outbound requests per cycle, ~720/day. That is negligible for AWS
billing, but it is not negligible to the people running DHM's and BIPAD's
servers. Do not lower `CYCLE_MINUTES` below 10, and do not run several
deployments against the same sources.

The tile prefetch is the one bulk operation in the system. Run it once per
environment. Never repoint it at OpenStreetMap's own tile servers — bulk
prefetching them is explicitly against their tile usage policy.
