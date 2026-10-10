from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
import re
from urllib import error, request
from pathlib import Path
from typing import Any

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY", "False")

import chromadb

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv() -> None:
        return None

CHROMA_DIR = Path("storage/chroma")
COLLECTION_NAME = "srs_requirements"
EMBED_DIM = 256
TOKEN_RE = re.compile(r"[a-zA-Z0-9]+(?:[-_][a-zA-Z0-9]+)*")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "shall",
    "the",
    "to",
    "under",
    "with",
}
QUERY_STOP_WORDS = STOP_WORDS | {
    "database",
    "databases",
    "db",
    "requirement",
    "requirements",
    "srs",
    "需求",
    "数据库",
    "向量数据库",
    "modify",
    "modifies",
    "modified",
    "modifying",
    "modification",
    "modifications",
    "update",
    "updates",
    "updated",
    "updating",
    "change",
    "changes",
    "changed",
    "changing",
    "focus",
    "concern",
    "concerns",
    "know",
    "修改",
    "变更",
    "更改",
    "关注",
    "知道",
}


def normalize_keywords(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.strip()
        return [value] if value and value.lower() not in QUERY_STOP_WORDS else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip() and str(v).strip().lower() not in QUERY_STOP_WORDS]
    return []


def clean_search_query(text: str) -> str:
    for word in QUERY_STOP_WORDS:
        if re.search(r"[\u4e00-\u9fff]", word):
            text = text.replace(word, " ")
        else:
            text = re.sub(rf"\b{re.escape(word)}\b", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def detect_negated_keywords(question: str) -> list[str]:
    lowered = question.lower()
    negation_markers = ("不相关", "无关", "不要", "排除", "不包含", "not related", "unrelated", "exclude", "without")
    if not any(marker in lowered for marker in negation_markers):
        return []

    return [token for token in tokenize(question) if token not in QUERY_STOP_WORDS]


def contains_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def try_translate_query_with_api(question: str) -> str | None:
    if not contains_cjk(question):
        return None

    api_url = os.getenv("QUERY_TRANSLATE_API_URL", "").strip()
    if not api_url:
        return None

    payload = json.dumps({"query": question}).encode("utf-8")
    req = request.Request(
        api_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, error.URLError, json.JSONDecodeError):
        return None

    for key in ("query", "translated", "keywords", "english"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            terms = [str(v).strip() for v in value if str(v).strip()]
            if terms:
                return " ".join(terms)
    return None


def try_translate_query_to_english(question: str) -> str | None:
    if not contains_cjk(question):
        return None

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        rsp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Translate the Chinese SRS search query into concise English search keywords. "
                        "Return only English keywords, no explanation."
                    ),
                },
                {"role": "user", "content": question},
            ],
        )
        translated = (rsp.choices[0].message.content or "").strip()
        return translated or None
    except Exception:
        return None


def prepare_search_question(question: str) -> str:
    translated_by_api = try_translate_query_with_api(question)
    translated = try_translate_query_to_english(question)
    additions = [value for value in (translated_by_api, translated) if value]
    if additions:
        return f"{question} {' '.join(additions)}"
    return question


def try_ollama_chat_json(system_prompt: str, user_prompt: str, timeout: int = 120) -> dict[str, Any] | None:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")
    model = os.getenv(
        "OLLAMA_MODEL",
        "dagbs/qwen2.5-coder-14b-instruct-abliterated:q5_k_m",
    ).strip()
    if not base_url or not model:
        return None

    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": 0},
    }
    req = request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            rsp = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    message = rsp.get("message") if isinstance(rsp, dict) else None
    content = message.get("content", "") if isinstance(message, dict) else ""
    if not isinstance(content, str) or not content.strip():
        return None

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    return parsed if isinstance(parsed, dict) else None


def plan_retrieval(question: str) -> dict[str, Any]:
    deterministic_excludes = detect_negated_keywords(question)
    system_prompt = (
        "你是一个向量数据库的检索前处理大模型。你负责分析用户输入，判断用户最需要用哪些词汇在向量数据库中查询。"
        "知识库是英文 SRS requirements。请把用户意图转换为适合英文 SRS 检索的关键词。"
        "注意: 这个向量数据库本身就是需求数据库，requirement、requirements、SRS、需求、数据库不是有效检索关键词，除非用户明确要查这些词本身。"
        "关键词应当只保留具体主题、功能、模块、设备、约束、属性或动作。"
        "如果用户是在问为了修改、变更、调整某对象应该关注哪些需求，检索关键词只保留被修改对象，不要把 modify、modification、change、修改、关注、知道 作为关键词。"
        "如果用户表达不相关、排除、不要某主题，请把要排除的词放到 exclude_keywords。"
        "只返回 JSON，不要解释。JSON 字段: search_query, keywords, exclude_keywords, intent, reason。"
    )
    user_prompt = (
        "用户输入:\n"
        f"{question}\n\n"
        "返回示例:\n"
        "{\"search_query\":\"motherboard main board\",\"keywords\":[\"motherboard\",\"main board\"],"
        "\"exclude_keywords\":[],\"intent\":\"include\",\"reason\":\"用户要查主板相关需求\"}"
    )
    plan = try_ollama_chat_json(system_prompt, user_prompt)
    fallback_query = prepare_search_question(question)

    if not plan:
        return {
            "search_query": fallback_query,
            "keywords": [token for token in tokenize(fallback_query) if token not in QUERY_STOP_WORDS],
            "exclude_keywords": deterministic_excludes,
            "intent": "fallback",
            "reason": "未获得 LLM 检索前分析，使用规则检索。",
        }

    keywords = normalize_keywords(plan.get("keywords"))
    exclude_keywords = normalize_keywords(plan.get("exclude_keywords"))
    exclude_keywords = list(dict.fromkeys(exclude_keywords + deterministic_excludes))
    search_query = clean_search_query(str(plan.get("search_query", "")).strip())
    for excluded in exclude_keywords:
        search_query = re.sub(rf"\b{re.escape(excluded)}\b", " ", search_query, flags=re.IGNORECASE)
    search_query = " ".join(search_query.split())
    if not search_query:
        remaining_keywords = [keyword for keyword in keywords if keyword.lower() not in {e.lower() for e in exclude_keywords}]
        search_query = " ".join(remaining_keywords) if remaining_keywords else clean_search_query(fallback_query)

    return {
        "search_query": search_query,
        "keywords": keywords,
        "exclude_keywords": exclude_keywords,
        "intent": str(plan.get("intent", "include")).strip() or "include",
        "reason": str(plan.get("reason", "")).strip(),
    }


def embed_text(text: str, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    for token in text.lower().split():
        vec[hash(token) % dim] += 1.0

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text)]


def content_for_scoring(doc: str, meta: dict) -> str:
    req_id = str(meta.get("req_id", "")) if isinstance(meta, dict) else ""
    section = str(meta.get("section", "")) if isinstance(meta, dict) else ""
    return f"{req_id} {section} {doc}"


def contains_any_keyword(text: str, keywords: list[str]) -> bool:
    haystack = text.lower()
    haystack_tokens = set(tokenize(haystack))
    for keyword in keywords:
        key = keyword.lower().strip()
        if not key:
            continue
        if " " in key and key in haystack:
            return True
        if key in haystack_tokens:
            return True
    return False


def lexical_score(question: str, doc: str, meta: dict) -> float:
    query = question.strip().lower()
    haystack = content_for_scoring(doc, meta).lower()

    query_tokens = [t for t in tokenize(question) if t not in STOP_WORDS]
    if not query_tokens:
        query_tokens = tokenize(question)
    if not query_tokens:
        return 0.0

    doc_counts = Counter(tokenize(haystack))
    unique_query_tokens = list(dict.fromkeys(query_tokens))
    matched = [t for t in unique_query_tokens if doc_counts.get(t, 0) > 0]

    score = 0.0
    if query and query in haystack:
        score += 100.0

    for token in matched:
        score += 8.0
        score += min(doc_counts[token], 3) * 0.5

    if unique_query_tokens:
        score += (len(matched) / len(unique_query_tokens)) * 40.0

    for left, right in zip(query_tokens, query_tokens[1:]):
        if f"{left} {right}" in haystack:
            score += 6.0

    return score


def get_collection():
    if not CHROMA_DIR.exists():
        raise FileNotFoundError("Vector database not found. Run build_srs_index.py first.")

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(name=COLLECTION_NAME)


def retrieve(
    question: str,
    top_k: int,
    retrieve_k: int,
    exclude_keywords: list[str] | None = None,
) -> tuple[list[str], list[dict]]:
    collection = get_collection()
    exclude_keywords = exclude_keywords or []

    all_records = collection.get(include=["documents", "metadatas"])
    all_docs = all_records.get("documents", []) or []
    all_metas = all_records.get("metadatas", []) or []
    scored = []
    for doc, meta in zip(all_docs, all_metas):
        meta_dict = meta if isinstance(meta, dict) else {}
        if contains_any_keyword(content_for_scoring(doc, meta_dict), exclude_keywords):
            continue
        score = lexical_score(question, doc, meta_dict)
        if score > 0:
            scored.append((score, doc, meta_dict))

    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        candidates = scored[:retrieve_k]
        top = candidates[:top_k]
        return [item[1] for item in top], [item[2] for item in top]

    qv = embed_text(question)
    result = collection.query(query_embeddings=[qv], n_results=max(retrieve_k * 2, retrieve_k))

    raw_docs = result.get("documents", [[]])[0]
    raw_metas = result.get("metadatas", [[]])[0]
    filtered = []
    for doc, meta in zip(raw_docs, raw_metas):
        meta_dict = meta if isinstance(meta, dict) else {}
        if contains_any_keyword(content_for_scoring(doc, meta_dict), exclude_keywords):
            continue
        filtered.append((doc, meta_dict))

    docs = [item[0] for item in filtered[:top_k]]
    metas = [item[1] for item in filtered[:top_k]]
    return docs, metas


def build_prompt(question: str, docs: list[str], metas: list[dict], retrieval_plan: dict[str, Any] | None = None) -> str:
    blocks = []
    for i, (doc, meta) in enumerate(zip(docs, metas), start=1):
        req_id = meta.get("req_id", "") if isinstance(meta, dict) else ""
        section = meta.get("section", "") if isinstance(meta, dict) else ""
        sheet = meta.get("sheet", "") if isinstance(meta, dict) else ""
        blocks.append(f"[{i}] req_id={req_id} section={section} sheet={sheet}\n{doc}")

    context = "\n\n".join(blocks)
    plan_text = json.dumps(retrieval_plan or {}, ensure_ascii=False, indent=2)
    return (
        "你是一个向量数据库的检索后处理大模型。你负责分析用户输入和检索结果，回复用户。"
        "只能基于提供的 Context 回答，并引用相关 req_id。若 Context 不足，请明确说明。\n\n"
        f"Question:\n{question}\n\n"
        f"RetrievalPlan:\n{plan_text}\n\n"
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
            temperature=0.1,
            messages=[
                {"role": "system", "content": "用简洁中文回答。"},
                {"role": "user", "content": prompt},
            ],
        )
        return (rsp.choices[0].message.content or "").strip()
    except Exception as ex:
        return f"[OpenAI generation failed] {ex}"


def try_ollama_generate(prompt: str) -> str | None:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")
    model = os.getenv(
        "OLLAMA_MODEL",
        "dagbs/qwen2.5-coder-14b-instruct-abliterated:q5_k_m",
    ).strip()
    if not base_url or not model:
        return None

    url = f"{base_url}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": "用简洁中文回答，并引用相关 req_id。"},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0.1},
    }
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as ex:
        return f"[Ollama generation failed] {ex}"

    message = data.get("message") if isinstance(data, dict) else None
    if isinstance(message, dict):
        content = str(message.get("content", "")).strip()
        if content:
            return content
    return None


def local_fallback(question: str, docs: list[str], metas: list[dict]) -> str:
    lines = [
        "未检测到 OPENAI_API_KEY，当前仅返回关键词检索结果。",
        f"问题: {question}",
        "",
        "命中需求:",
    ]

    for i, (doc, meta) in enumerate(zip(docs, metas), start=1):
        req_id = meta.get("req_id", "unknown") if isinstance(meta, dict) else "unknown"
        section = meta.get("section", "") if isinstance(meta, dict) else ""
        preview = doc[:260].replace("\n", " ")
        section_label = f" {section}" if section else ""
        lines.append(f"{i}. [{req_id}]{section_label} {preview}")

    return "\n".join(lines)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Query SRS vector index")
    parser.add_argument("question", type=str, help="Keyword or question")
    parser.add_argument("--top-k", type=int, default=50, help="Number of requirements to retrieve")
    parser.add_argument("--retrieve-k", type=int, default=100, help="Candidate pool size before final ranking")
    args = parser.parse_args()

    retrieval_plan = plan_retrieval(args.question)
    search_question = str(retrieval_plan.get("search_query", "")).strip() or prepare_search_question(args.question)
    exclude_keywords = normalize_keywords(retrieval_plan.get("exclude_keywords"))
    retrieve_k = max(args.retrieve_k, args.top_k)
    docs, metas = retrieve(search_question, args.top_k, retrieve_k, exclude_keywords=exclude_keywords)
    if not docs:
        print("No retrieved requirements. Run build_srs_index.py first.")
        os._exit(0)
        return

    prompt = build_prompt(args.question, docs, metas, retrieval_plan=retrieval_plan)
    answer = try_ollama_generate(prompt)
    if answer is None:
        answer = try_openai_generate(prompt)
    if answer is None:
        answer = local_fallback(args.question, docs, metas)

    print("\n=== Answer ===")
    print("\n=== Retrieval Plan ===")
    print(json.dumps(retrieval_plan, ensure_ascii=False, indent=2), flush=True)
    print("\n=== Final Answer ===")
    print(answer, flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
