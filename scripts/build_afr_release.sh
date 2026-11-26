#!/bin/bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    printf 'Usage: %s GIT_REVISION OUTPUT_TAR\n' "$0" >&2
    exit 2
fi

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
revision=$(git -C "$repo" rev-parse --verify "$1^{commit}")
git -C "$repo" archive --format=tar "$revision" \
    afr_agent backend/app/__init__.py backend/app/docx_adapter.py \
    backend/app/document_representation backend/app/source_blocks.py > "$2"
