from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from query_srs_rag import (
  build_prompt,
  local_fallback,
  normalize_keywords,
  plan_retrieval,
  retrieve,
  try_ollama_generate,
  try_openai_generate,
)

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY", "False")

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ultasound System Requirement RAG</title>
  <style>
    :root {
      --bg: #f6f3ec;
      --panel: #ffffff;
      --ink: #1e2428;
      --muted: #6a7178;
      --line: #d8d2c6;
      --accent: #0f766e;
      --accent-dark: #115e59;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: linear-gradient(135deg, #f6f3ec 0%, #eaf1ef 55%, #eef0f4 100%);
      color: var(--ink);
      font: 15px/1.55 Georgia, "Times New Roman", serif;
    }
    main {
      width: min(1120px, calc(100vw - 32px));
      margin: 28px auto;
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 18px;
    }
    aside, section {
      background: rgba(255,255,255,.88);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 12px 30px rgba(30,36,40,.08);
    }
    aside { padding: 18px; }
    h1 { margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }
    label { display: block; margin: 18px 0 8px; font-weight: 700; }
    textarea {
      width: 100%; min-height: 132px; resize: vertical;
      border: 1px solid var(--line); border-radius: 6px; padding: 12px;
      font: 14px/1.5 Consolas, "Courier New", monospace;
    }
    .row { display: flex; gap: 10px; align-items: center; margin-top: 12px; }
    input[type="number"] { width: 92px; padding: 8px; border: 1px solid var(--line); border-radius: 6px; }
    button {
      border: 0; border-radius: 6px; padding: 10px 14px;
      color: white; background: var(--accent); cursor: pointer; font-weight: 700;
    }
    button:hover { background: var(--accent-dark); }
    button:disabled { opacity: .6; cursor: wait; }
    section { min-height: calc(100vh - 56px); padding: 18px; }
    #answer { margin: 0 0 18px; }
    #answer p { margin: 0 0 10px; }
    #answer ol, #answer ul { margin: 0 0 14px 22px; padding: 0; }
    #answer li { margin: 6px 0; }
    #answer strong { color: var(--accent-dark); }
    #plan { white-space: pre-wrap; margin: 0 0 18px; padding: 12px; background: #f3f7f6; border: 1px solid var(--line); border-radius: 6px; font-family: Consolas, "Courier New", monospace; }
    .hit { border-top: 1px solid var(--line); padding: 14px 0; }
    .meta { color: var(--accent-dark); font-weight: 700; margin-bottom: 6px; }
    .doc { color: var(--ink); }
    @media (max-width: 820px) { main { grid-template-columns: 1fr; } section { min-height: auto; } }
  </style>
</head>
<body>
  <main>
    <aside>
      <h1>Ultasound System Requirement RAG</h1>
      <label for="q">问题或关键词</label>
      <textarea id="q"></textarea>
      <div class="row">
        <label for="topk" style="margin:0">Top-K</label>
        <input id="topk" type="number" min="1" max="100" value="25" />
        <label for="retrievek" style="margin:0">Recall</label>
        <input id="retrievek" type="number" min="1" max="500" value="100" />
      </div>
      <div class="row"><button id="ask">查询</button></div>
    </aside>
    <section>
      <pre id="plan">检索前分析会显示在这里...</pre>
      <div id="answer">等待查询...</div>
      <div id="hits"></div>
    </section>
  </main>
  <script>
    const ask = document.getElementById('ask');
    const answer = document.getElementById('answer');
    const plan = document.getElementById('plan');
    const hits = document.getElementById('hits');

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, (char) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[char]));
    }

    function renderInlineMarkdown(value) {
      return escapeHtml(value).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    }

    function renderMarkdown(value) {
      const lines = String(value || '').split(/\r?\n/);
      let html = '';
      let listType = '';

      function closeList() {
        if (listType) {
          html += `</${listType}>`;
          listType = '';
        }
      }

      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) {
          closeList();
          continue;
        }

        const ordered = line.match(/^\d+\.\s+(.+)$/);
        const bullet = line.match(/^[-*]\s+(.+)$/);
        if (ordered) {
          if (listType !== 'ol') {
            closeList();
            html += '<ol>';
            listType = 'ol';
          }
          html += `<li>${renderInlineMarkdown(ordered[1])}</li>`;
          continue;
        }
        if (bullet) {
          if (listType !== 'ul') {
            closeList();
            html += '<ul>';
            listType = 'ul';
          }
          html += `<li>${renderInlineMarkdown(bullet[1])}</li>`;
          continue;
        }

        closeList();
        html += `<p>${renderInlineMarkdown(line)}</p>`;
      }

      closeList();
      return html || '<p>无回答。</p>';
    }

    ask.addEventListener('click', async () => {
      ask.disabled = true;
      answer.textContent = '查询中...';
      plan.textContent = '检索前分析中...';
      hits.innerHTML = '';
      try {
        const rsp = await fetch('/api/query', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            question: document.getElementById('q').value,
            top_k: Number(document.getElementById('topk').value || 25),
            retrieve_k: Number(document.getElementById('retrievek').value || 100)
          })
        });
        const data = await rsp.json();
        if (!rsp.ok) throw new Error(data.error || rsp.statusText);
        plan.textContent = JSON.stringify(data.retrieval_plan || {}, null, 2);
        answer.innerHTML = renderMarkdown(data.answer);
        hits.innerHTML = data.hits.map((hit, idx) => `
          <div class="hit">
            <div class="meta">${idx + 1}. [${hit.req_id || 'unknown'}] ${hit.section || ''}</div>
            <div class="doc">${hit.document}</div>
          </div>`).join('');
      } catch (err) {
        answer.textContent = '查询失败: ' + err.message;
      } finally {
        ask.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/query":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            question = str(payload.get("question", "")).strip()
            top_k = int(payload.get("top_k", 25))
            retrieve_k = max(int(payload.get("retrieve_k", 100)), top_k)
            if not question:
                raise ValueError("question is required")

            retrieval_plan = plan_retrieval(question)
            search_question = str(retrieval_plan.get("search_query", "")).strip() or question
            exclude_keywords = normalize_keywords(retrieval_plan.get("exclude_keywords"))
            docs, metas = retrieve(search_question, top_k=top_k, retrieve_k=retrieve_k, exclude_keywords=exclude_keywords)
            prompt = build_prompt(question, docs, metas, retrieval_plan=retrieval_plan)
            answer = try_ollama_generate(prompt) or try_openai_generate(prompt) or local_fallback(question, docs, metas)
            hits = []
            for doc, meta in zip(docs, metas):
                meta = meta if isinstance(meta, dict) else {}
                hits.append({
                    "req_id": meta.get("req_id", ""),
                    "section": meta.get("section", ""),
                    "row": meta.get("row", ""),
                    "document": doc,
                })

            body = json.dumps(
              {"answer": answer, "hits": hits, "retrieval_plan": retrieval_plan},
              ensure_ascii=False,
            ).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        except Exception as ex:
            body = json.dumps({"error": str(ex)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def log_message(self, format: str, *args: object) -> None:
        print("web:", format % args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local SRS RAG web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if not Path("storage/chroma").exists():
        raise FileNotFoundError("storage/chroma not found. Run build_srs_index.py first.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SRS RAG web UI: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
