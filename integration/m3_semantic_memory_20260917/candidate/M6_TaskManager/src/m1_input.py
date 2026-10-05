"""Native M1 input: strict nine-field schema, one source row per operation, no M2."""
import hashlib
import json
from pathlib import Path
from jsonschema import Draft7Validator
from .identifiers import source_item_id
from .models import InputContractError, SourceContext


def load_m1_payload(path, *, document_id=None):
    payload=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload,dict) or set(payload)-{'items','mode','source_document_id'}:
        raise InputContractError('M1输入仅允许items及已有mode/source_document_id封装；不接受M2归并结果')
    schema=Path(__file__).resolve().parents[2]/'M1_Extraction/schemas/m1_items.schema.json'
    if not schema.is_file():raise InputContractError('M1 Schema缺失')
    errors=list(Draft7Validator(json.loads(schema.read_text())).iter_errors({'items':payload.get('items')}))
    if errors:raise InputContractError('M1 Schema错误：'+errors[0].message)
    document_id=document_id or payload.get('source_document_id')
    if not isinstance(document_id,str) or not document_id.strip():raise InputContractError('来源文档身份缺失')
    mode=payload.get('mode','generic')
    if mode not in ('generic','block'):raise InputContractError('M1 mode非法')
    contexts=[]
    for index,item in enumerate(payload['items']):
        evidence=item['evidence']
        if (not evidence['text'].strip() or evidence['start_char']>evidence['end_char']
                or (evidence['page_start'] is not None and evidence['page_end'] is not None and evidence['page_start']>evidence['page_end'])):
            raise InputContractError(f'M1条目{index}来源缺失或坐标倒置')
        trace={'item_index':index,'source_indexes':[index],'source_evidence':[evidence],
            'merged':False,'project_entity_id':None,'merge_reason':'M1直接输入，一对一来源映射，未执行语义归并'}
        project=item.get('project')
        entity='RAW-'+hashlib.sha256(project.encode()).hexdigest()[:20] if project else None
        contexts.append(SourceContext(source_document_id=document_id,
            source_item_id=source_item_id(document_id,index,item,trace),source_mode=mode,item_index=index,item=item,
            merge_trace=trace,project_entity_id=entity,project_entities=[],m2_validation={'status':'PASS','issues':[]},
            input_stage='M1',origin_document_id=payload.get('source_document_id') or document_id))
    return document_id,contexts
