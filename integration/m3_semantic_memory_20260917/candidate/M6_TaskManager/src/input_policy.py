"""Conservative restrictions for validated M1 sources and unresolved task reviews."""
from .candidate_retriever import normalize_text, project_compatible, same_goal
from .models import InputContractError

HARD_CODES = {"SCHEMA_VIOLATION", "PROVENANCE_MISSING", "CONTENT_NOT_GROUNDED",
              "TITLE_NOT_GROUNDED", "ASSIGNEE_NOT_GROUNDED"}


def restriction(source, action=None):
    validation = source.input_validation
    if validation.get("status") == "ERROR":
        raise InputContractError("Input contract validation failed")
    for issue in validation.get("issues", []):
        code = issue["code"]
        if issue["level"] == "error" or code in HARD_CODES:
            raise InputContractError("Invalid source item: " + code)
        if issue["level"] != "review":
            continue
        scope = issue.get("detail", {}).get("item_indexes")
        if scope is None and "item_index" in issue:
            scope = [issue["item_index"]]
        if not scope:
            return "INPUT_REVIEW_UNSCOPED:" + code
        if source.item_index in scope:
            return "INPUT_ITEM_REVIEW:" + code
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
