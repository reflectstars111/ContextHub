"""Small durable hub store. Public Git snapshots contain approved package data only."""

import errno
import hashlib
import hmac
import json
import logging
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
from .platforms import PLATFORMS, normalize_platform, package_platforms
from .storage_budget import BudgetConnection, StorageBudget, StoreCapacityError
from .library_state import LibraryState


class StoreConflict(ValueError):
    """The library changed since the caller loaded its expected base."""


class Store(LibraryState):
    def __init__(self, root, max_storage_bytes=None, min_free_bytes=0):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs = self.root / 'packages'
        self.blobs.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.budget = StorageBudget(self.root, max_storage_bytes, min_free_bytes)
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
        self._migrate_versions()
        self._migrate_platforms()
        self._migrate_library_state()
        self._repair_refs()

    def _migrate_versions(self):
        """Upgrade original immutable uploads in place, once, under a DB lock."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(packages)')}
            for column in ('head_version_id', 'upstream_package_id', 'upstream_version_id'):
                if column not in columns:
                    db.execute(f'ALTER TABLE packages ADD COLUMN {column} TEXT')
            db.execute('''CREATE TABLE IF NOT EXISTS package_versions (
                id TEXT PRIMARY KEY, package_id TEXT NOT NULL, number INTEGER NOT NULL,
                parent_id TEXT, sha256 TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL, conversation_count INTEGER NOT NULL,
                search_text TEXT NOT NULL, message TEXT NOT NULL,
                author_id TEXT NOT NULL, created_at TEXT NOT NULL, git_commit TEXT,
                UNIQUE(package_id, number))''')
            db.execute('CREATE INDEX IF NOT EXISTS versions_package ON package_versions(package_id, number DESC)')
            for row in db.execute('SELECT * FROM packages WHERE head_version_id IS NULL').fetchall():
                version_id = uuid.uuid4().hex
                db.execute('''INSERT INTO package_versions
                    (id,package_id,number,parent_id,sha256,title,description,conversation_count,
                     search_text,message,author_id,created_at,git_commit)
                    VALUES (?,?,1,NULL,?,?,?,?,?,?,?,?,?)''',
                    (version_id, row['id'], row['sha256'], row['title'], row['description'],
                     row['conversation_count'], row['search_text'], '初始版本', row['owner_id'],
                     row['created_at'], row['git_commit']))
                db.execute('UPDATE packages SET head_version_id=? WHERE id=?', (version_id, row['id']))

    def _migrate_platforms(self):
        """Backfill only missing head classifications, atomically and once."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS package_platforms (
                package_id TEXT NOT NULL, platform TEXT NOT NULL
                CHECK(platform IN ('codex','chatgpt','claude','deepseek','other')),
                PRIMARY KEY(package_id, platform))''')
            db.execute('CREATE INDEX IF NOT EXISTS platforms_lookup ON package_platforms(platform, package_id)')
            rows = db.execute('''SELECT p.id,p.sha256 FROM packages p WHERE p.deleted=0
                AND NOT EXISTS (SELECT 1 FROM package_platforms c WHERE c.package_id=p.id)''').fetchall()
            for row in rows:
                package = read_package((self.blobs / (row['sha256'] + '.contextpack')).read_bytes())
                self._index_platforms(db, row['id'], package)

    @staticmethod
    def _index_platforms(db, ident, package):
        # Callers hold the same transaction that changes the library head.
        db.execute('DELETE FROM package_platforms WHERE package_id=?', (ident,))
        db.executemany('INSERT INTO package_platforms (package_id,platform) VALUES (?,?)',
                       ((ident, platform) for platform in package_platforms(package)))

    @staticmethod
    def _indexed_platforms(db, identifiers):
        if not identifiers:
            return {}
        placeholders = ','.join('?' for _ in identifiers)
        rows = db.execute(f'SELECT package_id,platform FROM package_platforms WHERE package_id IN ({placeholders})',
                          identifiers).fetchall()
        values = {ident: set() for ident in identifiers}
        for row in rows:
            values[row['package_id']].add(row['platform'])
        return {ident: [platform for platform in PLATFORMS if platform in platforms] or ['other']
                for ident, platforms in values.items()}

    @contextmanager
    def db(self):
        with self.budget.hold():
            factory = BudgetConnection if self.budget.enabled else sqlite3.Connection
            conn = sqlite3.connect(self.root / 'hub.sqlite3', timeout=15, factory=factory)
            if self.budget.enabled:
                conn.budget = self.budget
                # Bound dirty pages not yet visible to directory accounting.
                sqlite3.Connection.execute(conn, 'PRAGMA cache_size=-256')
            conn.row_factory = sqlite3.Row
            # Purged message/search/draft text must not remain in freed pages.
            sqlite3.Connection.execute(conn, 'PRAGMA secure_delete=ON')
            try:
                yield conn
                conn.commit()
            except Exception as exc:
                conn.rollback()
                if ((isinstance(exc, sqlite3.Error) and getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL)
                        or (isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT))):
                    raise StoreCapacityError('服务器磁盘空间不足，保存失败；请联系管理员') from exc
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
                return self._new_session(db, user)
        except sqlite3.IntegrityError as exc:
            raise ValueError('该用户名已被使用') from exc

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
        with self.db() as db:
            return self._new_session(db, user)

    @staticmethod
    def _new_session(db, user):
        # Registration and its first session share one transaction so any
        # capacity or database failure leaves the username available to retry.
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
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

    def _git(self, *args, data=None):
        repo = self.root / 'public.git'
        env = dict(os.environ)
        env.update(GIT_AUTHOR_NAME='ContextHub', GIT_AUTHOR_EMAIL='contexthub@localhost',
                   GIT_COMMITTER_NAME='ContextHub', GIT_COMMITTER_EMAIL='contexthub@localhost',
                   GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, LC_ALL='C')
        with self.budget.hold():
            if args and args[0] not in ('show', 'rev-parse', 'cat-file', 'log', 'for-each-ref', 'rev-list', 'fsck'):
                self.budget.ensure(2 * len(data or b'') + 1024 * 1024)
            try:
                return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null',
                                       '-c', 'gc.auto=0', '-c', 'maintenance.auto=false',
                                       '--git-dir', str(repo), *args],
                                      input=data, capture_output=True, check=True, env=env).stdout.decode().strip()
            except subprocess.CalledProcessError as exc:
                if b'No space left on device' in (exc.stderr or b'') or b'Disk quota exceeded' in (exc.stderr or b''):
                    raise StoreCapacityError('服务器磁盘空间不足，Git 保存失败；请联系管理员') from exc
                raise

    def _git_snapshot(self, ident, package, parent=None, message='初始版本', second_parent=None):
        """Write immutable objects only; refs follow committed database state."""
        repo = self.root / 'public.git'
        if not repo.exists():
            with self.budget.hold():
                self.budget.ensure(1024 * 1024)
                subprocess.run(['git', 'init', '--bare', '--template=', str(repo)], check=True, capture_output=True)
        files = {'library.json': package['library'], 'manifest.json': package['manifest']}
        files.update({f'conversation-{i:04}.json': c for i, c in enumerate(package['conversations'])})
        tree = []
        for name, value in sorted(files.items()):
            oid = self._git('hash-object', '-w', '--stdin', data=json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
            tree.append(f'100644 blob {oid}\t{name}\n')
        tree_id = self._git('mktree', data=''.join(tree).encode())
        parents = ['-p', parent] if parent else []
        if second_parent:
            if not parent:
                raise ValueError('A merge snapshot requires both parent commits')
            parents.extend(['-p', second_parent])
        return self._git('commit-tree', tree_id, *parents, data=f'{message}\n\nContextHub library {ident}\n'.encode())

    def _sync_git_ref(self, ident):
        # Re-read while holding SQLite's cross-process writer lock. A delayed
        # ref update must never replace a newer committed head with an older one.
        try:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT git_commit FROM packages WHERE id=? AND visibility='public' AND deleted=0", (ident,)).fetchone()
                if row and row['git_commit']:
                    self._git('update-ref', f'refs/heads/packages/{ident}', row['git_commit'])
                elif (self.root / 'public.git').exists():
                    self._remove_git_ref(ident)
        except (OSError, subprocess.CalledProcessError, StoreCapacityError):
            logging.getLogger(__name__).warning('Public Git ref refresh deferred until next startup')

    def _repair_refs(self):
        with self.db() as db:
            identifiers = {row['id'] for row in db.execute("SELECT id FROM packages WHERE visibility='public' AND deleted=0 AND git_commit IS NOT NULL")}
        if (self.root / 'public.git').exists():
            identifiers.update(ref.removeprefix('refs/heads/packages/') for ref in
                               self._git('for-each-ref', '--format=%(refname)', 'refs/heads/packages/').splitlines())
        for ident in identifiers:
            self._sync_git_ref(ident)

    def _save_blob(self, data):
        digest = hashlib.sha256(data).hexdigest()
        path = self.blobs / (digest + '.contextpack')
        with self.budget.hold():
            if not path.exists():
                self.budget.ensure(len(data) + 8192)
                temp = self.blobs / (uuid.uuid4().hex + '.tmp')
                try:
                    with temp.open('xb') as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                    temp.replace(path)
                except OSError as exc:
                    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                        raise StoreCapacityError('服务器磁盘空间不足，对话包保存失败；请联系管理员') from exc
                    raise
                finally:
                    temp.unlink(missing_ok=True)
        return digest

    @staticmethod
    def _fields(package):
        library = package['library']
        title = library['title'][:200]
        description = library['description'][:4000]
        search = ' '.join([title, description] + [c['title'] + ' ' + ' '.join(m['content'] for m in c['messages']) for c in package['conversations']])
        return title, description, len(package['conversations']), search

    def _create_package(self, db, owner, data, package, visibility, upstream=None, message=None):
        ident, version_id = uuid.uuid4().hex, uuid.uuid4().hex
        digest = self._save_blob(data)
        title, description, count, search = self._fields(package)
        now = datetime.now(timezone.utc).isoformat()
        message = message or ('Fork 初始版本' if upstream else '初始版本')
        parent = upstream['git_commit'] if upstream and visibility == 'public' else None
        commit = self._git_snapshot(ident, package, parent, message) if visibility == 'public' else None
        db.execute('''INSERT INTO packages
            (id,owner_id,title,description,sha256,visibility,conversation_count,search_text,created_at,
             git_commit,head_version_id,upstream_package_id,upstream_version_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (ident, owner['id'], title, description, digest, visibility, count, search, now, commit,
             version_id, upstream['package_id'] if upstream else None, upstream['id'] if upstream else None))
        db.execute('''INSERT INTO package_versions
            (id,package_id,number,parent_id,sha256,title,description,conversation_count,search_text,
             message,author_id,created_at,git_commit) VALUES (?,?,1,NULL,?,?,?,?,?,?,?,?,?)''',
            (version_id, ident, digest, title, description, count, search, message, owner['id'], now, commit))
        self._index_platforms(db, ident, package)
        return ident, version_id

    def add_package(self, owner, data, visibility='private', message=None, new_library_key=None):
        if visibility not in ('private', 'public'):
            raise ValueError('visibility 必须为 private 或 public')
        if new_library_key is not None and (not isinstance(new_library_key, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', new_library_key)):
            raise ValueError('new_library_key 需要 32–128 个字母、数字、下划线或连字符')
        if message is not None:
            if not isinstance(message, str) or not 1 <= len(message.strip()) <= 2000:
                raise ValueError('请填写 1–2000 个字符的版本说明')
            message = message.strip()
        package = read_package(data)
        digest = hashlib.sha256(data).hexdigest()
        key_hash = hashlib.sha256(new_library_key.encode()).hexdigest() if new_library_key else None
        version_id = None
        with self.lock:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if key_hash is not None:
                    request = db.execute('SELECT * FROM package_create_requests WHERE owner_id=? AND key_hash=?',
                                         (owner['id'], key_hash)).fetchone()
                    if request:
                        existing = db.execute('SELECT visibility,deleted FROM packages WHERE id=?',
                                              (request['package_id'],)).fetchone()
                        if existing is None or existing['deleted']:
                            raise StoreConflict('创建请求对应的对话库已删除；请使用新的创建标识，避免意外恢复')
                        if request['sha256'] != digest or request['visibility'] != visibility:
                            raise StoreConflict('同一创建标识不能用于不同的内容或可见性')
                        if existing['visibility'] != visibility:
                            raise StoreConflict('创建请求对应库的可见性已变化，请先确认后重新操作')
                        ident, version_id = request['package_id'], request['version_id']
                    else:
                        ident, version_id = self._create_package(db, owner, data, package, visibility, message=message)
                        db.execute('INSERT INTO package_create_requests VALUES (?,?,?,?,?,?)',
                                   (owner['id'], key_hash, ident, version_id, digest, visibility))
                else:
                    duplicate = db.execute('SELECT id FROM packages WHERE owner_id=? AND sha256=? AND visibility=? AND deleted=0', (owner['id'], digest, visibility)).fetchone()
                    if duplicate:
                        ident = duplicate['id']
                    else:
                        ident, _ = self._create_package(db, owner, data, package, visibility, message=message)
        if visibility == 'public':
            self._sync_git_ref(ident)
        return self.detail(ident, owner['id'], version_id)

    def add_version(self, ident, owner, data, base_version, message, draft_revision=None,
                    expected_visibility=None):
        if draft_revision is not None:
            self._expected_revision(draft_revision)
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._package_row(db, ident, owner['id'])
            if row['owner_id'] != owner['id']:
                raise FileNotFoundError('对话库不存在或无权操作')
            if expected_visibility is None:
                if row['visibility_revision']:
                    raise StoreConflict('对话库可见性曾被修改，请刷新页面或升级 ctx 后确认可见性再发布')
            elif expected_visibility not in ('public', 'private'):
                raise ValueError('expected_visibility 必须为 private 或 public')
            elif expected_visibility != row['visibility']:
                raise StoreConflict('对话库可见性已变化，请保留草稿并刷新确认后再发布')
            if not isinstance(message, str) or not message.strip() or len(message) > 2000:
                raise ValueError('请填写 1–2000 个字符的版本说明')
            if not isinstance(base_version, str) or not base_version:
                raise ValueError('需要明确的基准版本')
            package = read_package(data)
            digest = hashlib.sha256(data).hexdigest()
            retry = db.execute('''SELECT id FROM package_versions
                WHERE package_id=? AND parent_id=? AND sha256=? AND message=?''',
                (ident, base_version, digest, message)).fetchone()
            if retry:
                version_id = retry['id']
            else:
                if row['head_version_id'] != base_version:
                    raise StoreConflict('对话库已有新版本，请刷新后比较并重新保存')
                if row['sha256'] == digest:
                    raise ValueError('内容没有变化，无需创建新版本')
                self._save_blob(data)
                version_id = uuid.uuid4().hex
                title, description, count, search = self._fields(package)
                now = datetime.now(timezone.utc).isoformat()
                commit = self._git_snapshot(ident, package, row['git_commit'], message) if row['visibility'] == 'public' else None
                db.execute('''INSERT INTO package_versions
                    (id,package_id,number,parent_id,sha256,title,description,conversation_count,
                     search_text,message,author_id,created_at,git_commit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (version_id, ident, row['version_count'] + 1, base_version, digest, title,
                     description, count, search, message, owner['id'], now, commit))
                changed = db.execute('''UPDATE packages SET head_version_id=?,sha256=?,title=?,description=?,
                    conversation_count=?,search_text=?,git_commit=? WHERE id=? AND head_version_id=? AND deleted=0''',
                    (version_id, digest, title, description, count, search, commit, ident, base_version)).rowcount
                if changed != 1:
                    raise StoreConflict('对话库已有新版本，请刷新后比较并重新保存')
                self._index_platforms(db, ident, package)
            self._consume_draft(db, ident, draft_revision)
        if row['visibility'] == 'public':
            self._sync_git_ref(ident)
        return self.detail(ident, owner['id'], version_id)

    def fork_package(self, ident, owner, version_id, visibility='private'):
        if visibility not in ('private', 'public'):
            raise ValueError('visibility 必须为 private 或 public')
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            source = self._package_row(db, ident, owner['id'])
            if source['visibility'] == 'private' and visibility == 'public':
                raise PermissionError('私有对话库只能创建私有副本')
            version = self._version_row(db, ident, version_id)
            data = (self.blobs / (version['sha256'] + '.contextpack')).read_bytes()
            new_id, new_version = self._create_package(db, owner, data, read_package(data), visibility, version)
            if source['visibility'] == 'private' or source['private_origin']:
                db.execute('UPDATE packages SET private_origin=1 WHERE id=?', (new_id,))
        if visibility == 'public':
            self._sync_git_ref(new_id)
        return self.detail(new_id, owner['id'], new_version)

    @staticmethod
    def public_row(row):
        return {key: row[key] for key in ('id', 'owner_id', 'title', 'description', 'sha256', 'visibility', 'conversation_count', 'created_at', 'git_commit', 'author', 'head_version_id', 'version_count')}

    @staticmethod
    def _version_metadata(row):
        return {key: row[key] for key in ('id', 'number', 'parent_id', 'sha256', 'message', 'author', 'created_at', 'git_commit')}

    @staticmethod
    def _package_row(db, ident, user_id):
        row = db.execute("""SELECT p.*,u.username AS author,
            (SELECT count(*) FROM package_versions v WHERE v.package_id=p.id) AS version_count
            FROM packages p JOIN users u ON u.id=p.owner_id
            WHERE p.id=? AND p.deleted=0 AND (p.visibility='public' OR p.owner_id=?)""", (ident, user_id)).fetchone()
        if row is None:
            raise FileNotFoundError('对话库不存在或无权访问')
        return row

    @staticmethod
    def _version_row(db, ident, version_id):
        row = db.execute('''SELECT v.*,u.username AS author FROM package_versions v
            JOIN users u ON u.id=v.author_id WHERE v.package_id=? AND v.id=?''', (ident, version_id)).fetchone()
        if row is None:
            raise FileNotFoundError('版本不存在或无权访问')
        return row

    def _upstream(self, db, row, user_id):
        if not row['upstream_package_id']:
            return None
        try:
            source = self._package_row(db, row['upstream_package_id'], user_id)
            version = self._version_row(db, source['id'], row['upstream_version_id'])
        except FileNotFoundError:
            return None
        return {'package_id': source['id'], 'version_id': version['id'], 'title': version['title'],
                'author': source['author'], 'number': version['number']}

    def _selected(self, ident, user_id, version_id=None):
        with self.db() as db:
            row = self._package_row(db, ident, user_id)
            version = self._version_row(db, ident, row['head_version_id'] if version_id is None else version_id)
            result = self.public_row(row)
            result.update({key: version[key] for key in ('title', 'description', 'sha256', 'conversation_count', 'git_commit')})
            result.update(version=self._version_metadata(version), upstream=self._upstream(db, row, user_id))
        return result

    def list_packages(self, user_id=None, query='', platform=None):
        selected_platform = normalize_platform(platform) if platform is not None else None
        with self.db() as db:
            db.execute('BEGIN')
            rows = db.execute("""SELECT p.*,u.username AS author,
                (SELECT count(*) FROM package_versions v WHERE v.package_id=p.id) AS version_count
                FROM packages p JOIN users u ON u.id=p.owner_id WHERE p.deleted=0
                AND (p.visibility='public' OR p.owner_id=?) AND instr(lower(p.search_text),lower(?))>0
                AND (? IS NULL OR EXISTS (SELECT 1 FROM package_platforms c WHERE c.package_id=p.id AND c.platform=?))
                ORDER BY p.created_at DESC LIMIT 200""", (user_id, query[:200], selected_platform, selected_platform)).fetchall()
            categories = self._indexed_platforms(db, [row['id'] for row in rows])
            return [{**self.public_row(row), 'upstream': self._upstream(db, row, user_id),
                     'platforms': categories[row['id']]} for row in rows]

    def get(self, ident, user_id=None):
        with self.db() as db:
            db.execute('BEGIN')
            row = self._package_row(db, ident, user_id)
            return {**self.public_row(row), 'upstream': self._upstream(db, row, user_id),
                    'platforms': self._indexed_platforms(db, [ident])[ident]}

    def download(self, ident, user_id=None, version_id=None):
        row = self._selected(ident, user_id, version_id)
        return (self.blobs / (row['sha256'] + '.contextpack')).read_bytes()

    def detail(self, ident, user_id=None, version_id=None):
        from .checkpoints import checkpoint_status
        row = self._selected(ident, user_id, version_id)
        package = read_package((self.blobs / (row['sha256'] + '.contextpack')).read_bytes())
        return {**row, 'title': package['library']['title'], 'description': package['library']['description'],
                'conversations': package['conversations'], 'compatibility': package['compatibility'],
                'library_source': package['library']['source'], 'platforms': package_platforms(package),
                'code_checkpoint': package['library'].get('code_checkpoint'),
                'code_checkpoint_status': checkpoint_status(package)}

    def versions(self, ident, user_id=None):
        with self.db() as db:
            self._package_row(db, ident, user_id)
            rows = db.execute('''SELECT v.*,u.username AS author FROM package_versions v
                JOIN users u ON u.id=v.author_id WHERE v.package_id=? ORDER BY v.number DESC''', (ident,)).fetchall()
            return [self._version_metadata(row) for row in rows]

    def delete(self, ident, user_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owned(db, ident, user_id)
            self._remove_git_ref(ident)
            changed = db.execute('UPDATE packages SET deleted=1 WHERE id=? AND owner_id=? AND deleted=0', (ident, user_id)).rowcount
            if not changed:
                raise FileNotFoundError('对话库不存在或无权操作')
