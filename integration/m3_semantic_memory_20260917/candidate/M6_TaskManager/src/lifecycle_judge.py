"""Compact model proposal; program resolves IDs and source-backed field values."""
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from .models import LifecycleAction, LifecycleDecision, TechnicalFailure


class LifecycleJudge:
    def __init__(self, client):
        root=Path(__file__).resolve().parents[1]
        self.client=client
        self.calls=0
        self.format_retries=0
        self.service_failures=0
        self.system_prompt=(root/'prompts/lifecycle_judge.md').read_text(encoding='utf-8')
        self.validator=Draft202012Validator(json.loads((root/'schemas/lifecycle_decision.schema.json').read_text()))

    def judge(self, source, candidates, *, expanded=False, correction=None):
        payload=self._payload(source,candidates)
        payload['retrieval_expanded']=expanded
        audit={'calls':[],'format_retries':0}
        for attempt in range(2):
            try:
                self.calls+=1
                response,call_audit=self.client.decide(self.system_prompt,payload,correction=correction)
                audit['calls'].append(call_audit)
            except (json.JSONDecodeError,ValueError) as error:
                response=None;correction='JSON解析错误：'+str(error)[:250]
            except Exception as error:
                self.service_failures+=1
                raise TechnicalFailure('MODEL_SERVICE: '+type(error).__name__) from error
            if isinstance(response,dict):
                response=self._normalize_shape(response)
                errors=self._errors(response)
                index=response.get('target_index')
                if isinstance(index,int) and not isinstance(index,bool) and not 0<=index<len(candidates):
                    errors.append('target_index 不在已提供的候选范围')
                fields={'title':source.item['title'],'description':source.item['content'],
                    'assignees':source.item.get('assignee') or [],'work_section':source.item.get('work_section'),
                    'delivery_group':source.item.get('delivery_group')}
                subs=[]
                if not errors and response['decision']=='MULTI':
                    for position,sub in enumerate(response['sub_decisions']):
                        sub_index=sub.get('target_index')
                        if isinstance(sub_index,int) and not isinstance(sub_index,bool) and not 0<=sub_index<len(candidates):
                            errors.append(f'sub_decisions[{position}] target_index 不在已提供的候选范围')
                            continue
                        sub_target=candidates[sub_index] if sub_index is not None else None
                        sub_change=None
                        if sub['decision']=='TRANSFER':
                            sub_change={'from_department':sub_target.get('department') if sub_target else None,
                                        'to_department':sub.get('transfer_to'),'evidence':sub['evidence']}
                        subs.append(LifecycleDecision(
                            decision=LifecycleAction(sub['decision']),
                            target_task_id=sub_target['task_id'] if sub_target else None,
                            reason=sub['reason'],event_summary=None,
                            changes={name:fields[name] for name in sub['fields']},
                            department_change=sub_change,scope=sub['scope'],
                            completion_evidence=sub['evidence']))
                if not errors and response['decision']=='MULTI':
                    return LifecycleDecision(
                        decision=LifecycleAction.MULTI,target_task_id=None,
                        reason=response['reason'],event_summary=None,changes={},
                        department_change=None,scope=response['scope'],
                        sub_decisions=tuple(subs)),audit
                if not errors:
                    target=candidates[index] if index is not None else None
                    change=None
                    if response['decision']=='TRANSFER':
                        change={'from_department':target.get('department') if target else None,
                                'to_department':response.get('transfer_to'),'evidence':response['evidence']}
                    return LifecycleDecision(
                        decision=LifecycleAction.REVIEW if response['decision']=='EXPAND' else LifecycleAction(response['decision']),
                        target_task_id=target['task_id'] if target else None,reason=response['reason'],event_summary=None,
                        changes={name:fields[name] for name in response['fields']},department_change=change,
                        scope=response['scope'],completion_evidence=response['evidence'],expand=response['decision']=='EXPAND'),audit
                correction='; '.join(errors)
            if attempt==0:
                audit['format_retries']+=1
                self.format_retries+=1
        raise TechnicalFailure('MODEL_FORMAT: finite repair exhausted; '+str(correction)[:300])

    @staticmethod
    def _normalize_shape(payload):
        if set(payload)=={'lifecycle_decision'} and isinstance(payload['lifecycle_decision'],dict):
            payload=payload['lifecycle_decision']
        return payload

    def _errors(self,payload):
        return [e.message for e in self.validator.iter_errors(payload)][:5]

    @staticmethod
    def _payload(source,candidates):
        fields=('item_type','project','source_project','identity_uncertain','department','work_section','delivery_group','title','description','assignees','status','recent_events')
        return {'current_item':source.item,'source_project':(source.admission or {}).get('original_item',source.item).get('project'),'historical_candidates':[
            {'index':i,**{k:c.get(k) for k in fields}} for i,c in enumerate(candidates)]}
