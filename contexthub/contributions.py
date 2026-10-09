"""Public, fixed-revision contribution reviews and atomic checked merges.

Git candidates are immutable objects. The SQLite transaction decides which
candidate becomes a published version; public refs follow that decision. Every
operation authorizes all source, target and fork-point dependencies again.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from .package import read_package
from .revisions import compare_packages
from .store import StoreConflict


RULES_VERSION = 'contexthub-merge-1'
MAX_COMMENTS = 200


def _now():
    return datetime.now(timezone.utc).isoformat()


def _same_content(first, second):
    # Compression choices and ZIP timestamps do not constitute contributions.
    # Compare full canonical fields, including edit history and source claims.
    return (first['library'] == second['library']
            and first['conversations'] == second['conversations'])


def _text(value, label, maximum, *, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f'{label}需为 {1 if required else 0}–{maximum} 个字符的文本')
    # SQLite cannot encode isolated surrogate characters in accepted JSON text.
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError(f'{label}包含无效字符') from exc
    return value.strip() if required else value


class Contributions:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS contributions (
                id TEXT PRIMARY KEY, source_package_id TEXT NOT NULL,
                source_version_id TEXT NOT NULL, target_package_id TEXT NOT NULL,
                fork_point_version_id TEXT NOT NULL, author_id TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                check_json TEXT, merged_version_id TEXT)''')
            db.execute('''CREATE INDEX IF NOT EXISTS contributions_target
                ON contributions(target_package_id, created_at DESC)''')
            db.execute('''CREATE INDEX IF NOT EXISTS contributions_source
                ON contributions(source_package_id, created_at DESC)''')
            db.execute('''CREATE TABLE IF NOT EXISTS contribution_comments (
                id TEXT PRIMARY KEY, contribution_id TEXT NOT NULL, author_id TEXT NOT NULL,
                body TEXT NOT NULL, created_at TEXT NOT NULL)''')
            db.execute('''CREATE INDEX IF NOT EXISTS comments_contribution
                ON contribution_comments(contribution_id, created_at)''')

    def _public(self, db, ident, user_id):
        row = self.store._package_row(db, ident, user_id)
        if row['visibility'] != 'public':
            raise FileNotFoundError('贡献所依赖的公开对话库不存在或不可访问')
        return row

    def _context(self, db, ident, user_id):
        proposal = db.execute('''SELECT c.*,u.username AS author FROM contributions c
            JOIN users u ON u.id=c.author_id WHERE c.id=?''', (ident,)).fetchone()
        if proposal is None:
            raise FileNotFoundError('贡献请求不存在或不可访问')
        source = self._public(db, proposal['source_package_id'], user_id)
        target = self._public(db, proposal['target_package_id'], user_id)
        if (source['upstream_package_id'] != target['id']
                or source['upstream_version_id'] != proposal['fork_point_version_id']):
            raise FileNotFoundError('贡献来源关系不存在或不可访问')
        version = self.store._version_row(db, source['id'], proposal['source_version_id'])
        ancestor = self.store._version_row(db, target['id'], proposal['fork_point_version_id'])
        head = self.store._version_row(db, target['id'], target['head_version_id'])
        return proposal, source, version, target, ancestor, head

    @staticmethod
    def _authorize(actor, owner_ids):
        if not actor or actor.get('id') not in owner_ids:
            raise PermissionError('无权执行此贡献操作')

    def _read(self, version):
        data = (self.store.blobs / (version['sha256'] + '.contextpack')).read_bytes()
        if hashlib.sha256(data).hexdigest() != version['sha256']:
            raise ValueError('存储内容校验失败，请检查备份')
        return data, read_package(data)

    @staticmethod
    def _check(proposal, target):
        if not proposal['check_json']:
            return None
        result = json.loads(proposal['check_json'])
        result['stale'] = proposal['status'] != 'merged' and (
            result['base_version_id'] != target['head_version_id']
            or result['source_version_id'] != proposal['source_version_id']
            or result['rules_version'] != RULES_VERSION)
        return result

    def _format(self, db, context, user_id, *, full=True):
        proposal, source, version, target, ancestor, _head = context
        checked = self._check(proposal, target)
        source_owner = user_id is not None and source['owner_id'] == user_id
        target_owner = user_id is not None and target['owner_id'] == user_id
        participant = source_owner or target_owner
        is_open = proposal['status'] == 'open'
        result = {key: proposal[key] for key in (
            'id', 'title', 'description', 'status', 'author', 'author_id',
            'created_at', 'updated_at', 'fork_point_version_id', 'merged_version_id')}
        result.update(
            source={'package_id': source['id'], 'title': version['title'], 'author': source['author'],
                    'version_id': version['id'], 'number': version['number'],
                    'head_version_id': source['head_version_id']},
            target={'package_id': target['id'], 'title': target['title'], 'author': target['author'],
                    'head_version_id': target['head_version_id']},
            permissions={
                'can_check': target_owner and is_open,
                'can_merge': bool(target_owner and is_open and checked
                                  and checked['status'] == 'ready' and not checked['stale']),
                'can_update': source_owner and proposal['status'] != 'merged',
                'can_comment': participant,
                'can_close': participant and is_open,
                'can_reopen': participant and proposal['status'] == 'closed',
            })
        if full:
            result['check'] = checked
            rows = db.execute('''SELECT c.id,u.username AS author,c.body,c.created_at
                FROM contribution_comments c JOIN users u ON u.id=c.author_id
                WHERE contribution_id=? ORDER BY c.created_at,c.id LIMIT ?''',
                (proposal['id'], MAX_COMMENTS)).fetchall()
            result['comments'] = [dict(row) for row in rows]
            result['comparison'] = compare_packages(self._read(ancestor)[1], self._read(version)[1])
        else:
            # Summaries never include checked bodies, conflicts or comment text.
            result['check'] = ({key: checked[key] for key in (
                'id', 'status', 'stale', 'checked_at', 'conflict_count')} if checked else None)
        return result

    def _source(self, db, source_id, actor, source_version_id):
        source = self._public(db, source_id, actor.get('id'))
        self._authorize(actor, {source['owner_id']})
        if not source['upstream_package_id'] or not source['upstream_version_id']:
            raise ValueError('请选择直接从上游 Fork 的公开对话库')
        target = self._public(db, source['upstream_package_id'], actor.get('id'))
        if not isinstance(source_version_id, str) or not source_version_id:
            raise ValueError('请选择要贡献的固定源版本')
        version = self.store._version_row(db, source['id'], source_version_id)
        ancestor = self.store._version_row(db, target['id'], source['upstream_version_id'])
        if _same_content(self._read(version)[1], self._read(ancestor)[1]):
            raise ValueError('所选源版本相对 Fork 起点没有变化')
        return source, version, target, ancestor

    def create(self, source_id, actor, source_version_id, title, description=''):
        title = _text(title, '贡献标题', 200, required=True)
        description = _text(description, '贡献说明', 10000)
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            source, version, target, ancestor = self._source(db, source_id, actor, source_version_id)
            ident, now = uuid.uuid4().hex, _now()
            db.execute('''INSERT INTO contributions
                (id,source_package_id,source_version_id,target_package_id,fork_point_version_id,
                 author_id,title,description,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'open',?,?)''',
                (ident, source['id'], version['id'], target['id'], ancestor['id'], actor['id'],
                 title, description, now, now))
            result = self._format(db, self._context(db, ident, actor['id']), actor['id'])
        return result

    def list(self, package_id, user_id=None):
        with self.store.db() as db:
            db.execute('BEGIN')
            self._public(db, package_id, user_id)
            rows = db.execute('''SELECT id FROM contributions
                WHERE target_package_id=? OR source_package_id=? ORDER BY created_at DESC LIMIT 200''',
                (package_id, package_id)).fetchall()
            result = []
            for row in rows:
                try:
                    context = self._context(db, row['id'], user_id)
                except FileNotFoundError:
                    continue
                result.append(self._format(db, context, user_id, full=False))
            return result

    def detail(self, ident, user_id=None):
        with self.store.db() as db:
            db.execute('BEGIN')
            return self._format(db, self._context(db, ident, user_id), user_id)

    def comment(self, ident, actor, body):
        body = _text(body, '评论', 4000, required=True)
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            context = self._context(db, ident, actor['id'])
            self._authorize(actor, {context[1]['owner_id'], context[3]['owner_id']})
            count = db.execute('SELECT count(*) FROM contribution_comments WHERE contribution_id=?', (ident,)).fetchone()[0]
            if count >= MAX_COMMENTS:
                raise ValueError(f'单个贡献请求最多保留 {MAX_COMMENTS} 条评论')
            now = _now()
            db.execute('INSERT INTO contribution_comments VALUES (?,?,?,?,?)',
                       (uuid.uuid4().hex, ident, actor['id'], body, now))
            db.execute('UPDATE contributions SET updated_at=? WHERE id=?', (now, ident))
            return self._format(db, self._context(db, ident, actor['id']), actor['id'])

    def set_status(self, ident, actor, status):
        if status not in ('open', 'closed'):
            raise ValueError('只能关闭或重新打开贡献请求')
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            context = self._context(db, ident, actor['id'])
            self._authorize(actor, {context[1]['owner_id'], context[3]['owner_id']})
            if context[0]['status'] == 'merged':
                raise StoreConflict('贡献已采用，不能修改其状态')
            if context[0]['status'] != status:
                db.execute('UPDATE contributions SET status=?,check_json=NULL,updated_at=? WHERE id=?',
                           (status, _now(), ident))
            return self._format(db, self._context(db, ident, actor['id']), actor['id'])

    def update_source(self, ident, actor, source_version_id):
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            context = self._context(db, ident, actor['id'])
            self._authorize(actor, {context[1]['owner_id']})
            if context[0]['status'] == 'merged':
                raise StoreConflict('贡献已采用，不能更换源版本')
            _, version, _, _ = self._source(db, context[1]['id'], actor, source_version_id)
            if version['id'] != context[0]['source_version_id']:
                db.execute('''UPDATE contributions SET source_version_id=?,check_json=NULL,
                    updated_at=? WHERE id=?''', (version['id'], _now(), ident))
            return self._format(db, self._context(db, ident, actor['id']), actor['id'])

    def check(self, ident, actor, expected_target_version_id):
        # Import only when needed, keeping store migrations independent of the
        # merge engine and its versioned review rules.
        from .merge import merge_packages

        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            context = self._context(db, ident, actor['id'])
            proposal, _source, version, target, ancestor, head = context
            self._authorize(actor, {target['owner_id']})
            if proposal['status'] != 'open':
                raise StoreConflict('请先打开贡献请求，再检查候选')
            if expected_target_version_id != head['id']:
                raise StoreConflict('上游已有新版本，请刷新贡献请求后重新检查')
            previous = self._check(proposal, target)
            if previous and not previous['stale']:
                return self._format(db, context, actor['id'])
            _, target_package = self._read(head)
            result = merge_packages(self._read(ancestor)[1], target_package, self._read(version)[1])
            checked = {
                'id': uuid.uuid4().hex, 'status': 'conflicts', 'stale': False,
                'base_version_id': head['id'], 'source_version_id': version['id'],
                'candidate_oid': None, 'candidate_sha256': None, 'rules_version': RULES_VERSION,
                'conflicts': result['conflicts'], 'conflict_count': result['conflict_count'],
                'truncated': result['truncated'], 'comparison': None, 'checked_at': _now(),
            }
            if result['package'] is not None and not result['conflict_count']:
                data = result['package']
                package = read_package(data)
                checked['comparison'] = compare_packages(target_package, package)
                if _same_content(package, target_package):
                    checked['status'] = 'unchanged'
                else:
                    if not head['git_commit'] or not version['git_commit']:
                        raise ValueError('公开版本缺少 Git 提交，无法生成双父候选')
                    checked['status'] = 'ready'
                    checked['candidate_sha256'] = self.store._save_blob(data)
                    checked['candidate_oid'] = self.store._git_snapshot(
                        target['id'], package, head['git_commit'],
                        f'采用贡献：{proposal["title"]}', second_parent=version['git_commit'])
            db.execute('UPDATE contributions SET check_json=?,updated_at=? WHERE id=?',
                       (json.dumps(checked, ensure_ascii=False), checked['checked_at'], ident))
            return self._format(db, self._context(db, ident, actor['id']), actor['id'])

    def candidate(self, ident, user_id=None, check_id=None):
        with self.store.db() as db:
            db.execute('BEGIN')
            context = self._context(db, ident, user_id)
            proposal, _, _, target, _, _ = context
            checked = self._check(proposal, target)
            if (not checked or checked['id'] != check_id or checked['status'] != 'ready'
                    or checked['stale'] or proposal['status'] not in ('open', 'merged')):
                raise StoreConflict('候选不存在或已失效，请重新检查')
            return self._read({'sha256': checked['candidate_sha256']})[0]

    def merge(self, ident, actor, check_id, candidate_oid):
        with self.store.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            context = self._context(db, ident, actor['id'])
            proposal, _, version, target, _, head = context
            self._authorize(actor, {target['owner_id']})
            checked = self._check(proposal, target)
            if (not checked or checked['id'] != check_id or checked['candidate_oid'] != candidate_oid
                    or checked['status'] != 'ready'):
                raise StoreConflict('确认的候选与当前检查不一致，请重新审阅')
            if proposal['status'] == 'merged':
                # Retry the accepted proposal, not the newer target head.
                return self._format(db, context, actor['id'])
            if proposal['status'] != 'open' or checked['stale']:
                raise StoreConflict('贡献已关闭或上游发生变化，请重新检查')
            data, package = self._read({'sha256': checked['candidate_sha256']})
            parents = self.store._git('show', '-s', '--format=%P', candidate_oid).split()
            if parents != [head['git_commit'], version['git_commit']]:
                raise StoreConflict('候选的父版本与本次检查不一致，请重新检查')
            version_id, now = uuid.uuid4().hex, _now()
            title, description, count, search = self.store._fields(package)
            message = f'采用贡献：{proposal["title"]}'
            db.execute('''INSERT INTO package_versions
                (id,package_id,number,parent_id,sha256,title,description,conversation_count,
                 search_text,message,author_id,created_at,git_commit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (version_id, target['id'], target['version_count'] + 1, head['id'],
                 checked['candidate_sha256'], title, description, count, search, message,
                 actor['id'], now, candidate_oid))
            changed = db.execute('''UPDATE packages SET head_version_id=?,sha256=?,title=?,description=?,
                conversation_count=?,search_text=?,git_commit=? WHERE id=? AND head_version_id=? AND deleted=0''',
                (version_id, checked['candidate_sha256'], title, description, count, search,
                 candidate_oid, target['id'], head['id'])).rowcount
            if changed != 1:
                raise StoreConflict('上游已有新版本，请重新检查')
            db.execute('''UPDATE contributions SET status='merged',merged_version_id=?,updated_at=?
                WHERE id=?''', (version_id, now, ident))
            result = self._format(db, self._context(db, ident, actor['id']), actor['id'])
        self.store._sync_git_ref(target['id'])
        return result
