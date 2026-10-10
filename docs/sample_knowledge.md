# RAG 项目示例知识库

这个文件用于演示本地向量数据库索引。

## 什么是 RAG

RAG 是 Retrieval-Augmented Generation 的缩写，核心流程是：
1. 对知识文档分块并建立向量索引。
2. 用户提问时先检索相关片段。
3. 将问题和片段一起交给大模型生成回答。

## 本项目做了什么

- 向量数据库: Chroma（本地持久化）
- 索引脚本: src/build_index.py
- 查询脚本: src/query_rag.py

## 注意事项

- 若未配置 OPENAI_API_KEY，查询脚本会返回检索片段作为回退模式。
- 添加新文档后，需要重新执行索引脚本。
