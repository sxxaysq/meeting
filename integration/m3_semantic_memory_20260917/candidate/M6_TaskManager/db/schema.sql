PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS departments (
    department_id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    route TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    item_type TEXT NOT NULL CHECK (
        item_type IN ('PROJECT_TASK', 'RESEARCH_TASK', 'NON_PROJECT_WORK')
    ),
    project_entity_id TEXT,
    project TEXT,
    source_project TEXT,
    department_id TEXT,
    department TEXT,
    work_section TEXT,
    delivery_group TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    assignees_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (
        status IN (
            'OPEN', 'IN_PROGRESS', 'BLOCKED',
            'COMPLETED', 'CANCELLED', 'CLOSED'
        )
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (department_id) REFERENCES departments(department_id)
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'CREATE', 'PROGRESS_UPDATE', 'MODIFY', 'COMPLETE',
            'CANCEL', 'REOPEN', 'TRANSFER'
        )
    ),
    content TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS task_source_links (
    link_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- MULTI: one item may fan out to several tasks; uniqueness is per (item, task).
    UNIQUE (source_document_id, source_item_id, task_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
    FOREIGN KEY (event_id) REFERENCES task_events(event_id)
);

CREATE TABLE IF NOT EXISTS task_audit (
    audit_id TEXT PRIMARY KEY,
    task_id TEXT,
    action TEXT NOT NULL,
    before_state_json TEXT,
    after_state_json TEXT,
    reason TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS dispatch_queue (
    dispatch_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    department_id TEXT NOT NULL,
    department TEXT NOT NULL,
    route TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        status IN ('PENDING', 'SENT', 'FAILED', 'CANCELLED')
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
    FOREIGN KEY (department_id) REFERENCES departments(department_id),
    FOREIGN KEY (source_event_id) REFERENCES task_events(event_id)
);

CREATE TABLE IF NOT EXISTS lifecycle_reviews (
    review_id TEXT PRIMARY KEY,
    source_document_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    item_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        status IN ('PENDING', 'APPROVED', 'REJECTED')
    ),
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE (source_document_id, source_item_id)
);

CREATE TABLE IF NOT EXISTS processing_records (
    processing_id TEXT PRIMARY KEY,
    source_document_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    action TEXT NOT NULL,
    task_id TEXT,
    event_id TEXT,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_document_id, source_item_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
    FOREIGN KEY (event_id) REFERENCES task_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_project
    ON tasks(project_entity_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_department
    ON tasks(department, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_events_task
    ON task_events(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatch_status
    ON dispatch_queue(status, created_at);
