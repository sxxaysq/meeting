# M6 task lifecycle manager

M6 consumes native **M1 nine-field items** through the **M1 → M3 → M6** workflow. Each item retains one source index and its original evidence. M3 supplies project identity memory, task retrieval and semantic evidence interpretation; M6 validates and applies lifecycle decisions.

## Input and execution

`src/m1_input.py` accepts a JSON envelope containing `items`, optional `mode` (`generic` or `block`) and `source_document_id`. Each item has exactly `department`, `work_section`, `delivery_group`, `project`, `item_type`, `assignee`, `title`, `content` and `evidence`. A document ID can also be supplied explicitly. Unknown fields, missing evidence and invalid intervals are rejected before writes.

`src/input_contract.py` builds a one-to-one `SourceContext`. Provenance uses `source_trace`, `input_validation`, `input_stage=M1` and `origin_document_id`. `input_validation=PASS` records acceptance of the input contract; it is not a separate model quality assessment. Source IDs derive from the document, original index and original content/evidence, so accepted source replay does not call the model or execute again.

`TaskLifecycleService.process_m1_file` is the file entry point. The service runs bounded candidate retrieval and lifecycle reasoning. It supports one retrieval expansion, version refresh, duplicate recheck and bounded model recovery. `MULTI` decisions are validated and executed atomically. Model output cannot choose arbitrary task IDs, versions or field values.

The validator checks candidate membership, source fields, project compatibility, department routing, status transitions and expected versions. The executor commits tasks, events, source links, audits, processing records and dispatch records in one SQLite transaction. Business ambiguity creates a review record; technical failure remains distinguishable from business review. Dispatch records do not send external messages.

## Configured semantic runner

`../integration/m6_service/native_full_dataset.py` constructs the full M3 layer and injects it into M6. It consumes frozen native M1 input and runs chronological batch processing. Its replay checks and batch artifacts do not constitute a continuously resumable online service.

The module CLI exposes `init-db`, `import-history`, `process` and `inspect`. `process` is a native M1 adapter and isolated M6 processing entry. It does not inject the full project resolver, semantic task index or `SemanticReader`; use the semantic batch runner for the complete chain. A successful CLI run is not full semantic-chain acceptance. Model configuration uses explicit arguments or environment variables (`M6_LLM_BASE_URL`, with `LLM_BASE_URL` fallback); do not store secrets in source files.

## Tests and evaluation

From the candidate directory:

```bash
python -m pytest M6_TaskManager/tests -q
```

Tests cover native input rejection, source preservation, task matching, schema limits, grounded commands, bounded recovery, multi-goal transactions, rollback, optimistic version checks and idempotent replay. Unit tests use scripted model responses and isolated SQLite databases.

`eval/build_eval_set.py` creates annotation templates from native M1 envelopes. `eval/evaluate_lifecycle.py` evaluates labeled lifecycle samples; `lexical` is a simple comparison baseline. The gold sample set materializes nine-field M1 items. These sample metrics are separate from a live end-to-end evaluation of embedding, graph memory and model services.
