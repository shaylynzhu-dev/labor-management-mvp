PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    gender TEXT NOT NULL,
    company_name TEXT NULL,
    introducer TEXT NULL,
    id_last4 TEXT NULL,
    hk_macao_last4 TEXT NULL,
    person_name TEXT NULL,
    worker_type TEXT NOT NULL DEFAULT 'new' CHECK(worker_type IN ('new','renewal')),
    birth_date DATE NULL,
    birth_year_month TEXT NULL,
    mainland_id_first4 TEXT NULL,
    mainland_id_last4 TEXT NULL,
    hkmo_permit_first4 TEXT NULL,
    hkmo_permit_last6 TEXT NULL,
    entry_permit_no TEXT NULL,
    hk_submission_date DATE NULL,
    visa_status_date DATE NULL,
    visa_status TEXT NULL,
    hk_id_appointment_status TEXT NULL,
    remarks TEXT NULL,
    data_source TEXT NOT NULL DEFAULT 'manual_input',
    data_precedence_rank INTEGER NOT NULL DEFAULT 1,
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)),
    deleted_at DATETIME NULL,
    deleted_by INTEGER NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS person_documents (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL,
    document_type TEXT NOT NULL DEFAULT 'other',
    original_filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    mime_type TEXT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    upload_batch_id TEXT NULL,
    person_case_id INTEGER NULL,
    inferred_case_confidence REAL NULL,
    case_binding_status TEXT NOT NULL DEFAULT 'unassigned',
    document_hash TEXT NULL,
    duplicate_of_document_id INTEGER NULL,
    version_no INTEGER NOT NULL DEFAULT 1,
    binding_source TEXT NOT NULL DEFAULT 'auto_inference',
    data_source TEXT NOT NULL DEFAULT 'auto_inference',
    data_precedence_rank INTEGER NOT NULL DEFAULT 6,
    ocr_text TEXT NOT NULL DEFAULT '',
    issue_date DATE NULL,
    expiry_date DATE NULL,
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'active',
    remarks TEXT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at DATETIME NULL,
    deleted_by INTEGER NULL,
    FOREIGN KEY(person_id) REFERENCES person(id) ON DELETE RESTRICT,
    FOREIGN KEY(person_case_id) REFERENCES person_cases(id) ON DELETE SET NULL,
    FOREIGN KEY(duplicate_of_document_id) REFERENCES person_documents(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS person_cases (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL,
    case_type TEXT NOT NULL DEFAULT 'other',
    case_label TEXT NOT NULL,
    start_date DATE NULL,
    end_date DATE NULL,
    contract_start_date DATE NULL,
    contract_end_date DATE NULL,
    contract_restart_due_date DATE NULL,
    endorsement_expiry_date DATE NULL,
    document_collection_due_date DATE NULL,
    renewal_alert_status TEXT NOT NULL DEFAULT 'pending',
    quota_id INTEGER NULL,
    contract_id INTEGER NULL,
    status TEXT NOT NULL DEFAULT 'active',
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    data_source TEXT NOT NULL DEFAULT 'manual_input',
    data_precedence_rank INTEGER NOT NULL DEFAULT 1,
    remarks TEXT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at DATETIME NULL,
    deleted_by INTEGER NULL,
    FOREIGN KEY(person_id) REFERENCES person(id) ON DELETE RESTRICT,
    FOREIGN KEY(quota_id) REFERENCES quota(id) ON DELETE SET NULL,
    FOREIGN KEY(contract_id) REFERENCES contract(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS conflict_queue (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NULL,
    conflict_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'human_review_required',
    source TEXT NOT NULL DEFAULT 'auto_inference',
    data_precedence_rank INTEGER NOT NULL DEFAULT 6,
    payload TEXT NOT NULL DEFAULT '{}',
    resolution TEXT NULL,
    resolved_by INTEGER NULL,
    resolved_at DATETIME NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS retry_queue (
    id INTEGER PRIMARY KEY,
    operation TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NULL,
    filename TEXT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_retry_at DATETIME NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS data_precedence_rules (
    source TEXT PRIMARY KEY,
    rank INTEGER NOT NULL UNIQUE,
    label TEXT NOT NULL
);

INSERT OR IGNORE INTO data_precedence_rules(source,rank,label) VALUES
('manual_input', 1, '手动输入'),
('confirmed_binding', 2, '已确认绑定'),
('folder_recognition', 3, '文件夹识别'),
('filename_recognition', 4, '文件名识别'),
('excel_import', 5, 'Excel导入'),
('auto_inference', 6, '自动推断');

CREATE TABLE IF NOT EXISTS quota (
    id INTEGER PRIMARY KEY,
    quota_type TEXT NOT NULL CHECK(quota_type IN ('SWD', 'LD')),
    company_name TEXT NOT NULL,
    approval_no TEXT NULL,
    quota_no TEXT NULL,
    user_id INTEGER NULL,
    start_date DATE NULL,
    end_date DATE NULL,
    usage_count INTEGER NOT NULL DEFAULT 0 CHECK(usage_count >= 0),
    replacement_count INTEGER NOT NULL DEFAULT 0 CHECK(replacement_count >= 0),
    max_replacement_count INTEGER NOT NULL DEFAULT 1
        CHECK(max_replacement_count IN (1, 2)),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','in_use','exhausted','invalid')),
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)),
    deleted_at DATETIME NULL,
    deleted_by INTEGER NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES person(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS contract (
    id INTEGER PRIMARY KEY,
    contract_no TEXT NULL,
    company_name TEXT NOT NULL,
    person_id INTEGER NULL,
    quota_id INTEGER NULL,
    entry_date DATE NULL,
    arrival_date DATE NULL,
    contract_start_date DATE NULL,
    contract_end_date DATE NULL,
    start_date DATE NULL,
    end_date DATE NULL,
    cycle_index INTEGER NOT NULL DEFAULT 1 CHECK(cycle_index >= 1),
    parent_contract_id INTEGER NULL,
    is_replaced INTEGER NOT NULL DEFAULT 0 CHECK(is_replaced IN (0, 1)),
    status TEXT NOT NULL DEFAULT '制作合同'
        CHECK(status IN ('制作合同','交接香港同事','交表香港入境处',
                         '批出入境签证','工人入境','完成合约')),
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)),
    deleted_at DATETIME NULL,
    deleted_by INTEGER NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CHECK (entry_date IS NULL OR date(contract_start_date) = date(entry_date)),
    CHECK (arrival_date IS NULL OR date(arrival_date) = date(entry_date)),
    CHECK (contract_start_date IS NULL OR date(start_date) = date(contract_start_date)),
    CHECK (contract_end_date IS NULL OR date(end_date) = date(contract_end_date)),
    FOREIGN KEY (person_id) REFERENCES person(id) ON DELETE SET NULL,
    FOREIGN KEY (quota_id) REFERENCES quota(id) ON DELETE SET NULL,
    FOREIGN KEY (parent_contract_id) REFERENCES contract(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY,
    event_type TEXT,
    person_id INTEGER,
    quota_id INTEGER,
    contract_id INTEGER,
    description TEXT,
    severity TEXT DEFAULT 'normal',
    event_date DATE NOT NULL DEFAULT CURRENT_DATE,
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)),
    deleted_at DATETIME NULL,
    deleted_by INTEGER NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id) ON DELETE SET NULL,
    FOREIGN KEY (quota_id) REFERENCES quota(id) ON DELETE SET NULL,
    FOREIGN KEY (contract_id) REFERENCES contract(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS risk (
    id INTEGER PRIMARY KEY,
    person_id INTEGER,
    quota_id INTEGER,
    contract_id INTEGER,
    person_case_id INTEGER,
    due_date DATE NULL,
    risk_type TEXT,
    status TEXT DEFAULT 'open',
    description TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)),
    deleted_at DATETIME NULL,
    deleted_by INTEGER NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id) ON DELETE SET NULL,
    FOREIGN KEY (quota_id) REFERENCES quota(id) ON DELETE SET NULL,
    FOREIGN KEY (contract_id) REFERENCES contract(id) ON DELETE SET NULL,
    FOREIGN KEY (person_case_id) REFERENCES person_cases(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_contract_contract_no
    ON contract(contract_no) WHERE contract_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_person ON event(person_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_quota ON event(quota_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_contract ON event(contract_id, created_at);
CREATE INDEX IF NOT EXISTS idx_risk_status ON risk(status, risk_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_quota_expiry_open
    ON risk(quota_id, risk_type) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_person_documents_hash
    ON person_documents(document_hash, original_filename, person_id, person_case_id);
CREATE INDEX IF NOT EXISTS idx_conflict_queue_status
    ON conflict_queue(status, conflict_type, created_at);
CREATE INDEX IF NOT EXISTS idx_retry_queue_status
    ON retry_queue(status, entity_type, created_at);
