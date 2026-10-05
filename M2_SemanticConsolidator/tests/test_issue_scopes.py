from conftest import make_item
from test_pipeline import _client
from pipeline import run_pipeline


def test_project_review_scopes_exclude_unrelated_items():
    items = [make_item(project='红沙泉二矿项目'), make_item(project='红沙泉项目', start=50, end=70),
             make_item(project=None, item_type='NON_PROJECT_WORK', start=100, end=120,
                       title='内部例会', content='召开内部例会。')]
    result = run_pipeline({'items':items}, 'generic',
        client=_client('KEEP_SEPARATE', entity={'decision':'UNCERTAIN','reason':'简称不明确'}))
    issues = [q for q in result.validation['issues'] if q['code']=='PROJECT_ENTITY_UNCERTAIN']
    assert issues
    for issue in issues:
        assert issue['detail']['item_indexes'] == [0,1]
        assert set(issue['detail']['project_names']) == {'红沙泉二矿项目','红沙泉项目'}


def test_uncertain_merge_has_scoped_original_singletons():
    result=run_pipeline({'items':[make_item(start=0,end=20),make_item(start=30,end=50)]},
                        'generic',client=_client('UNCERTAIN'))
    issues=[q for q in result.validation['issues'] if q['code']=='MERGE_UNCERTAIN']
    assert issues and issues[0]['detail']['item_indexes']==[0,1]
    assert all(not trace.merged and len(trace.source_indexes)==1 for trace in result.merge_trace)


def test_transitive_merge_cannot_bypass_uncertainty(monkeypatch):
    from models import PairJudgement
    from pipeline import MergeJudge
    judgements=[PairJudgement(0,1,'MERGE'),PairJudgement(1,2,'MERGE'),PairJudgement(0,2,'UNCERTAIN')]
    monkeypatch.setattr(MergeJudge,'judge',lambda self,*args:judgements)
    result=run_pipeline({'items':[make_item(start=i*30,end=i*30+20) for i in range(3)]},
                        'generic',client=_client('MERGE','KEEP_CLUSTER'))
    assert len(result.items)==3 and all(not trace.merged for trace in result.merge_trace)
