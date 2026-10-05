"""Department-scoped workbench routes (administrator preview, without login)."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse

from .database import connect
from .task_repository import _decode_task
from .organization import COMPANY, organization_key


PENDING = {'open', 'in_progress', 'blocked'}


def department_key(name):
    return organization_key(name)


def register_department_routes(app, repository, static_dir):
    router = APIRouter()

    def departments():
        with connect(repository.database_path) as connection:
            rows = connection.execute('''SELECT department,
                SUM(CASE WHEN is_deleted=0 AND status IN ('open','in_progress','blocked') THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN is_deleted=0 AND status='open' THEN 1 ELSE 0 END) AS open_count,
                SUM(CASE WHEN is_deleted=0 AND status='in_progress' THEN 1 ELSE 0 END) AS in_progress_count,
                SUM(CASE WHEN is_deleted=0 AND status='blocked' THEN 1 ELSE 0 END) AS blocked_count
                FROM tasks WHERE department IS NOT NULL AND TRIM(department) != ''
                GROUP BY department ORDER BY department''').fetchall()
            units = [dict(row) for row in connection.execute("SELECT * FROM organization_units WHERE kind!='company' ORDER BY sort_order")]
            aliases = [dict(row) for row in connection.execute('SELECT * FROM organization_aliases')]
        counts = {row['department']: dict(row) for row in rows}
        result = []
        for unit in units:
            name = unit['name']
            count = counts.pop(name, {})
            result.append({'department_id': unit['organization_id'], 'name': name,
                'url': '/departments/' + unit['organization_id'], 'kind': unit['kind'], 'source': unit['source'],
                'parent_id': unit['parent_id'],
                'aliases': [entry['alias'] for entry in aliases if entry['organization_id'] == unit['organization_id'] and entry['alias'] != name],
                **{key: count.get(key, 0) for key in ('pending_count', 'open_count', 'in_progress_count', 'blocked_count')}})
        # Preserve unknown responsibilities without guessing which formal unit they belong to.
        for name, count in counts.items():
            result.append({'department_id': department_key(name), 'name': name, 'url': '/departments/' + department_key(name),
                'kind': 'unmapped', 'source': '原有任务名称，待确认组织归属', 'parent_id': None, 'aliases': [],
                **{key: count[key] for key in ('pending_count', 'open_count', 'in_progress_count', 'blocked_count')}})
        return result

    def workspace(department_id):
        result = next((item for item in departments() if item['department_id'] == department_id
                       or any(department_key(alias) == department_id for alias in item['aliases'])), None)
        if result is None:
            raise HTTPException(status_code=404, detail='部门工作台不存在')
        return result

    def own_task(department_id, task_id):
        department = workspace(department_id)['name']
        with connect(repository.database_path) as connection:
            row = connection.execute('SELECT * FROM tasks WHERE task_id=? AND department=? AND is_deleted=0',
                                     (task_id, department)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='本部门未找到该任务')
        return _decode_task(dict(row))

    def meeting_metadata(meeting):
        return {key: meeting.get(key) for key in ('meeting_id', 'meeting_title', 'meeting_date', 'meeting_time', 'source_file')} if meeting else None

    @router.get('/api/organization')
    def organization():
        return {'company': COMPANY, 'organization_id': organization_key(COMPANY),
                'source': '用户提供的企业通讯录截图', 'units': departments()}

    @router.get('/api/departments')
    def get_departments():
        return departments()

    @router.get('/api/departments/{department_id}')
    def get_workspace(department_id: str):
        return workspace(department_id)

    @router.get('/departments/{department_id}')
    def page(department_id: str):
        target = workspace(department_id)
        if department_id != target['department_id']:
            return RedirectResponse(target['url'], status_code=307)
        return FileResponse(static_dir/'index.html', headers={'Cache-Control': 'no-store'})

    @router.get('/api/departments/{department_id}/tasks')
    def tasks(department_id: str, status: str | None = Query(default=None), meeting_id: str | None = Query(default=None)):
        department = workspace(department_id)['name']
        if status is not None and status not in PENDING:
            raise HTTPException(status_code=400, detail='部门待办仅包含待执行、进行中和受阻任务')
        sql = "SELECT * FROM tasks WHERE department=? AND is_deleted=0 AND status IN ('open','in_progress','blocked')"
        values = [department]
        if status:
            sql += ' AND status=?'; values.append(status)
        if meeting_id:
            sql += ' AND source_meeting_id=?'; values.append(meeting_id)
        with connect(repository.database_path) as connection:
            rows = connection.execute(sql+' ORDER BY updated_at DESC,task_id', values).fetchall()
        return [_decode_task(dict(row)) for row in rows]

    @router.get('/api/departments/{department_id}/meetings')
    def meetings(department_id: str):
        department = workspace(department_id)['name']
        with connect(repository.database_path) as connection:
            ids = {row[0] for row in connection.execute('''SELECT source_meeting_id FROM tasks
                WHERE department=? AND is_deleted=0
                UNION SELECT e.source_meeting_id FROM task_events e JOIN tasks t ON t.task_id=e.task_id
                WHERE t.department=? AND t.is_deleted=0''', (department, department))}
        return [meeting_metadata(item) for item in repository.list_meetings() if item['meeting_id'] in ids]

    @router.get('/api/departments/{department_id}/tasks/{task_id}')
    def detail(department_id: str, task_id: str):
        task = own_task(department_id, task_id)
        return {'task': task, 'events': repository.list_events(task_id=task_id),
                'meeting': meeting_metadata(repository.get_meeting(task['source_meeting_id']))}

    @router.get('/api/departments/{department_id}/tasks/{task_id}/history')
    def history(department_id: str, task_id: str):
        own_task(department_id, task_id)
        return repository.list_task_history(task_id)

    @router.patch('/api/departments/{department_id}/tasks/{task_id}')
    def process(department_id: str, task_id: str, payload: dict):
        department = workspace(department_id)['name']
        if set(payload) - {'status', 'progress_note', 'expected_version'}:
            raise HTTPException(status_code=400, detail='部门工作台只能填写进展和更新状态')
        version = payload.get('expected_version')
        if type(version) is not int or version < 1:
            raise HTTPException(status_code=400, detail='请刷新任务后提交有效版本')
        status = payload.get('status')
        if not isinstance(status, str) or status not in PENDING | {'completed'}:
            raise HTTPException(status_code=400, detail='任务状态无效')
        note = payload.get('progress_note', '')
        if not isinstance(note, str) or len(note) > 5000:
            raise HTTPException(status_code=400, detail='进展说明必须是文本，且不超过 5000 字')
        try:
            return repository.update_task_by_human(task_id, {'status': status},
                expected_department=department, expected_version=version, progress_note=note.strip())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail='本部门未找到该任务') from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    app.include_router(router)
