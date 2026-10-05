"""One real-model HTTP smoke run, isolated from the production lifecycle DB."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from service import ROOT, create_app


def main():
    with TemporaryDirectory(prefix='m6-live-smoke-') as temporary:
        app = create_app(Path(temporary), ROOT / 'integration/m2_service/data/m2')
        app.state.repository.upsert_department('D-SMOKE', '智能矿山事业部', 'test://never-send')
        payload = json.loads((ROOT / 'M6_TaskManager/examples/m2_pass.sample.json').read_text())
        body = {'source_document_id': 'SMOKE-EXAMPLE-ONLY', 'm2_payload': payload}
        with TestClient(app) as http:
            response = http.post('/m6/process', json=body)
            assert response.status_code == 200, response.text
            result = response.json()
            record = result['records'][0]
            assert record['llm_called'], result
            assert 'model_failure' not in record['model_audit'], result
            assert record['decision'] is not None, result
            replay = http.post('/m6/process', json=body).json()
            assert replay['idempotent_replay'] is True
            report = {'isolated': True, 'production_writes': False,
                      'model': record['model_audit'].get('model'),
                      'action_counts': result['action_counts'],
                      'execution_status_counts': result['execution_status_counts'],
                      'replay_verified': True}
            if len(sys.argv) > 1:
                Path(sys.argv[1]).write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps(report))


if __name__ == '__main__':
    main()
