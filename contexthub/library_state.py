"""Owner-only drafts, visibility transitions and retryable physical cleanup."""

import json
from datetime import datetime, timezone

from .package import read_package
from .revisions import build_revision


def _now():
    return datetime.now(timezone.utc).isoformat()


class LibraryState:
    def _migrate_library_state(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(packages)')}
            if 'private_origin' not in columns:
                db.execute('ALTER TABLE packages ADD COLUMN private_origin INTEGER NOT NULL DEFAULT 0')
                db.execute('''UPDATE packages SET private_origin=1 WHERE upstream_package_id IN
                    (SELECT id FROM packages WHERE visibility='private')''')
            if 'visibility_revision' not in columns:
                db.execute('ALTER TABLE packages ADD COLUMN visibility_revision INTEGER NOT NULL DEFAULT 0')
            db.execute('''CREATE TABLE IF NOT EXISTS package_drafts (
                package_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                payload_json TEXT, updated_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS package_cleanup (
                package_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                digests_json TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS package_create_requests (
                owner_id TEXT NOT NULL, key_hash TEXT NOT NULL,
                package_id TEXT, version_id TEXT, sha256 TEXT, visibility TEXT,
                PRIMARY KEY(owner_id,key_hash))''')

    def _owned(self, db, ident, owner_id):
        row = self._package_row(db, ident, owner_id)
        if row['owner_id'] != owner_id:
            raise FileNotFoundError('对话库不存在或无权操作')
        return row

    @staticmethod
    def _draft_state(db, ident):
        row = db.execute('SELECT * FROM package_drafts WHERE package_id=?', (ident,)).fetchone()
        revision = row['revision'] if row else 0
        draft = json.loads(row['payload_json']) if row and row['payload_json'] else None
        if draft is not None:
            draft.update(revision=revision, updated_at=row['updated_at'])
        return {'draft': draft, 'revision': revision}

    def get_draft(self, ident, owner_id):
        with self.db() as db:
            db.execute('BEGIN')
            self._owned(db, ident, owner_id)
            return self._draft_state(db, ident)

    @staticmethod
    def _expected_revision(value):
        if type(value) is not int or not 0 <= value < 2**53:
            raise ValueError('需要有效的草稿 expected_revision')
        return value

    def save_draft(self, ident, owner_id, payload):
        from .store import StoreConflict
        expected = self._expected_revision(payload.get('expected_revision'))
        message = payload.get('message', '')
        if not isinstance(message, str) or len(message) > 2000:
            raise ValueError('草稿版本说明不能超过 2000 个字符')
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owned(db, ident, owner_id)
            base_id = payload.get('base_version')
            if not isinstance(base_id, str) or not base_id:
                raise ValueError('草稿需要明确的库内基准版本')
            version = self._version_row(db, ident, base_id)
            if self._draft_state(db, ident)['revision'] != expected:
                raise StoreConflict('其他页面已更新草稿，请先加载服务器草稿，避免覆盖')
            # Autosave protects unfinished editing even if another client has
            # published a newer head. Publishing still compares the head CAS;
            # retain the explicit historical base for later reconciliation.
            base = read_package((self.blobs / (version['sha256'] + '.contextpack')).read_bytes())
            package = read_package(build_revision(base, payload))
            # Store validated canonical data; never accept forged source/edit
            # provenance from the editor or persist an unbounded arbitrary JSON.
            draft = {'base_version': base_id,
                     'title': package['library']['title'],
                     'description': package['library']['description'],
                     'code_checkpoint': package['library'].get('code_checkpoint'),
                     'conversations': package['conversations'], 'message': message}
            db.execute('''INSERT INTO package_drafts VALUES (?,?,?,?)
                ON CONFLICT(package_id) DO UPDATE SET revision=excluded.revision,
                payload_json=excluded.payload_json,updated_at=excluded.updated_at''',
                (ident, expected + 1, json.dumps(draft, ensure_ascii=False), _now()))
            return self._draft_state(db, ident)

    def delete_draft(self, ident, owner_id, expected_revision):
        from .store import StoreConflict
        expected = self._expected_revision(expected_revision)
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owned(db, ident, owner_id)
            if self._draft_state(db, ident)['revision'] != expected:
                raise StoreConflict('其他页面已更新草稿，请先加载服务器草稿，避免删除')
            db.execute('''INSERT INTO package_drafts VALUES (?,?,NULL,?)
                ON CONFLICT(package_id) DO UPDATE SET revision=excluded.revision,
                payload_json=NULL,updated_at=excluded.updated_at''', (ident, expected + 1, _now()))
            return {'deleted': True, 'revision': expected + 1}

    @staticmethod
    def _consume_draft(db, ident, revision):
        if revision is not None:
            db.execute('''UPDATE package_drafts SET payload_json=NULL,revision=revision+1,
                updated_at=? WHERE package_id=? AND revision=? AND payload_json IS NOT NULL''',
                (_now(), ident, revision))

    def set_visibility(self, ident, owner_id, visibility, base_version):
        from .store import StoreConflict
        if visibility not in ('public', 'private'):
            raise ValueError('visibility 必须为 private 或 public')
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._owned(db, ident, owner_id)
            if base_version != row['head_version_id']:
                raise StoreConflict('对话库已有新版本，请刷新后修改可见性')
            if visibility != row['visibility']:
                commit = None
                if visibility == 'public':
                    upstream = (db.execute('SELECT visibility,deleted FROM packages WHERE id=?',
                                (row['upstream_package_id'],)).fetchone()
                                if row['upstream_package_id'] else None)
                    if row['private_origin'] or (row['upstream_package_id'] and
                            (not upstream or upstream['deleted'] or upstream['visibility'] != 'public')):
                        raise PermissionError('私有或不可访问上游的 Fork 不能转为公开')
                    package = read_package((self.blobs / (row['sha256'] + '.contextpack')).read_bytes())
                    # No parent: private versions must never enter public Git
                    # ancestry as a side effect of publishing the current head.
                    commit = self._git_snapshot(ident, package, message='将当前快照设为公开')
                    db.execute('UPDATE package_versions SET git_commit=? WHERE id=?',
                               (commit, row['head_version_id']))
                else:
                    self._remove_git_ref(ident)
                    db.execute('UPDATE package_versions SET git_commit=NULL WHERE package_id=?', (ident,))
                db.execute('''UPDATE packages SET visibility=?,git_commit=?,
                    visibility_revision=visibility_revision+1 WHERE id=?''',
                           (visibility, commit, ident))
                if self._has_contributions(db):
                    # Re-publication creates a fresh Git root even when the
                    # version ID is unchanged. Old checks must be rebuilt with
                    # the new parent OIDs, or they would look ready forever.
                    db.execute('''UPDATE contributions SET check_json=NULL WHERE status!='merged'
                        AND (source_package_id=? OR target_package_id=?)''', (ident, ident))
        if visibility == 'public':
            self._sync_git_ref(ident)
        return self.detail(ident, owner_id)

    def _remove_git_ref(self, ident):
        if (self.root / 'public.git').exists():
            self._git('update-ref', '-d', f'refs/heads/packages/{ident}')

    @staticmethod
    def _has_contributions(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='contributions'").fetchone() is not None

    def _referenced_digests(self, db):
        # Retained soft-deleted libraries are also references. Purging one
        # library must not silently erase another library's retained storage.
        digests = {row[0] for row in db.execute('SELECT sha256 FROM package_versions')}
        if self._has_contributions(db):
            for row in db.execute('SELECT check_json FROM contributions WHERE check_json IS NOT NULL'):
                digest = json.loads(row[0]).get('candidate_sha256')
                if digest:
                    digests.add(digest)
        return digests

    def _prune_git(self, db):
        if not (self.root / 'public.git').exists():
            return
        # SQLite's writer lock and storage-budget admission lock stay held
        # through repack/prune. Pending snapshots cannot be pruned before their
        # metadata commits. Extra roots preserve unpublished merge candidates
        # and retained libraries without creating publicly advertised refs.
        roots = {row[0] for row in db.execute(
            'SELECT git_commit FROM package_versions WHERE git_commit IS NOT NULL')}
        if self._has_contributions(db):
            for row in db.execute('SELECT check_json FROM contributions WHERE check_json IS NOT NULL'):
                oid = json.loads(row[0]).get('candidate_oid')
                if oid:
                    roots.add(oid)
        self._git('repack', '-A', '-d')
        self._git('prune', '--expire=now', *sorted(roots))

    def purge(self, ident, owner_id, confirmation):
        if confirmation != ident:
            raise ValueError('永久删除需要 confirmation 与对话库 ID 完全一致')
        # First durably revoke access and record only hashes needed to finish
        # cleanup. Files are removed after this commit; a crash cannot roll
        # metadata back to point at missing files. A failed cleanup is retryable.
        with self.lock, self.budget.hold():
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM packages WHERE id=? AND owner_id=?', (ident, owner_id)).fetchone()
                job = db.execute('SELECT * FROM package_cleanup WHERE package_id=? AND owner_id=?', (ident, owner_id)).fetchone()
                if row is None and job is None:
                    raise FileNotFoundError('对话库不存在或无权操作')
                if row is not None:
                    digests = {r[0] for r in db.execute('SELECT sha256 FROM package_versions WHERE package_id=?', (ident,))}
                    if self._has_contributions(db):
                        proposals = db.execute('''SELECT id,check_json FROM contributions
                            WHERE source_package_id=? OR target_package_id=?''', (ident, ident)).fetchall()
                        for proposal in proposals:
                            if proposal['check_json']:
                                digest = json.loads(proposal['check_json']).get('candidate_sha256')
                                if digest:
                                    digests.add(digest)
                            db.execute('DELETE FROM contribution_comments WHERE contribution_id=?', (proposal['id'],))
                            db.execute('DELETE FROM contributions WHERE id=?', (proposal['id'],))
                    self._remove_git_ref(ident)
                    db.execute('INSERT OR REPLACE INTO package_cleanup VALUES (?,?,?)',
                               (ident, owner_id, json.dumps(sorted(digests))))
                    db.execute('DELETE FROM package_drafts WHERE package_id=?', (ident,))
                    db.execute('DELETE FROM package_platforms WHERE package_id=?', (ident,))
                    db.execute('DELETE FROM package_versions WHERE package_id=?', (ident,))
                    # Retain only owner/key-hash tombstones: a delayed first
                    # upload retry must not resurrect a permanently removed
                    # library. Erase all package IDs, payload hashes and state.
                    db.execute('''UPDATE package_create_requests SET package_id=NULL,
                        version_id=NULL,sha256=NULL,visibility=NULL WHERE package_id=?''', (ident,))
                    db.execute('DELETE FROM packages WHERE id=?', (ident,))
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                job = db.execute('SELECT * FROM package_cleanup WHERE package_id=? AND owner_id=?', (ident, owner_id)).fetchone()
                referenced = self._referenced_digests(db)
                removed, removed_bytes, shared = 0, 0, 0
                for digest in json.loads(job['digests_json']):
                    if digest in referenced:
                        shared += 1
                        continue
                    path = self.blobs / (digest + '.contextpack')
                    if path.exists():
                        size = path.stat().st_size
                        path.unlink()
                        removed += 1
                        removed_bytes += size
                self._prune_git(db)
            with self.db() as db:
                # Secure-delete overwrites freed SQLite cells. Truncate the WAL
                # only after its transaction commits, so prior message frames
                # are also removed. Keep the job if an external reader blocks it.
                checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0]:
                    from .store import StoreConflict
                    raise StoreConflict('访问已撤销；数据库正被读取，请稍后重试永久清理')
                db.execute('DELETE FROM package_cleanup WHERE package_id=?', (ident,))
            return {'purged': True, 'removed_blobs': removed, 'removed_bytes': removed_bytes,
                    'retained_shared_blobs': shared, 'backups_unchanged': True}
