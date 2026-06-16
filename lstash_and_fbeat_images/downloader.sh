#!/usr/bin/env bash
set -euo pipefail

VERSION="9.4.2"
PLATFORM="linux/amd64"
OUTDIR="elastic-images-${VERSION}"

mkdir -p "$OUTDIR"
cd "$OUTDIR"

declare -A IMAGES
IMAGES[logstash]="docker.elastic.co/logstash/logstash:${VERSION}"
IMAGES[filebeat]="docker.elastic.co/beats/filebeat:${VERSION}"
IMAGES[elastic-agent]="docker.elastic.co/elastic-agent/elastic-agent:${VERSION}"

for NAME in logstash filebeat elastic-agent; do
  IMAGE="${IMAGES[$NAME]}"
  TAR="${NAME}-${VERSION}-linux-amd64.tar"

  echo "Pulling $IMAGE for $PLATFORM..."
  docker pull --platform "$PLATFORM" "$IMAGE"

  echo "Saving $IMAGE to $TAR..."
  docker save "$IMAGE" -o "$TAR"

  echo "Splitting $TAR into 50MB chunks..."
  split -b 50M "$TAR" "${TAR}.part-"

  echo "Removing main tar file $TAR..."
  rm -f "$TAR"

  echo "Done: $NAME"
done

echo "All images pulled, saved, split, and main tar files removed."