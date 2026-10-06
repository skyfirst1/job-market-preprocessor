import csv
import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical_url(url):
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Only absolute HTTP(S) source URLs are supported')
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    # SPA fragments and WeChat signature parameters are part of source identity.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/',
                       urlencode(query), parsed.fragment))


TERMS = ('agent', 'llm', 'aigc', 'ai infra', '人工智能', '算法', '大模型', '多模态',
         '计算机视觉', '机器视觉', '视觉', '图像', '机器学习', '深度学习', '强化学习',
         '感知', 'slam', '具身智能', '自动驾驶')


def coarse_tags(row):
    text = ' '.join(str(row.get(k, '')) for k in ('公司名称', '行业分类', '招聘岗位', '专业要求')).lower()
    tags = [term for term in TERMS if term in text]
    if re.search(r'(?<![a-z])ai(?![a-z])', text):
        tags.append('ai')
    return tags


class Store:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'workstation.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                  id TEXT PRIMARY KEY, input_path TEXT NOT NULL, row_number INTEGER NOT NULL,
                  company TEXT, raw_json TEXT NOT NULL, tags_json TEXT NOT NULL, priority INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
                  priority INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                  attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                  error TEXT, result_json TEXT, parent_id TEXT);
                CREATE TABLE IF NOT EXISTS source_tasks (
                  source_id TEXT NOT NULL REFERENCES sources(id), task_id TEXT NOT NULL REFERENCES tasks(id),
                  field TEXT NOT NULL, PRIMARY KEY(source_id,task_id,field));
                CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,priority DESC);
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, task_id TEXT, kind TEXT, detail TEXT);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
            for name, definition in (('available_at', 'TEXT'), ('last_error_kind', 'TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')
            db.execute('CREATE INDEX IF NOT EXISTS task_retry_queue ON tasks(status,available_at)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add_task(self, url, priority=0, kind='page', source_id=None, field='', parent_id=None):
        url = canonical_url(url)
        task_id = hashlib.sha256(url.encode()).hexdigest()[:24]
        with self.connect() as db:
            db.execute('''INSERT INTO tasks(id,url,kind,priority,updated_at,parent_id) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(id) DO UPDATE SET priority=MAX(tasks.priority,excluded.priority)''',
                       (task_id, url, kind, priority, now(), parent_id))
            if source_id:
                db.execute('INSERT OR IGNORE INTO source_tasks VALUES(?,?,?)', (source_id, task_id, field))
            if parent_id:
                db.execute('''INSERT OR IGNORE INTO source_tasks SELECT source_id,?, 'discovered-detail'
                              FROM source_tasks WHERE task_id=?''', (task_id, parent_id))
        return task_id

    def import_csv(self, path):
        path = Path(path).resolve()
        count = 0
        with self.connect() as db, path.open(encoding='utf-8-sig', newline='') as handle:
            for number, row in enumerate(csv.DictReader(handle), start=2):
                if not row.get('更新时间') or not row.get('公司名称'):
                    continue
                source_id = hashlib.sha256(f'{path}:{number}'.encode()).hexdigest()[:24]
                tags = coarse_tags(row)
                priority = len(tags) * 10
                db.execute('''INSERT INTO sources VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                              company=excluded.company,raw_json=excluded.raw_json,
                              tags_json=excluded.tags_json,priority=excluded.priority''',
                           (source_id, str(path), number, row.get('公司名称'),
                            json.dumps(row, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), priority))
                for field in ('公告链接', '投递链接'):
                    for url in re.findall(r'https?://[^\s<>"\u3000]+', row.get(field, '')):
                        try:
                            url = canonical_url(url.rstrip('，。；'))
                            task_id = hashlib.sha256(url.encode()).hexdigest()[:24]
                            db.execute('''INSERT INTO tasks(id,url,kind,priority,updated_at) VALUES(?,?,?,?,?)
                                          ON CONFLICT(id) DO UPDATE SET priority=MAX(tasks.priority,excluded.priority)''',
                                       (task_id, url, 'page', priority, now()))
                            db.execute('INSERT OR IGNORE INTO source_tasks VALUES(?,?,?)', (source_id, task_id, field))
                        except ValueError:
                            db.execute('INSERT INTO events(timestamp,kind,detail) VALUES(?,?,?)',
                                       (now(), 'invalid_url', f'row={number} field={field}'))
                count += 1
        return {'imported_rows': count, **self.stats()}

    def claim(self, ai_only=False, task_ids=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            where = "(status='pending' OR (status='retry_wait' AND available_at<=?))"
            args = [now()]
            if ai_only:
                where += ' AND priority>0'
            if task_ids is not None:
                if not task_ids:
                    return None
                where += ' AND id IN (' + ','.join('?' for _ in task_ids) + ')'
                args.extend(task_ids)
            row = db.execute(f'SELECT * FROM tasks WHERE {where} ORDER BY priority DESC,id LIMIT 1', args).fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET status='running',attempts=attempts+1,updated_at=?,available_at=NULL WHERE id=?",
                       (now(), row['id']))
            task = dict(row)
            task.update(status='running', attempts=task['attempts'] + 1, available_at=None)
            return task

    def finish(self, task_id, status, result=None, error=None):
        with self.connect() as db:
            db.execute('UPDATE tasks SET status=?,result_json=?,error=?,updated_at=?,available_at=NULL,last_error_kind=NULL WHERE id=?',
                       (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                        error, now(), task_id))

    def schedule_retry(self, task_id, delay_seconds, error_kind, result=None, error=None):
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float)) or not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError('Retry delay cannot be negative')
        available_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        with self.connect() as db:
            db.execute('''UPDATE tasks SET status='retry_wait',available_at=?,last_error_kind=?,
                          result_json=?,error=?,updated_at=? WHERE id=?''',
                       (available_at, error_kind,
                        json.dumps(result, ensure_ascii=False) if result is not None else None,
                        error, now(), task_id))
        return available_at

    def next_retry_delay(self, ai_only=False, task_ids=None):
        with self.connect() as db:
            where, args = "status='retry_wait'", []
            if ai_only:
                where += ' AND priority>0'
            if task_ids is not None:
                if not task_ids:
                    return None
                where += ' AND id IN (' + ','.join('?' for _ in task_ids) + ')'
                args.extend(task_ids)
            row = db.execute(f'SELECT MIN(available_at) due FROM tasks WHERE {where}', args).fetchone()
        return max(0, (datetime.fromisoformat(row['due']) - datetime.now(timezone.utc)).total_seconds()) if row['due'] else None

    def recover_running(self):
        with self.connect() as db:
            cursor = db.execute("UPDATE tasks SET status='pending',updated_at=? WHERE status='running'", (now(),))
            return cursor.rowcount

    def retry(self, statuses, task_ids=None, reset_attempts=False):
        if not statuses:
            return 0
        with self.connect() as db:
            reset = ',attempts=0' if reset_attempts else ''
            query = "UPDATE tasks SET status='pending',available_at=NULL,last_error_kind=NULL,updated_at=?" + reset + ' WHERE status IN (' + ','.join('?' for _ in statuses) + ')'
            args = [now(), *statuses]
            if task_ids is not None:
                if not task_ids:
                    return 0
                query += ' AND id IN (' + ','.join('?' for _ in task_ids) + ')'
                args.extend(task_ids)
            return db.execute(query, args).rowcount

    def event(self, task_id, kind, detail):
        with self.connect() as db:
            db.execute('INSERT INTO events(timestamp,task_id,kind,detail) VALUES(?,?,?,?)',
                       (now(), task_id, kind, str(detail)[:2000]))

    def stats(self):
        with self.connect() as db:
            statuses = {row['status']: row['n'] for row in db.execute('SELECT status,COUNT(*) n FROM tasks GROUP BY status')}
            return {'sources': db.execute('SELECT COUNT(*) FROM sources').fetchone()[0],
                    'tasks': sum(statuses.values()), 'statuses': statuses}

    def tasks(self, limit=100, status=None):
        with self.connect() as db:
            where = 'WHERE status=?' if status else ''
            args = [status, limit] if status else [limit]
            rows = db.execute(f'SELECT * FROM tasks {where} ORDER BY priority DESC,id LIMIT ?', args)
            return [dict(row) for row in rows]

    def task(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            return dict(row) if row else None

    def source_references(self, task_id):
        with self.connect() as db:
            rows = db.execute('''SELECT s.*,st.field FROM sources s JOIN source_tasks st ON s.id=st.source_id
                                 WHERE st.task_id=? ORDER BY s.row_number''', (task_id,))
            return [{'source_id': row['id'], 'row_number': row['row_number'], 'field': row['field'],
                     'raw': json.loads(row['raw_json']), 'coarse_tags': json.loads(row['tags_json'])}
                    for row in rows]


@contextmanager
def worker_lock(data_dir):
    path = Path(data_dir) / 'worker.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    try:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if __import__('os').name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close()
        raise RuntimeError('Another workstation worker is running') from None
    try:
        yield
    finally:
        handle.seek(0)
        if __import__('os').name == 'nt':
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
