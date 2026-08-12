#!/usr/bin/env bash
#
# AWS path only — companion to deploy/aws.sh, for the EC2 box it stands up.
#
# Reach the memory cassette from any network, without touching the security
# group: an SSM port forward to the box's tapes listener.
#
# The security group pins port 8082 to a single source address, and that
# address goes stale every time the laptop's public IP moves. This tunnel
# sidesteps the problem instead of chasing it — Session Manager rides the
# instance's own outbound connection to AWS, so no ingress rule is consulted
# at all. Works from home, the office, or conference wifi, unchanged.
#
# Usage:  ./deploy/tunnel.sh [local-port]     # default 8082
#
# Then point your client's memory base at http://localhost:8082.
# Foreground process; Ctrl-C ends the tunnel.

set -euo pipefail

REGION="${AWS_REGION:-us-west-1}"
NAME="memory-cassette"
PORT=8082
LOCAL="${1:-8082}"

id=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
[[ -n "$id" && "$id" != "None" ]] || { echo "no running $NAME instance in $REGION" >&2; exit 1; }

printf '\033[36m==>\033[0m %s\n' "Forwarding localhost:$LOCAL -> $id:$PORT (Ctrl-C to stop)"
exec aws ssm start-session --target "$id" --region "$REGION" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "{\"portNumber\":[\"$PORT\"],\"localPortNumber\":[\"$LOCAL\"]}"
