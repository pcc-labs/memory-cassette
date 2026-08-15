#!/usr/bin/env bash
#
# Build the cassette image for the box's architecture, push it to ECR, and roll
# the running box onto it.
#
# `deploy/aws.sh` assumes the image is already at
# $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/memory-cassette:latest — this script is
# what puts it there. It goes one step further than handoff-cassette's
# push.sh, which stops after the push: aws.sh exits early once the box is
# running ("already running... redeploy with aws ssm"), so a pushed image that
# nothing pulls is not a deploy, and the pull was the part being hand-typed.
#
# Two things this exists to stop repeating:
#
#   1. The box's ECR credential expires. Its login is from whenever cloud-init
#      last ran, and ECR tokens last 12 hours, so `docker compose pull` fails
#      with "repository does not exist or may require 'docker login'" — which
#      reads like the image is missing rather than the token being stale. The
#      instance role carries AmazonEC2ContainerRegistryReadOnly, so the box can
#      always mint a fresh one; this refreshes it before every pull.
#
#   2. Only the `memory` service is recreated. Postgres holds the entries and
#      tapes fronts the cassette, so rolling all three would take the store
#      down to ship a service that carries no state.
#
# Never runs aws.sh. That script converges the security group first, revoking
# every allowlisted CIDR that is not the caller's current public IP — a
# redeploy has no business touching who can reach the box.
#
# The box is t4g (arm64), so the image is built linux/arm64 regardless of the
# laptop's architecture.
#
# Usage:  ./deploy/push.sh                 # build, push, roll the box
#         ./deploy/push.sh v0.2.0          # same, at an explicit tag
#         ./deploy/push.sh --no-restart    # build and push only

set -euo pipefail

REGION="${AWS_REGION:-us-west-1}"
NAME="memory-cassette"
SERVICE="memory"          # the one service that carries no state
RESTART=1
TAG="latest"
for arg in "$@"; do
  case "$arg" in
    --no-restart) RESTART=0 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) TAG="$arg" ;;
  esac
done

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
REPO="$REGISTRY/$NAME"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*" >&2; }

# --- build and push -----------------------------------------------------------

if ! aws ecr describe-repositories --region "$REGION" --repository-names "$NAME" >/dev/null 2>&1; then
  aws ecr create-repository --region "$REGION" --repository-name "$NAME" \
    --image-scanning-configuration scanOnPush=true >/dev/null
  say "Created ECR repository $NAME"
fi

say "Logging in to $REGISTRY"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

say "Building and pushing $REPO:$TAG (linux/arm64)"
docker buildx build --platform linux/arm64 -t "$REPO:$TAG" --push \
  "$(dirname "$0")/.."

[[ "$RESTART" == 1 ]] || { say "Pushed $REPO:$TAG. Skipping the box (--no-restart)."; exit 0; }

# --- roll the box -------------------------------------------------------------

INSTANCE=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[0].InstanceId' --output text 2>/dev/null || echo None)

if [[ -z "$INSTANCE" || "$INSTANCE" == "None" ]]; then
  warn "No running instance tagged Name=$NAME in $REGION."
  say  "Image is pushed; ./deploy/aws.sh will pull it when the box is created."
  exit 0
fi

say "Rolling $SERVICE on $INSTANCE"
CMD=$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript --timeout-seconds 600 \
  --parameters commands="[
    \"set -e\",
    \"cd /opt/$NAME\",
    \"aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY\",
    \"docker compose pull $SERVICE\",
    \"docker compose up -d $SERVICE\",
    \"sleep 6\",
    \"docker compose ps --format '{{.Service}}\\t{{.Status}}'\"
  ]" \
  --query 'Command.CommandId' --output text)

# send-command returns as soon as the command is queued; the roll takes a pull.
for _ in $(seq 1 40); do
  STATUS=$(aws ssm get-command-invocation --region "$REGION" \
    --command-id "$CMD" --instance-id "$INSTANCE" \
    --query 'Status' --output text 2>/dev/null || echo Pending)
  case "$STATUS" in Success|Failed|Cancelled|TimedOut) break ;; esac
  sleep 3
done

OUT=$(aws ssm get-command-invocation --region "$REGION" \
  --command-id "$CMD" --instance-id "$INSTANCE" \
  --query 'StandardOutputContent' --output text 2>/dev/null || true)

if [[ "$STATUS" != "Success" ]]; then
  warn "Roll finished $STATUS. Command id $CMD"
  aws ssm get-command-invocation --region "$REGION" --command-id "$CMD" \
    --instance-id "$INSTANCE" --query 'StandardErrorContent' --output text >&2 || true
  exit 1
fi

printf '%s\n' "$OUT"
say "Deployed $REPO:$TAG to $INSTANCE"
