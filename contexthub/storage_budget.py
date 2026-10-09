"""Conservative disk-write admission, not a filesystem quota.

Cooperating Store instances serialize their database/blob/Git work with one
advisory lock. Each write reserves a pessimistic amount of headroom against
both the data-directory budget and free filesystem space. Unrelated processes
can still consume disk space; enforce a filesystem quota for a hard limit.
"""

import os
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager

from . import safe_fs


class StoreCapacityError(Exception):
    """A durable write cannot be admitted within the configured disk budget."""


_locks = {}
_locks_guard = threading.Lock()


class StorageBudget:
    def __init__(self, root, max_storage_bytes=None, min_free_bytes=0):
        for name, value in (('max_storage_bytes', max_storage_bytes), ('min_free_bytes', min_free_bytes)):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f'{name} must be a non-negative integer')
        if max_storage_bytes == 0:
            raise ValueError('max_storage_bytes must be positive or None')
        if min_free_bytes is None:
            raise ValueError('min_free_bytes must be a non-negative integer')
        self.root = root
        self.max_storage_bytes = max_storage_bytes
        self.min_free_bytes = min_free_bytes
        self.enabled = max_storage_bytes is not None or min_free_bytes > 0
        lock_key = os.path.normcase(os.path.abspath(root))
        with _locks_guard:
            self._lock, self._local = _locks.setdefault(lock_key, (threading.RLock(), threading.local()))

    @contextmanager
    def hold(self):
        if not self.enabled:
            yield
            return
        with self._lock:
            depth = getattr(self._local, 'depth', 0)
            if depth:
                self._local.depth += 1
                try:
                    yield
                finally:
                    self._local.depth -= 1
                return
            # The shared per-root thread state also makes nested calls through
            # a second Store instance reentrant, without acquiring flock twice.
            with (self.root / '.storage.lock').open('a+b') as handle:
                safe_fs.lock(handle.fileno(), blocking=True)
                self._local.depth = 1
                try:
                    yield
                finally:
                    self._local.depth = 0
                    # Closing the handle releases both LockFileEx and flock.

    @staticmethod
    def _size(stat):
        # Count allocated blocks and logical bytes, whichever is larger. This
        # deliberately does not give sparse files or hard links a quota credit.
        return max(stat.st_size, getattr(stat, 'st_blocks', 0) * 512)

    def usage_bytes(self):
        total = self._size(self.root.stat())
        pending = [self.root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        total += self._size(entry.stat(follow_symlinks=False))
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                    except FileNotFoundError:
                        # A checkpoint can remove a WAL after closing its last
                        # reader. Files removed here consume no further space.
                        continue
        return total

    def ensure(self, additional_bytes):
        if not self.enabled:
            return
        with self.hold():
            if (self.max_storage_bytes is not None
                    and self.usage_bytes() + additional_bytes > self.max_storage_bytes):
                raise StoreCapacityError('服务存储预算不足，暂时无法保存；请联系管理员')
            if shutil.disk_usage(self.root).free < self.min_free_bytes + additional_bytes:
                raise StoreCapacityError('服务器可用磁盘空间不足，暂时无法保存；请联系管理员')

    def before_sql(self, connection, sql, parameters=()):
        statement = sql.lstrip()
        command = statement.split(None, 1)[0].upper() if statement else ''
        if command in ('SELECT', 'EXPLAIN', 'BEGIN', 'COMMIT', 'ROLLBACK', 'SAVEPOINT', 'RELEASE'):
            return
        if command == 'PRAGMA':
            # Application read pragmas and an already enabled WAL mode do not
            # need capacity. Other pragmas are conservatively treated as writes.
            if re.fullmatch(r'PRAGMA\s+(?:table_info\s*\(\w+\)|journal_mode|page_count|page_size)\s*;?', statement, re.I):
                return
            if re.fullmatch(r'PRAGMA\s+journal_mode\s*=\s*WAL\s*;?', statement, re.I):
                mode = sqlite3.Connection.execute(connection, 'PRAGMA journal_mode').fetchone()[0]
                if mode.lower() == 'wal':
                    return
        existing = re.match(r'CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+(\w+)', statement, re.I)
        if existing:
            row = sqlite3.Connection.execute(connection,
                'SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                (existing[1].lower(), existing[2])).fetchone()
            if row:
                return
        values = parameters.values() if isinstance(parameters, dict) else parameters
        payload = len(statement.encode('utf-8'))
        for value in values:
            payload += len(value.encode('utf-8')) if isinstance(value, str) else (
                len(value) if isinstance(value, (bytes, bytearray, memoryview)) else 32)
        database_bytes = 0
        for name in ('hub.sqlite3', 'hub.sqlite3-wal'):
            try:
                database_bytes += self._size((self.root / name).stat())
            except FileNotFoundError:
                pass
        # A large DELETE/UPDATE may rewrite existing pages; a checkpoint may
        # allocate their database copies as well. Reserve for both, plus new
        # bound text/index records and small B-tree/schema/SHM overhead. This
        # intentionally refuses some writes before the nominal byte ceiling.
        self.ensure(2 * database_bytes + 4 * payload + 256 * 1024)


class BudgetCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        self.connection.budget.before_sql(self.connection, sql, parameters)
        return super().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        for parameters in seq_of_parameters:
            self.execute(sql, parameters)
        return self

    def executescript(self, script):
        if self.connection.in_transaction:
            self.connection.commit()
        pending = ''
        for character in script:
            pending += character
            if character == ';' and sqlite3.complete_statement(pending):
                self.execute(pending)
                pending = ''
        if pending.strip():
            self.execute(pending)
        return self


class BudgetConnection(sqlite3.Connection):
    def cursor(self, factory=BudgetCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, script):
        return self.cursor().executescript(script)
