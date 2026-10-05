"""FastAPI adapter for the M6 core; M2 JSON remains authoritative."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from M6_TaskManager.src.candidate_retriever import CandidateRetriever
from M6_TaskManager.src.command_validator import CommandValidator
from M6_TaskManager.src.department_router import DepartmentRouter
from M6_TaskManager.src.executor import TaskExecutor
from M6_TaskManager.src.lifecycle_judge import LifecycleJudge
from M6_TaskManager.src.llm_client import OpenAICompatibleLifecycleClient
from M6_TaskManager.src.m2_input import load_m2_payload
from M6_TaskManager.src.models import InputContractError
from M6_TaskManager.src.repository import TaskRepository
from M6_TaskManager.src.service import TaskLifecycleService, atomic_write_json

TABLES = Literal['tasks', 'task_events', 'task_source_links', 'task_audit',
                 'lifecycle_reviews', 'processing_records', 'departments', 'dispatch_queue']


class ProcessInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_document_id: str = Field(min_length=1, max_length=240, pattern=r'^[^/\\\x00-\x1f]+$')
    m2_payload: dict | None = None


def create_app(data_dir: Path | None = None, m2_dir: Path | None = None, client=None, allow_test_documents=False):
    data_dir = Path(data_dir or os.getenv('M6_DATA_DIR', Path(__file__).parent / 'data')).resolve()
    m2_dir = Path(m2_dir or os.getenv('M6_M2_DIR', ROOT / 'integration/m2_service/data/m2')).resolve()
    for name in ('inputs', 'results', 'rejected'):
        (data_dir / name).mkdir(parents=True, exist_ok=True)
    if not (ROOT / 'M2_SemanticConsolidator/schemas/m2_output.schema.json').is_file():
        raise RuntimeError('M2 schema missing; refusing to start without validation')
    repository = TaskRepository(data_dir / 'lifecycle.sqlite')
    repository.initialize()
    app = FastAPI(title='M6 Task Lifecycle Service', version='1.0.0')
    app.state.repository = repository

    @contextmanager
    def writer():
        # ponytail: serialize lifecycle runs across workers; shard by business scope if needed.
        with (data_dir / 'process.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HTTPException(409, 'Another M6 run is in progress; retry later')
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def paths(doc_id):
        key = hashlib.sha256(doc_id.encode()).hexdigest()
        return data_dir / 'inputs' / (key + '.json'), data_dir / 'results' / (key + '.json')

    def reject(raw, reason):
        receipt = uuid4().hex
        atomic_write_json(data_dir / 'rejected' / (receipt + '.json'),
                          {'reason': reason, 'payload': raw})
        return receipt

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        receipt = reject((await request.body()).decode('utf-8', errors='replace'), 'Invalid HTTP input')
        return JSONResponse(status_code=422, content={'detail': 'Invalid HTTP input', 'receipt': receipt})

    def read_source(doc_id):
        path = (m2_dir / (doc_id + '.m2.json')).resolve()
        if path.parent != m2_dir:
            raise HTTPException(422, 'Source must be inside the configured M2 directory')
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise HTTPException(404, 'M2 document not found')

    def validate(raw, doc_id, archive_rejection=True):
        temporary = data_dir / 'inputs' / (uuid4().hex + '.tmp')
        temporary.write_bytes(raw)
        try:
            load_m2_payload(temporary, document_id=doc_id, require_pass=False)
            payload = json.loads(raw)
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        except (InputContractError, ValueError, TypeError, KeyError) as error:
            reason = str(error)[:2000]
            detail = {'message': reason}
            if archive_rejection:
                detail['receipt'] = reject(raw.decode('utf-8', errors='replace'), reason)
            raise HTTPException(422, detail)
        finally:
            temporary.unlink(missing_ok=True)

    def process(body):
        if body.source_document_id.startswith('E2E-') and not allow_test_documents:
            raise HTTPException(422, 'Synthetic E2E documents must use the isolated /test service')
        raw = (json.dumps(body.m2_payload, ensure_ascii=False).encode()
               if body.m2_payload is not None else read_source(body.source_document_id))
        canonical = validate(raw, body.source_document_id)
        input_path, output_path = paths(body.source_document_id)
        if input_path.exists() and input_path.read_bytes() != canonical:
            raise HTTPException(409, 'M2 source changed after first acceptance; explicit reconciliation required')
        if output_path.exists():
            cached = json.loads(output_path.read_text())
            if not cached.get('action_counts', {}).get('TECHNICAL_FAILURE'):
                return {**cached, 'idempotent_replay': True}
        if not input_path.exists():
            temporary = input_path.with_suffix('.tmp')
            temporary.write_bytes(canonical)
            temporary.replace(input_path)
        lifecycle_client = client or OpenAICompatibleLifecycleClient(
            base_url=os.getenv('LLM_BASE_URL', 'http://192.168.30.215:8000/v1'),
            model=os.getenv('LLM_MODEL', 'qwen3.8-27b'),
            api_key=os.getenv('LLM_API_KEY', 'EMPTY'),
            timeout_seconds=float(os.getenv('LLM_TIMEOUT', '180')),
            enable_thinking=False,
        )
        lifecycle = TaskLifecycleService(
            repository=repository, retriever=CandidateRetriever(repository),
            judge=LifecycleJudge(lifecycle_client),
            validator=CommandValidator(repository, DepartmentRouter(repository)),
            executor=TaskExecutor(repository),
        )
        try:
            # Core commits each item atomically; retries resume through processing_records.
            result = lifecycle.process_file(input_path, output_path=output_path,
                                            document_id=body.source_document_id)
        except Exception:
            logging.exception('M6 run interrupted; retry will resume committed items')
            raise HTTPException(500, 'M6 run interrupted; safe to retry the same input')
        return {**result, 'idempotent_replay': False}

    def pending_documents():
        if not m2_dir.is_dir():
            raise HTTPException(503, 'Configured M2 directory is unavailable')
        pending, blocked = [], []
        for path in sorted(m2_dir.glob('*.m2.json')):
            doc_id = path.name[:-len('.m2.json')]
            if doc_id.startswith('E2E-') and not allow_test_documents:
                continue
            try:
                ProcessInput(source_document_id=doc_id)
                raw = read_source(doc_id)
                canonical = validate(raw, doc_id, archive_rejection=False)
                saved, output = paths(doc_id)
                if saved.exists() and saved.read_bytes() != canonical:
                    blocked.append({'source_document_id': doc_id, 'reason': 'SOURCE_CHANGED'})
                elif not output.exists() or json.loads(output.read_text()).get('action_counts', {}).get('TECHNICAL_FAILURE'):
                    pending.append({'source_document_id': doc_id})
                elif json.loads(output.read_text()).get('action_counts', {}).get('REJECTED'):
                    blocked.append({'source_document_id':doc_id,'reason':'REJECTED_OPERATIONS'})
            except (HTTPException, ValueError) as error:
                blocked.append({'source_document_id': doc_id, 'reason': str(getattr(error, 'detail', error))})
        return {'pending_count': len(pending), 'pending': pending, 'blocked_count': len(blocked), 'blocked': blocked}

    @app.get('/healthz')
    def health():
        with repository.connect() as connection:
            connection.execute('SELECT 1').fetchone()
        return {'status': 'ok', 'python': sys.version.split()[0], 'dialect': 'sqlite',
                'input_policy': 'M2_ITEM_OPERATION_POLICY', 'm2_source_available': m2_dir.is_dir(),
                'department_count': len(repository.list_departments()),
                'dispatch_mode': 'queue_only',
                'llm_model': os.getenv('LLM_MODEL', 'qwen3.8-27b')}

    @app.post('/m6/process')
    def process_document(body: ProcessInput):
        with writer():
            return result_response(process(body))

    def result_response(value):
        counts=value.get('action_counts',{})
        if counts.get('TECHNICAL_FAILURE'):
            return JSONResponse(status_code=503,content={**value,'retryable':True})
        if counts.get('REJECTED'):
            return JSONResponse(status_code=422,content={**value,'retryable':False})
        return value

    @app.get('/m6/pending')
    def pending():
        return pending_documents()

    @app.post('/m6/run-pending')
    def run_pending(limit: int = Query(default=1, ge=1, le=10)):
        with writer():
            state = pending_documents()
            results = [process(ProcessInput(**row)) for row in state['pending'][:limit]]
            return {'processed_count': len(results), 'results': results, 'blocked': state['blocked'],
                    'technical_failure_count':sum(r.get('action_counts',{}).get('TECHNICAL_FAILURE',0) for r in results),
                    'rejected_count':sum(r.get('action_counts',{}).get('REJECTED',0) for r in results)}

    @app.get('/m6/result')
    def result(source_document_id: str):
        _, output = paths(source_document_id)
        if not output.exists():
            raise HTTPException(404, 'No completed run for this document')
        return result_response(json.loads(output.read_text()))

    @app.get('/m6/export/{table}')
    def export(table: TABLES, limit: int = Query(default=100, ge=1, le=1000),
               offset: int = Query(default=0, ge=0)):
        # Table identifier comes exclusively from the Literal allowlist; values are bound.
        with repository.connect() as connection:
            connection.execute('BEGIN')
            total = connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            rows = connection.execute(f'SELECT * FROM {table} ORDER BY rowid LIMIT ? OFFSET ?',
                                      (limit, offset)).fetchall()
        return {'table': table, 'total': total, 'limit': limit, 'offset': offset,
                'has_more': offset + len(rows) < total, 'rows': [dict(row) for row in rows]}

    return app
