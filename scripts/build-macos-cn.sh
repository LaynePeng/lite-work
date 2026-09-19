#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/mirrors.sh"
exec bash "$(dirname "$0")/build-macos.sh" "$@"
