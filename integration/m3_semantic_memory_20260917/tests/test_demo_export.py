"""Regression for chronological legacy description projection (no model calls)."""
import importlib.util
import json
from pathlib import Path


def test_export_preserves_each_release_and_deduplicates_progress():
    path = Path(__file__).resolve().parents[1] / 'export_semantic_demo.py'
    spec = importlib.util.spec_from_file_location('semantic_export', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    audits, events = [], {}
    for index, (action, content) in enumerate([
        ('CREATE', '原始要求'), ('PROGRESS_UPDATE', '完成安装'),
        ('PROGRESS_UPDATE', '完成安装'), ('MODIFY', '开始调试'),
        ('TRANSFER', '移交运维'), ('COMPLETE', '验收完成'),
    ]):
        state = {'description': '原始要求'}
        audits.append({'task_id': 'T', 'source_document_id': 'D', 'source_item_id': str(index),
                       'action': action, 'before_state_json': json.dumps(state) if index else None,
                       'after_state_json': json.dumps(state)})
        events[('D', str(index), 'T')] = {'content': content}
    module.project_descriptions(audits, events)
    descriptions = [json.loads(a['after_state_json'])['description'] for a in audits]
    assert descriptions == ['原始要求', '原始要求\n完成安装', '原始要求\n完成安装',
                            '原始要求\n完成安装\n开始调试', '原始要求\n完成安装\n开始调试\n移交运维',
                            '原始要求\n完成安装\n开始调试\n移交运维']
    for index in range(1, len(audits)):
        assert json.loads(audits[index]['before_state_json'])['description'] == descriptions[index - 1]
    module.project_descriptions(audits, events)
    assert [json.loads(a['after_state_json'])['description'] for a in audits] == descriptions
