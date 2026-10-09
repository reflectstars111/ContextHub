"""Small durable hub store. Public Git snapshots contain approved package data only."""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .package import read_package


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs = self.root / 'packages'
        self.blobs.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS packages (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    title TEXT NOT NULL, description TEXT NOT NULL,
                    sha256 TEXT NOT NULL, visibility TEXT NOT NULL,
                    conversation_count INTEGER NOT NULL, search_text TEXT NOT NULL,
                    created_at TEXT NOT NULL, git_commit TEXT,
                    deleted INTEGER NOT NULL DEFAULT 0);
            ''')

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.root / 'hub.sqlite3', timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 310000).hex()

    def register(self, username, password):
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_\-]{3,32}', username):
            raise ValueError('用户名需为 3–32 位字母、数字、下划线或连字符')
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise ValueError('密码需为 10–128 个字符')
        salt = secrets.token_hex(16)
        user = {'id': uuid.uuid4().hex, 'username': username.lower()}
        try:
            with self.db() as db:
                db.execute('INSERT INTO users VALUES (?,?,?,?)', (user['id'], user['username'], salt, self.password_hash(password, salt)))
        except sqlite3.IntegrityError as exc:
            raise ValueError('该用户名已被使用') from exc
        return self.new_session(user)

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 128:
            raise PermissionError('用户名或密码错误')
        with self.db() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (username.lower(),)).fetchone()
        salt = row['salt'] if row else '00' * 16
        digest = self.password_hash(password, salt)
        if row is None or not hmac.compare_digest(digest, row['password_hash']):
            raise PermissionError('用户名或密码错误')
        return self.new_session({'id': row['id'], 'username': row['username']})

    def new_session(self, user):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['id'], csrf, time.time() + 7 * 86400))
        return {'user': user, 'csrf_token': csrf}, token

    def session(self, token):
        if not token:
            return {'user': None, 'csrf_token': None}
        with self.db() as db:
            row = db.execute('SELECT u.id,u.username,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return {'user': {'id': row['id'], 'username': row['username']}, 'csrf_token': row['csrf']} if row else {'user': None, 'csrf_token': None}

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def _git_snapshot(self, ident, package):
        repo = self.root / 'public.git'
        if not repo.exists():
            subprocess.run(['git', 'init', '--bare', str(repo)], check=True, capture_output=True)
        def git(*args, data=None):
            env = dict(os.environ)
            env.update(GIT_AUTHOR_NAME='ContextHub', GIT_AUTHOR_EMAIL='contexthub@localhost', GIT_COMMITTER_NAME='ContextHub', GIT_COMMITTER_EMAIL='contexthub@localhost', GIT_CONFIG_NOSYSTEM='1')
            return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '--git-dir', str(repo), *args], input=data, capture_output=True, check=True, env=env).stdout.decode().strip()
        files = {'library.json': package['library'], 'manifest.json': package['manifest']}
        files.update({f'conversation-{i:04}.json': c for i, c in enumerate(package['conversations'])})
        tree = []
        for name, value in sorted(files.items()):
            oid = git('hash-object', '-w', '--stdin', data=json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
            tree.append(f'100644 blob {oid}\t{name}\n')
        tree_id = git('mktree', data=''.join(tree).encode())
        commit = git('commit-tree', tree_id, data=f'Publish conversation package {ident}\n'.encode())
        git('update-ref', f'refs/heads/packages/{ident}', commit)
        return commit

    def add_package(self, owner, data, visibility='private'):
        if visibility not in ('private', 'public'):
            raise ValueError('visibility 必须为 private 或 public')
        package = read_package(data)
        digest = hashlib.sha256(data).hexdigest()
        with self.lock:
            with self.db() as db:
                duplicate = db.execute('SELECT id FROM packages WHERE owner_id=? AND sha256=? AND visibility=? AND deleted=0', (owner['id'], digest, visibility)).fetchone()
            if duplicate:
                return self.detail(duplicate['id'], owner['id'])
            ident = uuid.uuid4().hex
            path = self.blobs / (digest + '.contextpack')
            if not path.exists():
                temp = self.blobs / (uuid.uuid4().hex + '.tmp')
                with temp.open('xb') as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                temp.replace(path)
            commit = self._git_snapshot(ident, package) if visibility == 'public' else None
            library = package['library']
            title = str(library.get('title', '未命名对话库'))[:200]
            description = str(library.get('description', ''))[:4000]
            search = ' '.join([title, description] + [c.get('title', '') + ' ' + ' '.join(m['content'] for m in c['messages']) for c in package['conversations']])
            with self.db() as db:
                db.execute('INSERT INTO packages (id,owner_id,title,description,sha256,visibility,conversation_count,search_text,created_at,git_commit) VALUES (?,?,?,?,?,?,?,?,?,?)', (ident, owner['id'], title, description, digest, visibility, len(package['conversations']), search, datetime.now(timezone.utc).isoformat(), commit))
        return self.detail(ident, owner['id'])

    @staticmethod
    def public_row(row):
        return {key: row[key] for key in ('id', 'owner_id', 'title', 'description', 'sha256', 'visibility', 'conversation_count', 'created_at', 'git_commit', 'author')}

    def list_packages(self, user_id=None, query=''):
        with self.db() as db:
            rows = db.execute("SELECT p.*,u.username AS author FROM packages p JOIN users u ON u.id=p.owner_id WHERE p.deleted=0 AND (p.visibility='public' OR p.owner_id=?) AND instr(lower(p.search_text),lower(?))>0 ORDER BY p.created_at DESC LIMIT 200", (user_id, query[:200])).fetchall()
        return [self.public_row(r) for r in rows]

    def get(self, ident, user_id=None):
        with self.db() as db:
            row = db.execute("SELECT p.*,u.username AS author FROM packages p JOIN users u ON u.id=p.owner_id WHERE p.id=? AND p.deleted=0 AND (p.visibility='public' OR p.owner_id=?)", (ident, user_id)).fetchone()
        if row is None:
            raise FileNotFoundError('对话库不存在或无权访问')
        return self.public_row(row)

    def download(self, ident, user_id=None):
        row = self.get(ident, user_id)
        return (self.blobs / (row['sha256'] + '.contextpack')).read_bytes()

    def detail(self, ident, user_id=None):
        row = self.get(ident, user_id)
        package = read_package(self.download(ident, user_id))
        return {**row, 'conversations': package['conversations'], 'compatibility': package['compatibility']}

    def delete(self, ident, user_id):
        with self.db() as db:
            changed = db.execute('UPDATE packages SET deleted=1 WHERE id=? AND owner_id=? AND deleted=0', (ident, user_id)).rowcount
            if not changed:
                raise FileNotFoundError('对话库不存在或无权操作')
