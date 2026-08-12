#!/usr/bin/env bash
#
# Stand up the memory cassette on one EC2 instance in us-west-1.
#
# Deliberately one box rather than a container platform. Lightsail and App
# Runner are less work to host, but both publish a public endpoint with no IP
# allowlist, and this service has no authentication of its own: anything that
# can reach it can read and write your memory. A security group is the boundary,
# so the thing that takes a security group wins.
#
# No inbound SSH. The instance gets an SSM role and you reach it with
# `aws ssm start-session`, which means port 22 is never open at all.
#
# Idempotent: every resource is looked up before it is created, so re-running
# after a partial failure resumes rather than duplicates.
#
# Usage:  ./deploy/aws.sh            # create or converge
#         ./deploy/aws.sh destroy    # remove everything it made

set -euo pipefail

REGION="${AWS_REGION:-us-west-1}"
NAME="memory-cassette"
INSTANCE_TYPE="${INSTANCE_TYPE:-t4g.small}"   # arm64; both images are multi-arch
PORT=8082
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
IMAGE="${MEMORY_IMAGE:-$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$NAME:latest}"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }

# --- destroy ------------------------------------------------------------------

if [[ "${1:-}" == "destroy" ]]; then
  say "Terminating instances tagged Name=$NAME"
  ids=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)
  if [[ -n "$ids" ]]; then
    aws ec2 terminate-instances --region "$REGION" --instance-ids $ids >/dev/null
    aws ec2 wait instance-terminated --region "$REGION" --instance-ids $ids
  fi
  say "Removing security group, role, and instance profile"
  sg=$(aws ec2 describe-security-groups --region "$REGION" --filters "Name=group-name,Values=$NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)
  [[ "$sg" != "None" ]] && aws ec2 delete-security-group --region "$REGION" --group-id "$sg" || true
  aws iam remove-role-from-instance-profile --instance-profile-name "$NAME" --role-name "$NAME" 2>/dev/null || true
  aws iam delete-instance-profile --instance-profile-name "$NAME" 2>/dev/null || true
  for arn in arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore \
             arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly; do
    aws iam detach-role-policy --role-name "$NAME" --policy-arn "$arn" 2>/dev/null || true
  done
  aws iam delete-role --role-name "$NAME" 2>/dev/null || true
  say "Done. The ECR repository and its images are left alone."
  exit 0
fi

# --- who is allowed in --------------------------------------------------------

MY_IP="${ALLOW_IP:-$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')}"
[[ -n "$MY_IP" ]] || { echo "could not determine your public IP; set ALLOW_IP" >&2; exit 1; }
say "Allowing $MY_IP/32 to reach port $PORT, and nothing else"

# --- security group -----------------------------------------------------------

VPC=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)

SG=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=$NAME" "Name=vpc-id,Values=$VPC" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)

if [[ "$SG" == "None" || -z "$SG" ]]; then
  SG=$(aws ec2 create-security-group --region "$REGION" --group-name "$NAME" \
    --description "memory cassette: tapes API, single source address" \
    --vpc-id "$VPC" --query GroupId --output text)
  say "Created security group $SG"
fi

# Converge the ingress rule on every run, because a home IP moves. Old rules on
# this port are revoked first so a stale address never keeps access.
existing=$(aws ec2 describe-security-groups --region "$REGION" --group-ids "$SG" \
  --query "SecurityGroups[0].IpPermissions[?FromPort==\`$PORT\`].IpRanges[].CidrIp" --output text)
for cidr in $existing; do
  [[ "$cidr" == "$MY_IP/32" ]] && continue
  say "Revoking stale ingress from $cidr"
  aws ec2 revoke-security-group-ingress --region "$REGION" --group-id "$SG" \
    --protocol tcp --port "$PORT" --cidr "$cidr" >/dev/null
done
if ! grep -qw "$MY_IP/32" <<<"$existing"; then
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG" \
    --protocol tcp --port "$PORT" --cidr "$MY_IP/32" >/dev/null
  say "Authorized $MY_IP/32 on $PORT"
fi

# --- instance role: SSM in, ECR pull out --------------------------------------

if ! aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$NAME" --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }' >/dev/null
  say "Created role $NAME"
fi
for arn in arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore \
           arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly; do
  aws iam attach-role-policy --role-name "$NAME" --policy-arn "$arn"
done
if ! aws iam get-instance-profile --instance-profile-name "$NAME" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$NAME" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$NAME" --role-name "$NAME"
  say "Created instance profile; waiting for IAM to propagate"
  sleep 15
fi

# --- already running? ---------------------------------------------------------

RUNNING=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
  --query 'Reservations[].Instances[0].InstanceId' --output text)
if [[ -n "$RUNNING" && "$RUNNING" != "None" ]]; then
  ip=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$RUNNING" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  say "Instance $RUNNING already running at http://$ip:$PORT"
  say "Redeploy the image with: aws ssm start-session --target $RUNNING --region $REGION"
  exit 0
fi

# --- passwords ----------------------------------------------------------------
# Generated per deploy and only ever written to the instance's own disk. They
# guard a database that is not reachable from outside the box in any case.

PGPW=$(openssl rand -hex 16)
CASSPW=$(openssl rand -hex 16)

AMI=$(aws ssm get-parameters --region "$REGION" \
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query 'Parameters[0].Value' --output text)
say "Launching $INSTANCE_TYPE from $AMI"

USER_DATA=$(cat <<EOF
#!/bin/bash
set -eux
dnf install -y docker
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
  https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-aarch64
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

mkdir -p /opt/$NAME && cd /opt/$NAME
cat > provision.sql <<'SQL'
$(cat "$(dirname "$0")/../provision.sql" | sed "s/PASSWORD 'cassette'/PASSWORD '$CASSPW'/")
SQL
cat > compose.yaml <<'YAML'
$(cat "$(dirname "$0")/compose.aws.yaml")
YAML
cat > .env <<ENV
POSTGRES_PASSWORD=$PGPW
CASSETTE_PASSWORD=$CASSPW
MEMORY_IMAGE=$IMAGE
ENV
chmod 600 .env

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
docker compose up -d

# Survive a reboot without depending on the cloud-init script running again.
cat > /etc/systemd/system/$NAME.service <<UNIT
[Unit]
Description=memory cassette
Requires=docker.service
After=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/$NAME
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
[Install]
WantedBy=multi-user.target
UNIT
systemctl enable $NAME.service
EOF
)

INSTANCE=$(aws ec2 run-instances --region "$REGION" \
  --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
  --security-group-ids "$SG" \
  --iam-instance-profile "Name=$NAME" \
  --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3,Encrypted=true}' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
  --user-data "$USER_DATA" \
  --query 'Instances[0].InstanceId' --output text)

say "Launched $INSTANCE; waiting for it to run"
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE"
IP=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

cat <<DONE

  instance   $INSTANCE  ($INSTANCE_TYPE, $REGION)
  tapes      http://$IP:$PORT
  reachable  from $MY_IP/32 only
  shell      aws ssm start-session --target $INSTANCE --region $REGION

  Docker images still have to pull on first boot, so give it a couple of
  minutes, then:

    curl http://$IP:$PORT/v1/cassettes

  Point paperplane's Memory base at http://$IP:$PORT

DONE
