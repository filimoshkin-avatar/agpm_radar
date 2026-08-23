#!/usr/bin/env bash
# Install a local embedding runtime on Local Ru.
#
# The owner asked for a local tool and a vector store so the two ways of linking a
# statement to evidence can be compared rather than argued about. Local because
# the alternative is sending 20 000 chunks to somebody's API for a comparison, and
# because pgvector has been in this schema since migration 001 with nothing to
# fill it.
#
# Three things about the shape of this:
#
#   Its own runtime. torch is ~600 MB with its dependencies and has no business in
#   the worker's locked requirements, which every deployment carries. The
#   interpreter is the standalone CPython 3.11 already on this host - the system
#   python is 3.14 and torch has no wheels for it.
#
#   The model is downloaded once, here, by a person running an installer, into the
#   runtime directory. After that the unit that uses it has no internet at all.
#
#   CPU only. `--index-url .../whl/cpu` is the difference between 180 MB and
#   2.5 GB of CUDA nobody on this host can use.
set -euo pipefail

RUNTIME=/usr/local/lib/radar-embed-runtime
DONOR_PYTHON=/usr/local/lib/radar-hermes-runtime/python
MODEL="${RADAR_KX_EMBED_MODEL:-intfloat/multilingual-e5-small}"

say() { printf '[install-embedder] %s\n' "$*"; }
die() { printf '[install-embedder] FAIL: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root"
[[ -x "$DONOR_PYTHON/bin/python3.11" ]] || die "no standalone python at $DONOR_PYTHON"

say "1/5 interpreter"
if [[ ! -x "$RUNTIME/python/bin/python3.11" ]]; then
    mkdir -p "$RUNTIME"
    cp -a "$DONOR_PYTHON" "$RUNTIME/python"
    say "    copied a standalone CPython 3.11"
else
    say "    present"
fi

say "2/5 virtualenv"
if [[ ! -x "$RUNTIME/venv/bin/python" ]]; then
    "$RUNTIME/python/bin/python3.11" -m venv "$RUNTIME/venv"
    "$RUNTIME/venv/bin/python" -m pip install --quiet --upgrade pip
    say "    created"
else
    say "    present"
fi

say "3/5 torch, CPU build"
if ! "$RUNTIME/venv/bin/python" -c "import torch" 2>/dev/null; then
    "$RUNTIME/venv/bin/python" -m pip install --quiet \
        --index-url https://download.pytorch.org/whl/cpu torch
    say "    installed"
else
    say "    present"
fi

say "4/5 sentence-transformers"
if ! "$RUNTIME/venv/bin/python" -c "import sentence_transformers" 2>/dev/null; then
    "$RUNTIME/venv/bin/python" -m pip install --quiet sentence-transformers
    say "    installed"
else
    say "    present"
fi
"$RUNTIME/venv/bin/python" -m pip freeze > "$RUNTIME/requirements.txt"

say "5/5 model"
mkdir -p "$RUNTIME/models"
HF_HOME="$RUNTIME/models" HF_HUB_DISABLE_TELEMETRY=1 \
    "$RUNTIME/venv/bin/python" - "$MODEL" <<'PYEOF'
import sys
from sentence_transformers import SentenceTransformer

name = sys.argv[1]
model = SentenceTransformer(name, device="cpu")
vector = model.encode(["query: проверка", "passage: a check"], normalize_embeddings=True)
print(f"    {name}: {vector.shape[1]} dimensions, encoded {vector.shape[0]} sentences")
PYEOF

chmod -R u=rwX,go=rX "$RUNTIME"
say "runtime at $RUNTIME, model cached under models/"
du -sh "$RUNTIME" | sed 's/^/[install-embedder]     /'
