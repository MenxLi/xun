#!/bin/sh
# Overwrite TARGET_DIR with the docs shipped inside the PyPI xun-agent wheel.
# usage: sync_docs.sh TARGET_DIR [VERSION]
set -eu

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

pip install -q --no-deps --target "$tmp/pkg" "xun-agent${2:+==$2}"
version=$(basename "$tmp"/pkg/xun_agent-*.dist-info)

rm -rf "$1"
cp -r "$tmp/pkg/xun/assets/docs" "$1"
echo "synced docs ($version) to $1"
