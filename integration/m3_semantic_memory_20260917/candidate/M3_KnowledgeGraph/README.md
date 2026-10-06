# M3 semantic memory and retrieval

M3 provides the memory and retrieval layer for the **M1 → M3 → M6** workflow. Native M1 items keep their original fields and evidence. M3 resolves project surfaces and families, retrieves historical task candidates, and reads completion/status/rename evidence. M6 remains responsible for validated commands and transactional execution.

## Modules

- `embedding_client.py`: embedding/reranking clients and caching.
- `semantic_memory.py`: persistent project surfaces, families, rules, task links and semantic evidence in Neo4j.
- `project_resolver.py`: memory lookup followed by vector recall and bounded model decisions. Unknown or unsupported identities remain unresolved.
- `project_memory.py`: source-backed, date-bounded project memories from committed task history.
- `task_retrieval.py`: incremental task vector index and bounded candidate retrieval, with project-family compatibility checks.
- `semantic_reader.py`: grounded completion, status-change and rename interpretation with reusable semantic facts.

These modules are configured by the semantic batch runner at `../integration/m6_service/native_full_dataset.py`. The runner builds the embedder, memory, resolver, reader and task index, then injects them into M6. Runtime endpoints and credentials come from deployment configuration and are not included here.

## Verification

From the candidate directory, run:

```bash
python -m pytest M3_KnowledgeGraph/tests -q
```

The semantic-layer tests use isolated in-memory doubles. Test collection never opens or clears an external Neo4j database. They cover remembered identities, candidate bounds, unresolved outages, grounded typo decisions, cached semantic interpretation and family-compatible retrieval. Live service integration requires a separate configured environment.
