"""SQLite 结构、事务和审计事件辅助函数。"""
from __future__ import annotations
import json, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY,role TEXT NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires_at TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS segments(segment_id TEXT PRIMARY KEY,district TEXT NOT NULL,network_type TEXT NOT NULL,length_m REAL NOT NULL,criticality INTEGER NOT NULL,status TEXT NOT NULL,install_year INTEGER,material TEXT NOT NULL DEFAULT 'steel',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS readings(reading_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL REFERENCES segments(segment_id),sensor_id TEXT NOT NULL,pressure_kpa REAL NOT NULL,flow_lps REAL NOT NULL,acoustic_db REAL NOT NULL,observed_at TEXT NOT NULL,UNIQUE(segment_id,sensor_id,observed_at));
CREATE TABLE IF NOT EXISTS alerts(alert_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL REFERENCES segments(segment_id),fingerprint TEXT NOT NULL UNIQUE,severity TEXT NOT NULL,score REAL NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,resolved_at TEXT);
CREATE TABLE IF NOT EXISTS work_orders(work_order_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL,alert_id TEXT NOT NULL,assignee TEXT NOT NULL,status TEXT NOT NULL,priority INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resources(resource_id TEXT PRIMARY KEY,kind TEXT NOT NULL,district TEXT NOT NULL,capacity INTEGER NOT NULL,available INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(allocation_id TEXT PRIMARY KEY,resource_id TEXT NOT NULL,work_order_id TEXT NOT NULL,quantity INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(resource_id,work_order_id));
CREATE TABLE IF NOT EXISTS audit_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health_rules(rule_id TEXT NOT NULL,version INTEGER NOT NULL,effective_at TEXT NOT NULL,config TEXT NOT NULL,fingerprint TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_by TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(rule_id,version));
CREATE TABLE IF NOT EXISTS health_reports(report_id TEXT PRIMARY KEY,rule_id TEXT NOT NULL,rule_version INTEGER NOT NULL,rule_fingerprint TEXT NOT NULL,effective_at TEXT NOT NULL,config TEXT NOT NULL,input_fingerprint TEXT NOT NULL,input_snapshot TEXT NOT NULL,district TEXT,as_of TEXT NOT NULL,segment_count INTEGER NOT NULL,uncertain_count INTEGER NOT NULL,created_by TEXT NOT NULL,generated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health_report_segments(report_id TEXT NOT NULL,segment_id TEXT NOT NULL,score REAL NOT NULL,risk_band TEXT NOT NULL,uncertain INTEGER NOT NULL,confidence TEXT NOT NULL,reasons TEXT NOT NULL,contributions TEXT NOT NULL,reading_count INTEGER NOT NULL,repair_count INTEGER NOT NULL,reading_ids TEXT NOT NULL,work_order_ids TEXT NOT NULL,rank_position INTEGER,PRIMARY KEY(report_id,segment_id));
CREATE INDEX IF NOT EXISTS idx_hrs_report_band ON health_report_segments(report_id,risk_band);
CREATE INDEX IF NOT EXISTS idx_hrs_report_score ON health_report_segments(report_id,score);
"""
# 旧库补列（基线库可能缺少安装年份与材质字段）。
_MIGRATIONS = (
    "ALTER TABLE segments ADD COLUMN install_year INTEGER",
    "ALTER TABLE segments ADD COLUMN material TEXT NOT NULL DEFAULT 'steel'",
)
def utcnow() -> str: return datetime.now(timezone.utc).isoformat()
def connect(path: str = ":memory:") -> sqlite3.Connection:
    db=sqlite3.connect(path,timeout=10,check_same_thread=False); db.row_factory=sqlite3.Row; db.execute("PRAGMA foreign_keys=ON"); db.execute("PRAGMA journal_mode=WAL"); db.executescript(SCHEMA)
    for statement in _MIGRATIONS:
        try: db.execute(statement)
        except sqlite3.OperationalError: pass
    db.commit(); return db
@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try: db.execute("BEGIN IMMEDIATE"); yield db; db.commit()
    except Exception: db.rollback(); raise
def audit(db, entity_type, entity_id, action, actor, payload):
    db.execute("INSERT INTO audit_events(entity_type,entity_id,action,actor,payload,created_at) VALUES(?,?,?,?,?,?)",(entity_type,entity_id,action,actor,json.dumps(payload,ensure_ascii=False,sort_keys=True),utcnow()))
def rows(db, query, args=()): return [dict(r) for r in db.execute(query,args).fetchall()]
