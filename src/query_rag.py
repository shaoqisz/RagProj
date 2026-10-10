from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

try:
    import chromadb

    HAS_CHROMADB = True
except Exception:
    HAS_CHROMADB = False

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv() -> None:
        return None

CHROMA_DIR = Path("storage/chroma")
COLLECTION_NAME = "rag_docs"
LOCAL_INDEX_PATH = Path("storage/local_index.json")


def get_collection():
    if not CHROMA_DIR.exists():
        raise FileNotFoundError("Vector database not found. Run build_index.py first.")

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(name=COLLECTION_NAME)


def embed_text(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    for token in text.lower().split():
        vector[hash(token) % dim] += 1.0

    norm = math.sqrt(sum(v * v for v in vector))
    if norm > 0:
        vector = [v / norm for v in vector]
    return vector


def cosine(v1: list[float], v2: list[float]) -> float:
    return sum(a * b for a, b in zip(v1, v2))


def retrieve_local(question: str, top_k: int) -> tuple[list[str], list[dict]]:
    if not LOCAL_INDEX_PATH.exists():
        raise FileNotFoundError("Local index not found. Run build_index.py first.")

    payload = json.loads(LOCAL_INDEX_PATH.read_text(encoding="utf-8"))
    dim = int(payload.get("dim", 256))
    items = payload.get("items", [])
    if not isinstance(items, list):
        return [], []

    qv = embed_text(question, dim)
    scored: list[tuple[float, str, dict]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", ""))
        meta = item.get("meta", {})
        vec = item.get("vector", [])
        if not isinstance(vec, list):
            continue
        vec = [float(v) for v in vec]
        score = cosine(qv, vec)
        scored.append((score, text, meta if isinstance(meta, dict) else {}))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]
    return [t[1] for t in top], [t[2] for t in top]


def retrieve(question: str, top_k: int) -> tuple[list[str], list[dict]]:
    if HAS_CHROMADB:
        collection = get_collection()
        qv = embed_text(question, dim=256)
        result = collection.query(query_embeddings=[qv], n_results=top_k)

        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        return docs, metas

    return retrieve_local(question, top_k)


def build_prompt(question: str, chunks: list[str], metas: list[dict]) -> str:
    context_blocks = []
    for idx, (chunk, meta) in enumerate(zip(chunks, metas), start=1):
        src = meta.get("source", "unknown") if isinstance(meta, dict) else "unknown"
        context_blocks.append(f"[{idx}] source={src}\n{chunk}")

    context = "\n\n".join(context_blocks)
    return (
        "You are a helpful assistant. Answer only using the provided context. "
        "If information is insufficient, clearly say so.\n\n"
        f"Question:\n{question}\n\n"
        f"Context:\n{context}\n"
    )


def try_openai_generate(prompt: str) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        rsp = client.chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": "Answer in concise Chinese."},
                {"role": "user", "content": prompt},
            ],
        )
        return (rsp.choices[0].message.content or "").strip()
    except Exception as ex:
        return f"[OpenAI generation failed] {ex}"


def local_fallback_answer(question: str, chunks: list[str], metas: list[dict]) -> str:
    lines = [
        "未检测到 OPENAI_API_KEY，当前使用本地回退模式（仅展示检索结果）。",
        f"问题: {question}",
        "",
        "命中片段:",
    ]

    for i, (chunk, meta) in enumerate(zip(chunks, metas), start=1):
        src = meta.get("source", "unknown") if isinstance(meta, dict) else "unknown"
        preview = chunk[:220].replace("\n", " ")
        lines.append(f"{i}. [{src}] {preview}")

    return "\n".join(lines)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Query local RAG index")
    parser.add_argument("question", type=str, help="Natural language question")
    parser.add_argument("--top-k", type=int, default=4, help="Number of chunks to retrieve")
    args = parser.parse_args()

    chunks, metas = retrieve(args.question, top_k=args.top_k)
    if not chunks:
        print("No retrieved chunks. Build index and check docs content first.")
        return

    prompt = build_prompt(args.question, chunks, metas)
    answer = try_openai_generate(prompt)

    if answer is None:
        answer = local_fallback_answer(args.question, chunks, metas)

    print("\n=== Answer ===")
    print(answer)


if __name__ == "__main__":
    main()
