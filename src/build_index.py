from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Iterable

try:
    import chromadb
    from chromadb.api.models.Collection import Collection

    HAS_CHROMADB = True
except Exception:
    HAS_CHROMADB = False
    Collection = object  # type: ignore[assignment,misc]

DOCS_DIR = Path("docs")
CHROMA_DIR = Path("storage/chroma")
COLLECTION_NAME = "rag_docs"
LOCAL_INDEX_PATH = Path("storage/local_index.json")
EMBED_DIM = 256


def iter_source_files(root: Path) -> Iterable[Path]:
    for ext in ("*.md", "*.txt"):
        yield from root.rglob(ext)


def chunk_text(text: str, chunk_size: int = 500, chunk_overlap: int = 80) -> list[str]:
    cleaned = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if not cleaned:
        return []

    chunks: list[str] = []
    start = 0
    step = max(1, chunk_size - chunk_overlap)
    while start < len(cleaned):
        end = min(len(cleaned), start + chunk_size)
        chunks.append(cleaned[start:end])
        start += step
    return chunks


def embed_text(text: str, dim: int = EMBED_DIM) -> list[float]:
    # Lightweight hashing embedding so the flow runs even when third-party packages are unavailable.
    vector = [0.0] * dim
    for token in text.lower().split():
        idx = hash(token) % dim
        vector[idx] += 1.0

    norm = math.sqrt(sum(v * v for v in vector))
    if norm > 0:
        vector = [v / norm for v in vector]
    return vector


class LocalEmbeddingFunction:
    def __call__(self, input: list[str]) -> list[list[float]]:
        return [embed_text(text) for text in input]


def build_local_index(records: list[dict[str, object]]) -> int:
    LOCAL_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "collection": COLLECTION_NAME,
        "dim": EMBED_DIM,
        "items": records,
    }
    LOCAL_INDEX_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"Indexed {len(records)} chunks into local index '{LOCAL_INDEX_PATH.as_posix()}'.")
    return len(records)


def load_collection(rebuild: bool) -> Collection:
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    if rebuild:
        existing = [c.name for c in client.list_collections()]
        if COLLECTION_NAME in existing:
            client.delete_collection(COLLECTION_NAME)

    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def build_index(rebuild: bool = False) -> int:
    if not DOCS_DIR.exists():
        raise FileNotFoundError(f"Source folder not found: {DOCS_DIR}")

    ids: list[str] = []
    docs: list[str] = []
    metadatas: list[dict[str, str | int]] = []
    local_records: list[dict[str, object]] = []

    for file_path in iter_source_files(DOCS_DIR):
        rel_path = file_path.as_posix()
        text = file_path.read_text(encoding="utf-8")
        chunks = chunk_text(text)

        for idx, chunk in enumerate(chunks):
            chunk_id = f"{rel_path}::chunk-{idx}"
            ids.append(chunk_id)
            docs.append(chunk)
            metadatas.append({"source": rel_path, "chunk": idx})
            local_records.append(
                {
                    "id": chunk_id,
                    "text": chunk,
                    "meta": {"source": rel_path, "chunk": idx},
                    "vector": embed_text(chunk),
                }
            )

    if not docs:
        print("No source documents found under docs/. Add .md or .txt files first.")
        return 0

    if HAS_CHROMADB:
        collection = load_collection(rebuild=rebuild)
        embeddings = [embed_text(doc) for doc in docs]
        collection.upsert(ids=ids, documents=docs, metadatas=metadatas, embeddings=embeddings)
        print(f"Indexed {len(docs)} chunks into collection '{COLLECTION_NAME}'.")
        return len(docs)

    if rebuild and LOCAL_INDEX_PATH.exists():
        LOCAL_INDEX_PATH.unlink()

    return build_local_index(local_records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build local vector index for RAG")
    parser.add_argument("--rebuild", action="store_true", help="Delete and rebuild collection")
    args = parser.parse_args()

    build_index(rebuild=args.rebuild)


if __name__ == "__main__":
    main()
