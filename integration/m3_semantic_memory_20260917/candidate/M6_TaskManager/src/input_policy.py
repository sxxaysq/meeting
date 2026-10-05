"""Deterministic item/operation restrictions; never trust a document PASS alone."""
from .candidate_retriever import normalize_text, project_compatible, same_goal
from .models import InputContractError

HARD_CODES = {'SCHEMA_VIOLATION', 'PROVENANCE_MISSING', 'CONTENT_NOT_GROUNDED',
              'TITLE_NOT_GROUNDED', 'ASSIGNEE_NOT_GROUNDED'}
DUPLICATE_RISKS = {'MERGE_UNCERTAIN', 'CLUSTER_REVIEW', 'UNDER_MERGE_SUSPECT'}


def restriction(source, action=None):
    validation = source.m2_validation
    if validation.get('status') == 'ERROR':
        raise InputContractError('M2 ERROR input cannot be executed')
    for issue in validation.get('issues', []):
        code = issue['code']
        if issue['level'] == 'error' or code in HARD_CODES:
            raise InputContractError('M2 hard error: ' + code)
        if issue['level'] != 'review':
            continue
        scope = issue.get('detail', {}).get('item_indexes')
        if scope is None and 'item_index' in issue:
            scope = [issue['item_index']]
            other = issue.get('detail', {}).get('other_item_index')
            if other is not None:
                scope.append(other)
        if not scope:
            return 'UNSCOPED_REVIEW: ' + code
        if source.item_index not in scope:
            continue
        split = not source.merge_trace.get('merged') and len(source.merge_trace['source_indexes']) == 1
        if code == 'STRUCTURE_CONFLICT_VETO' and split:
            continue  # The risky merge was already vetoed; normal per-item validation still applies.
        if code in DUPLICATE_RISKS and split:
            if action == 'CREATE':
                return 'DUPLICATE_NOT_EXCLUDED: ' + code
            continue
        return 'M2_ITEM_REVIEW: ' + code
    return None


def pending_restriction(source, repository, action=None):
    if action != 'CREATE':
        return None
    for review in repository.list_reviews():
        if review['status'] != 'PENDING':
            continue
        item = review['item']
        same_department = normalize_text(source.item.get('department')) == normalize_text(item.get('department'))
        if project_compatible(source,item) and same_department and same_goal(source.item,item):
            return 'PENDING_REVIEW_DUPLICATE: ' + review['review_id']
    return None
