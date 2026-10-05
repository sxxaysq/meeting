"""Organization directory transcribed from the two user-provided address-book images."""
import hashlib
import json
import uuid


COMPANY = '中煤科工集团信息技术有限公司'
USER_ADDED_UNITS = ('公司级', '工会', '团支部', '纪检专干')
OFFICIAL_UNITS = (
    '智能矿山事业部', '数字化技术服务事业部', '数维智能事业部',
    '北京分公司（信息网络部）', '智能制造部', '数字咨询规划研究院', '市场经营部',
    '人工智能研究院（科技发展中心）', '安全生产部', '资产财务部（业财数字中心）',
    '人力资源部', '党建宣传部', '综合办公室（董事会办公室）',
) + USER_ADDED_UNITS
SHORT_NAMES = {
    '北京分公司': '北京分公司（信息网络部）',
    '人工智能研究院': '人工智能研究院（科技发展中心）',
    '资产财务部': '资产财务部（业财数字中心）',
    '综合办公室': '综合办公室（董事会办公室）',
}


def organization_key(name):
    return hashlib.sha256(name.encode('utf-8')).hexdigest()[:20]


def initialize_organization(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS organization_units (
        organization_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        parent_id TEXT REFERENCES organization_units(organization_id),
        kind TEXT NOT NULL, sort_order INTEGER NOT NULL, source TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS organization_aliases (
        alias TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organization_units(organization_id))''')
    root = organization_key(COMPANY)
    connection.execute('INSERT OR IGNORE INTO organization_units VALUES (?,?,NULL,?,?,?)',
                       (root, COMPANY, 'company', 0, '用户提供企业通讯录截图'))
    for index, name in enumerate(OFFICIAL_UNITS, 1):
        connection.execute('''INSERT INTO organization_units VALUES (?,?,?,?,?,?)
            ON CONFLICT(organization_id) DO UPDATE SET kind=excluded.kind,sort_order=excluded.sort_order,source=excluded.source''',
            (organization_key(name), name, root, 'official', index,
             '用户明确指定为组织条目（2026-09-20）' if name in USER_ADDED_UNITS else '用户提供企业通讯录截图'))
        aliases = [name, *(alias for alias, canonical in SHORT_NAMES.items() if canonical == name)]
        if '（' in name:
            aliases.append(name.replace('（', '(').replace('）', ')'))
        for alias in aliases:
            connection.execute('INSERT OR IGNORE INTO organization_aliases VALUES (?,?)', (alias, organization_key(name)))
    columns = {row['name'] for row in connection.execute('PRAGMA table_info(tasks)')}
    if 'organization_id' not in columns:
        connection.execute('ALTER TABLE tasks ADD COLUMN organization_id TEXT REFERENCES organization_units(organization_id)')
    if 'source_department' not in columns:
        connection.execute('ALTER TABLE tasks ADD COLUMN source_department TEXT')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_tasks_organization ON tasks(organization_id,is_deleted,status)')
    # All existing writers (imports, manual edits, review executor) share this database boundary.
    for suffix, event in (('insert', 'INSERT'), ('update', 'UPDATE OF department')):
        connection.execute(f'''CREATE TRIGGER IF NOT EXISTS tasks_organization_{suffix}
            AFTER {event} ON tasks
            WHEN NEW.organization_id IS NOT (SELECT organization_id FROM organization_aliases WHERE alias=TRIM(NEW.department))
              OR NEW.department IS NOT COALESCE((SELECT u.name FROM organization_units u JOIN organization_aliases a
                    ON a.organization_id=u.organization_id WHERE a.alias=TRIM(NEW.department)),NEW.department)
              OR (NEW.source_department IS NULL AND NEW.department IS NOT NULL)
            BEGIN
              UPDATE tasks SET source_department=COALESCE(NEW.source_department,NEW.department),
                organization_id=(SELECT organization_id FROM organization_aliases WHERE alias=TRIM(NEW.department)),
                department=COALESCE((SELECT u.name FROM organization_units u JOIN organization_aliases a
                    ON a.organization_id=u.organization_id WHERE a.alias=TRIM(NEW.department)),NEW.department)
                WHERE task_id=NEW.task_id;
            END''')


def merge_existing_tasks(database_path):
    """Idempotent explicit backfill; preserve source names and existing audit snapshots."""
    from .database import connect, utc_now
    linked, renamed = 0, 0
    with connect(database_path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        aliases = {row['alias']: (row['organization_id'], row['name']) for row in connection.execute(
            'SELECT a.alias,a.organization_id,u.name FROM organization_aliases a JOIN organization_units u USING(organization_id)')}
        for row in connection.execute('SELECT * FROM tasks').fetchall():
            old = dict(row)
            match = aliases.get((old.get('department') or '').strip())
            if not match:
                continue
            org_id, name = match
            changed_name = old['department'] != name
            if not changed_name and old['organization_id'] == org_id and old['source_department'] is not None:
                continue
            now = utc_now()
            connection.execute('''UPDATE tasks SET department=?,organization_id=?,source_department=COALESCE(source_department,?),
                version=version+?,updated_at=CASE WHEN ? THEN ? ELSE updated_at END WHERE task_id=?''',
                (name, org_id, old['department'], int(changed_name), int(changed_name), now, old['task_id']))
            linked += 1
            if changed_name:
                after = dict(connection.execute('SELECT * FROM tasks WHERE task_id=?', (old['task_id'],)).fetchone())
                connection.execute('''INSERT INTO task_events(event_id,event_key,run_id,task_id,source_meeting_id,
                    db_action,execution_status,before_json,after_json,evidence_text,failure_reason,created_at)
                    VALUES (?,?, 'ORGANIZATION_MERGE',?,?,'ORG_MERGE','applied',?,?,?,NULL,?)''',
                    ('E'+uuid.uuid4().hex[:12].upper(), 'organization-merge:'+uuid.uuid4().hex,
                     old['task_id'], old['source_meeting_id'], json.dumps(old, ensure_ascii=False),
                     json.dumps(after, ensure_ascii=False), old['evidence_text'], now))
                renamed += 1
        connection.commit()
    return {'tasks_linked': linked, 'tasks_renamed': renamed}
