"""Local sentence embeddings, and the comparison they exist for (owner request).

The lexical binding of slice 2.5 matches a wiki statement to a quotation by
shared words. The owner looked at the result and called the connection quality
mediocre, which it is: an OR query over a sentence returns anything that shares
three of its words.

This is the other way of doing it - cosine distance between sentence embeddings -
computed locally, with no egress and no cost, so the two can be compared on the
same statements against the same claims rather than argued about.

Three things worth knowing about the model:

* `intfloat/multilingual-e5-small`, 384 dimensions. Multilingual is not optional
  here: the corpus is 4 183 Russian documents against 1 191 English ones, and a
  monolingual model would be blind to most of it.
* **e5 wants prefixes.** `query:` on the side asking and `passage:` on the side
  being searched. Without them the model is being used off-distribution and the
  comparison would measure that rather than the method.
* The import is lazy and the dependency is not in the worker's locked
  requirements. torch is 600 MB with its dependencies and belongs in the
  embedder's own runtime, not in every deployment of the fetcher.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from radar_kx.identifiers import sha256_bytes

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

DEFAULT_MODEL = "intfloat/multilingual-e5-small"
DEFAULT_DIMENSIONS = 384

#: Where the installer cached the weights. The unit that runs this has no
#: internet, so a model that is not here is an error rather than a download.
RUNTIME_HOME = "/usr/local/lib/radar-embed-runtime"
MODEL_CACHE = f"{RUNTIME_HOME}/models"

#: e5's convention. The side asking is a query; the side being searched is a
#: passage. Using one prefix for both measures the wrong thing.
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

#: How many texts go to the model at once. Larger is faster and costs memory;
#: 32 keeps the four-core host comfortable.
BATCH_SIZE = 32

#: Longer than this and the model truncates anyway; cutting here makes the cost
#: predictable and the truncation visible in one place.
MAX_CHARS = 2000


class EmbeddingError(RuntimeError):
    """The embedder cannot be loaded or used."""


def load_model(name: str = DEFAULT_MODEL) -> Any:
    """Load the local model. Imported here so the worker never sees torch."""
    import os

    os.environ.setdefault("HF_HOME", MODEL_CACHE)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        # Not in the worker's requirements on purpose; it lives in the
        # embedder runtime and mypy has nothing to check it against.
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the runtime
        raise EmbeddingError(
            "sentence-transformers is not installed in this runtime;"
            " run scripts/install_embedder.sh and use the embedder runtime"
        ) from exc
    try:
        return SentenceTransformer(name, device="cpu", cache_folder=None)
    except Exception as exc:  # pragma: no cover - depends on the cache
        raise EmbeddingError(f"could not load {name}: {exc}") from exc


def prepare(text: str, *, is_query: bool) -> str:
    prefix = QUERY_PREFIX if is_query else PASSAGE_PREFIX
    return prefix + " ".join(text.split())[:MAX_CHARS]


def encode(
    model: Any, texts: Sequence[str], *, is_query: bool, batch_size: int = BATCH_SIZE
) -> list[list[float]]:
    """Encode, normalised, so cosine distance is a dot product."""
    prepared = [prepare(text, is_query=is_query) for text in texts]
    vectors = model.encode(
        prepared,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return [[float(value) for value in row] for row in vectors]


def to_pgvector(vector: Sequence[float]) -> str:
    """pgvector's text form. Six decimals is well inside float32 precision."""
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


def text_fingerprint(text: str) -> str:
    """What was embedded, by hash. A vector whose source cannot be identified is
    a number nobody can check."""
    return sha256_bytes(text.encode("utf-8"))
