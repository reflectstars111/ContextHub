"""Bounded, inert adapters for uploaded conversation exports.

Adapters project visible text, never native execution/session state. Unknown
formats require explicit text archive mode; no parser failure silently archives
credentials or account files. Detection identifies structure, not authenticity.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import stat
import zipfile
import zlib

from .package import (
    MAX_ARCHIVE_BYTES, MAX_CONVERSATION_BYTES, MAX_CONVERSATIONS, MAX_MESSAGES,
    MAX_TOTAL_BYTES, PackageError, build_package, read_package,
)

MAX_IMPORT_BYTES = 16 * 1024 * 1024
MAX_IMPORT_FILES = 1000
MAX_RECORDS = 20000
_PLATFORMS = {'auto', 'codex', 'chatgpt', 'claude', 'deepseek', 'other'}
_SESSION_LOG = re.compile(r'session(?:\.v[0-9]+)?\.jsonl\Z')
_TEXT_EXTENSIONS = {'.json', '.jsonl', '.txt', '.md'}


def _basename(filename):
    if not isinstance(filename, str):
        raise PackageError('filename must be text')
    value = filename.replace('\\', '/').rsplit('/', 1)[-1]
    value = ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127)
    return value.encode('utf-8', errors='replace')[:240].decode('utf-8', errors='ignore') or 'conversation.txt'


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageError('Duplicate JSON key in uploaded data')
        result[key] = value
    return result


def _prescan_json(text):
    # Bound nesting and structural tokens before json.loads allocates its tree.
    # Quotes and backslash escapes are tracked so message text is not counted.
    quoted = escaped = False
    depth = tokens = 0
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
            tokens += 1
        elif character in '[{':
            depth += 1
            tokens += 1
        elif character in ']}':
            depth -= 1
        elif character in ',:':
            tokens += 1
        if depth > 64 or tokens > 300000:
            raise PackageError('JSON nesting or node count exceeds the import limit')


def _load_json(text):
    _prescan_json(text)
    def invalid_constant(_value):
        raise PackageError('Nonfinite JSON number in uploaded data')
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise PackageError('Invalid JSON; use explicit text archive mode to retain unsupported UTF-8 text') from exc
    # Iterator frames bound traversal memory even for broad shallow arrays.
    pending = [iter((value,))]
    count = 0
    while pending:
        try:
            node = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        count += 1
        if count > 300000:
            raise PackageError('JSON node count exceeds the import limit')
        if isinstance(node, str):
            try:
                node.encode('utf-8')
            except UnicodeEncodeError as exc:
                raise PackageError('Invalid Unicode in uploaded JSON') from exc
        elif isinstance(node, float) and not math.isfinite(node):
            raise PackageError('Nonfinite JSON number in uploaded data')
        elif isinstance(node, dict):
            pending.append(iter(node.keys()))
            pending.append(iter(node.values()))
        elif isinstance(node, list):
            pending.append(iter(node))
    return value


def _decode(data):
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PackageError('Import requires UTF-8 text') from exc


def _parse(data, filename):
    text = _decode(data).removeprefix('\ufeff')
    if filename.lower().endswith('.jsonl'):
        lines = [line for line in text.splitlines() if line.strip()]
        if not 1 <= len(lines) <= MAX_RECORDS:
            raise PackageError('JSONL record count exceeds the import limit')
        return [_load_json(line) for line in lines], True
    try:
        return _load_json(text), False
    except PackageError as exc:
        # Only a genuine multi-document stream is a JSONL candidate. Duplicate
        # keys, excessive depth and bad Unicode must never trigger a fallback.
        if not isinstance(exc.__cause__, json.JSONDecodeError) or exc.__cause__.msg != 'Extra data':
            raise
        lines = [line for line in text.splitlines() if line.strip()]
        if not 2 <= len(lines) <= MAX_RECORDS:
            raise
        return [_load_json(line) for line in lines], True


class _Report:
    def __init__(self):
        self.warnings = []
        self.partial = False

    def warn(self, message, *, partial=True):
        if message not in self.warnings:
            self.warnings.append(message)
        self.partial |= partial


def _unsupported(kind, report):
    kind = str(kind).lower()
    if kind in {'thinking', 'reasoning', 'reasoning_content', 'redacted_thinking', 'think'}:
        report.warn('Reasoning/thinking content was omitted; only visible conversation text is imported.')
    elif 'image' in kind or kind in {'audio', 'video', 'input_audio'}:
        report.warn('Image/audio/video content was omitted; media bytes are not imported.')
    elif 'tool' in kind or kind in {'function_call', 'function_result'}:
        report.warn('Tool calls and tool results were omitted; imported text cannot execute tools.')
    elif kind in {'document', 'file', 'attachment', 'attachments', 'files'}:
        report.warn('Attachments/files were omitted; only visible conversation text is imported.')
    else:
        report.warn('Unsupported content blocks were omitted; review the extracted text before saving.')


def _content(value, report):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        kind = value.get('content_type', value.get('type', ''))
        if kind == 'text' and 'parts' in value:
            return _content(value['parts'], report)
        if isinstance(kind, str) and kind in {'text', 'input_text', 'output_text'} and isinstance(value.get('text'), str):
            return value['text']
        _unsupported(kind, report)
        return ''
    if isinstance(value, list):
        return '\n'.join(part for block in value if (part := _content(block, report)))
    if value is not None:
        report.warn('Non-text message content was omitted.')
    return ''


def _native_message(message, report, *, role=None, content=None):
    if not isinstance(message, dict):
        raise PackageError('Every imported message must be an object')
    metadata = message.get('metadata', {})
    if not isinstance(metadata, dict):
        metadata = {}
    if (message.get('is_visually_hidden_from_conversation') is True
            or metadata.get('is_visually_hidden_from_conversation') is True):
        report.warn('Hidden conversation messages were omitted; private internal text is not imported.')
        return None
    if message.get('is_user_system_message') is True or metadata.get('is_user_system_message') is True:
        report.warn('User system-instruction messages were omitted.')
        return None
    for channel in (message.get('channel'), metadata.get('channel')):
        if channel not in (None, '', 'final', 'commentary'):
            report.warn('Analysis/reasoning or unsupported message channels were omitted; only visible conversation text is imported.')
            return None
    if message.get('recipient') not in (None, '', 'all'):
        report.warn('Tool-directed messages were omitted; imported text cannot execute tools.')
        return None
    if role is not None and 'role' in message and message['role'] != role:
        raise PackageError('Session event role conflicts with its message role')
    native_role = role if role is not None else message.get('role', message.get('sender'))
    if not isinstance(native_role, str):
        report.warn('Messages with unknown roles were omitted.')
        return None
    normalized_role = {'human': 'user', 'bot': 'assistant'}.get(native_role.lower(), native_role.lower())
    if normalized_role in {'system', 'developer'}:
        report.warn(f'{normalized_role.capitalize()} instruction messages were omitted; privileged instructions are not portable.')
        return None
    if normalized_role not in {'user', 'assistant'}:
        if normalized_role in {'tool', 'function'}:
            _unsupported('tool', report)
        else:
            report.warn('Messages with unknown roles were omitted.')
        return None
    for key in ('attachments', 'files', 'thinking', 'reasoning_content', 'tool_calls', 'function_call'):
        if message.get(key):
            _unsupported(key, report)
    if content is None:
        content = message.get('content', message.get('text'))
    text = _content(content, report)
    if not text:
        report.warn('Messages without readable text were omitted.')
        return None
    return {'role': normalized_role, 'content': text, 'provenance': 'imported', 'transformations': ['text_extracted']}


def _source(platform, adapter, report, *, claimed=False, kind='conversation_export'):
    value = {'platform': platform, 'adapter': adapter, 'kind': kind,
             'method': 'file_import', 'verification': 'user_claimed' if claimed else 'structure_detected',
             'completeness': 'partial_text' if report.partial else 'text_only'}
    if report.warnings:
        value['warnings'] = list(report.warnings)
    return value


def _resolve_platform(detected, claimed):
    if claimed != 'auto' and detected not in {'other', claimed}:
        raise PackageError(f'The selected platform conflicts with the detected {detected} format')
    return detected if claimed == 'auto' else claimed


def _conversation(title, messages, platform, adapter, report, *, native_id=None, claimed=False):
    if not isinstance(title, str):
        raise PackageError('Conversation title must be text')
    if not messages:
        raise PackageError('No readable conversation messages; choose explicit text archive mode to retain the original text')
    if len(messages) > MAX_MESSAGES:
        raise PackageError('Conversation exceeds the 2000-message import limit')
    source = _source(platform, adapter, report, claimed=claimed)
    item = {'title': title, 'messages': messages, 'source': source}
    if isinstance(native_id, str) and native_id:
        item['id'] = 'sha256:' + hashlib.sha256(native_id.encode()).hexdigest()
        source['conversation_id'] = native_id
    return item


def _conversation_list(value):
    if isinstance(value, list):
        result = value
    elif isinstance(value, dict) and isinstance(value.get('conversations'), list):
        result = value['conversations']
    elif isinstance(value, dict):
        result = [value]
    else:
        raise PackageError('Unrecognized conversation structure; choose explicit text archive mode')
    if not 1 <= len(result) <= MAX_CONVERSATIONS or not all(isinstance(v, dict) for v in result):
        raise PackageError('An import requires 1–200 conversation objects')
    return result


def _chatgpt_messages(conversation, report):
    mapping = conversation['mapping']
    if not isinstance(mapping, dict) or not mapping or len(mapping) > MAX_RECORDS:
        raise PackageError('ChatGPT mapping must be a bounded object')
    current = conversation.get('current_node')
    if not isinstance(current, str) or current not in mapping:
        raise PackageError('ChatGPT current_node must select an existing branch')
    chain, seen = [], set()
    while current is not None:
        if not isinstance(current, str) or current in seen or current not in mapping:
            raise PackageError('ChatGPT branch contains a cycle or missing parent')
        seen.add(current)
        node = mapping[current]
        if not isinstance(node, dict) or 'parent' not in node:
            raise PackageError('ChatGPT branch node is malformed')
        if node.get('message') is not None:
            message = node['message']
            if not isinstance(message, dict) or not isinstance(message.get('author'), dict):
                raise PackageError('ChatGPT message author is malformed')
            parsed = _native_message(message, report, role=message['author'].get('role'))
            if parsed:
                chain.append(parsed)
        current = node['parent']
    if len(seen) < len(mapping):
        report.warn('Only the ChatGPT current_node branch is imported; other branches were omitted.')
    return list(reversed(chain))


def _fragments(message, report):
    fragments = message.get('fragments')
    if not isinstance(fragments, list):
        raise PackageError('DeepSeek fragments must be an array')
    parts = []
    for fragment in fragments:
        if not isinstance(fragment, dict):
            raise PackageError('DeepSeek fragments must contain objects')
        kind = str(fragment.get('type', '')).lower()
        if kind in {'response', 'text'} and isinstance(fragment.get('content'), str):
            parts.append(fragment['content'])
        else:
            _unsupported(kind, report)
    return '\n'.join(parts)


def _native_conversations(value, claimed, report):
    result, formats = [], []
    for conversation in _conversation_list(value):
        if 'chat_messages' in conversation:
            detected, adapter, format_name = 'claude', 'claude-export', 'claude-json'
            raw_messages = conversation['chat_messages']
        elif 'mapping' in conversation:
            detected, adapter, format_name = 'chatgpt', 'chatgpt-export', 'chatgpt-json'
            raw_messages = None
        elif isinstance(conversation.get('messages'), list):
            branch_keys = {'current_node', 'current_message_id', 'parent', 'parent_id', 'parent_message_id', 'children', 'child_ids'}
            if (branch_keys.intersection(conversation)
                    or any(isinstance(m, dict) and branch_keys.intersection(m) for m in conversation['messages'])):
                raise PackageError('Generic messages contain branch metadata; choose explicit text archive mode instead of flattening branches')
            detected, adapter, format_name = 'other', 'generic-messages', 'messages-json'
            raw_messages = conversation['messages']
        else:
            raise PackageError('Unrecognized conversation format; choose explicit text archive mode')
        for key in ('attachments', 'files'):
            if conversation.get(key):
                _unsupported(key, report)
        platform = _resolve_platform(detected, claimed)
        if raw_messages is None:
            messages = _chatgpt_messages(conversation, report)
        else:
            if not isinstance(raw_messages, list) or len(raw_messages) > MAX_MESSAGES:
                raise PackageError('Conversation messages must be an array of at most 2000 entries')
            messages = []
            for message in raw_messages:
                if isinstance(message, dict) and 'fragments' in message:
                    report.warn('Fragment-based exports have no verified stable schema; review the extracted response text.', partial=False)
                    content = _fragments(message, report)
                elif (detected == 'claude' and isinstance(message, dict)
                      and message.get('content') in (None, []) and isinstance(message.get('text'), str)):
                    content = message['text']
                else:
                    content = None
                parsed = _native_message(message, report, content=content)
                if parsed:
                    messages.append(parsed)
        result.append(_conversation(conversation.get('title', conversation.get('name', 'Imported conversation')),
                                    messages, platform, adapter, report,
                                    native_id=conversation.get('uuid', conversation.get('id')),
                                    claimed=detected == 'other'))
        formats.append(format_name)
    return result, formats[0] if len(set(formats)) == 1 else 'mixed-json'


def _log_records(value, is_jsonl):
    records = value if isinstance(value, list) else [value]
    if not records or len(records) > MAX_RECORDS or not all(isinstance(r, dict) for r in records):
        return None, None
    if any(isinstance(r.get('type'), str) and r['type'] in {'user/message', 'assistant/message', 'session'} for r in records):
        return records, 'deepseek-harness-jsonl'
    if any(isinstance(r.get('type'), str) and r['type'] in {'user', 'assistant'} and isinstance(r.get('message'), dict)
           and ('sessionId' in r or is_jsonl) for r in records):
        return records, 'claude-code-jsonl'
    return None, None


def _from_log(records, format_name, claimed, report, filename):
    dsh = format_name == 'deepseek-harness-jsonl'
    platform = _resolve_platform('deepseek' if dsh else 'claude', claimed)
    adapter = 'deepseek-harness' if dsh else 'claude-code'
    report.warn('Session logs are imported as historical visible text in file order, not a reconstructed active branch or resumable native session.')
    messages, session_ids = [], set()
    header_seen = False
    title = _basename(filename)
    for record in records:
        kind = record.get('type')
        if not isinstance(kind, str):
            raise PackageError('Session event type must be text')
        if dsh:
            if kind == 'session':
                if header_seen:
                    raise PackageError('A DeepSeek Harness log must contain at most one session header')
                header_seen = True
                version = record.get('version')
                if type(version) is not int or not 2 <= version <= 4:
                    raise PackageError('Unsupported DeepSeek Harness session version; use explicit text archive mode')
                if isinstance(record.get('id'), str):
                    session_ids.add(record['id'])
                continue
            if type(record.get('seq')) is not int or not isinstance(kind, str) or not isinstance(record.get('data'), dict):
                raise PackageError('Unsupported DeepSeek Harness event framing; use explicit text archive mode')
            if kind == 'user/message':
                message = record['data']
                source = message.get('source', {})
                if not isinstance(source, dict) or source.get('kind') != 'user':
                    report.warn('Injected runtime/plugin context was omitted; only user-authored input is imported.')
                    continue
            elif kind == 'assistant/message':
                message = record['data'].get('message')
            else:
                if kind.startswith(('system/', 'developer/')):
                    report.warn('System/developer instruction events were omitted.')
                elif kind.startswith('tool/'):
                    _unsupported('tool', report)
                else:
                    report.warn('Runtime, progress, and other non-message events were omitted.')
                continue
            role = 'user' if kind == 'user/message' else 'assistant'
        else:
            if isinstance(record.get('sessionId'), str):
                session_ids.add(record['sessionId'])
            if kind not in {'user', 'assistant'}:
                report.warn('Runtime, progress, and other non-message events were omitted.')
                continue
            message, role = record.get('message'), kind
        parsed = _native_message(message, report, role=role)
        if parsed:
            messages.append(parsed)
    if len(session_ids) > 1:
        raise PackageError('A session log must not combine multiple session identities')
    return [_conversation(title, messages, platform, adapter, report,
                          native_id=next(iter(session_ids), None))]


def _export_zip(data, report):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= MAX_IMPORT_FILES:
                raise PackageError('ZIP entry count exceeds the import limit')
            names, total, payloads = set(), 0, []
            for info in infos:
                name = info.filename
                components = name.rstrip('/').split('/')
                if (name != info.orig_filename or not name or name.startswith('/') or '\\' in name
                        or any(p in {'', '.', '..'} or ':' in p for p in components)
                        or any(ord(c) < 32 or ord(c) == 127 for c in name)):
                    raise PackageError('Unsafe path in uploaded ZIP')
                if name in names:
                    raise PackageError('Duplicate path in uploaded ZIP')
                names.add(name)
                kind = stat.S_IFMT(info.external_attr >> 16)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not info.is_dir()):
                    raise PackageError('Only regular files/directories are permitted in uploaded ZIP')
                if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise PackageError('Encrypted or unsupported ZIP compression')
                total += info.file_size
                if not 0 <= info.file_size <= MAX_TOTAL_BYTES or total > MAX_TOTAL_BYTES:
                    raise PackageError('Expanded ZIP exceeds 100 MiB')
                if name == 'conversations.json' or _SESSION_LOG.fullmatch(name):
                    payloads.append(info)
            if len(payloads) != 1:
                raise PackageError('ZIP must contain exactly one root conversations.json or session[.vN].jsonl; extract a supported text file for archive mode')
            info = payloads[0]
            if info.file_size > MAX_IMPORT_BYTES:
                raise PackageError('Conversation JSON in ZIP exceeds 16 MiB')
            with archive.open(info) as stream:
                raw = stream.read(MAX_IMPORT_BYTES + 1)
                if len(raw) != info.file_size or len(raw) > MAX_IMPORT_BYTES or stream.read(1):
                    raise PackageError('ZIP entry exceeds its bounded declared size')
            if any(not i.is_dir() and i is not info for i in infos):
                report.warn('Other ZIP files, account data, attachments, and subagent logs were omitted.')
            return raw, info.filename
    except PackageError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError, NotImplementedError, zlib.error) as exc:
        raise PackageError('Invalid or corrupt uploaded ZIP') from exc


def _report(package, format_name, mode, details, *, original=False):
    from .platforms import package_platforms
    return {'format': format_name, 'mode': mode, 'platforms': package_platforms(package),
            'warnings': details.warnings, 'partial': details.partial,
            'requires_confirmation': bool(details.warnings), 'original_preserved': original}


def import_upload(data, *, filename='', platform='auto', mode='normalize'):
    """Return (validated contextpack bytes, decoded package, conversion report).

    Existing contextpacks retain their exact bytes and format limits. Native
    input is bounded to 16 MiB. Explicit archive mode preserves the original
    UTF-8 text (including BOM/newlines) inside one inert user message.
    """
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_ARCHIVE_BYTES:
        raise PackageError('Import requires nonempty bytes within the package size limit')
    if not isinstance(platform, str) or not isinstance(mode, str) or platform not in _PLATFORMS or mode not in {'normalize', 'archive'}:
        raise PackageError('Unsupported platform or import mode')
    filename = _basename(filename)
    data = bytes(data)
    details = _Report()
    is_zip = data.startswith(b'PK') or zipfile.is_zipfile(io.BytesIO(data))
    if mode == 'normalize' and (filename.lower().endswith('.contextpack') or is_zip):
        if filename.lower().endswith('.contextpack'):
            existing = True
        else:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    existing = 'manifest.json' in archive.namelist()
            except (zipfile.BadZipFile, OSError) as exc:
                raise PackageError('Invalid uploaded ZIP') from exc
        if existing:
            package = read_package(data)
            if platform != 'auto':
                from .platforms import package_platforms
                if any(value != platform for value in package_platforms(package)):
                    raise PackageError('Selected platform conflicts with the existing contextpack; its metadata is preserved')
            return data, package, _report(package, 'contextpack', mode, details, original=True)
    if len(data) > MAX_IMPORT_BYTES:
        raise PackageError('Native import exceeds 16 MiB')
    if mode == 'archive':
        extension = '.' + filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if is_zip or extension not in _TEXT_EXTENSIONS:
            raise PackageError('Text archive supports only UTF-8 .json, .jsonl, .txt, and .md; ZIP archives must be unpacked first')
        text = _decode(data)
        if '\x00' in text:
            raise PackageError('Binary/NUL content cannot be stored as a text archive')
        if len(data) > MAX_CONVERSATION_BYTES:
            raise PackageError('Text archive exceeds the 5 MiB conversation limit')
        details.warn('Original text is preserved without automatic redaction; it may contain secrets, account data, or private paths. Review it before sharing.', partial=False)
        selected = 'other' if platform == 'auto' else platform
        conversation = _conversation(filename, [{'role': 'user', 'content': text}], selected, 'text-archive', details, claimed=True)
        conversation['source'].update(kind='source_archive', title=filename, completeness='original_text')
        blob = build_package([conversation], filename, source=conversation['source'])
        package = read_package(blob)
        return blob, package, _report(package, 'text-archive', mode, details, original=True)
    if is_zip:
        data, filename = _export_zip(data, details)
    value, is_jsonl = _parse(data, filename)
    records, format_name = _log_records(value, is_jsonl)
    description = ''
    canonical = value if isinstance(value, list) else [value]
    canonical_list = (bool(canonical) and all(isinstance(c, dict) and c.get('schema_version') == '0.1' and 'messages' in c for c in canonical))
    if records is not None:
        conversations = _from_log(records, format_name, platform, details, filename)
        title, source = conversations[0]['title'], {}
    elif canonical_list:
        conversations = canonical
        title, source = (conversations[0].get('title', 'Imported conversation') if len(conversations) == 1 else 'Imported conversations'), {}
        format_name = 'contexthub-json'
    elif (isinstance(value, dict) and isinstance(value.get('library'), dict)
          and isinstance(value.get('conversations'), list)):
        conversations = value['conversations']
        if not conversations or not all(isinstance(c, dict) and c.get('schema_version') == '0.1' for c in conversations):
            raise PackageError('ContextHub JSON requires canonical 0.1 conversations')
        title, source = value['library'].get('title', 'Imported library'), value['library'].get('source', {})
        description = value['library'].get('description', '')
        format_name = 'contexthub-json'
    else:
        if is_jsonl:
            raise PackageError('Unrecognized JSONL session format; choose explicit text archive mode')
        conversations, format_name = _native_conversations(value, platform, details)
        title, source = (conversations[0]['title'] if len(conversations) == 1 else 'Imported conversations'), {}
    # Reports discovered in later messages/conversations apply to the whole
    # extraction. Never leave an earlier conversation claiming completeness.
    if format_name != 'contexthub-json':
        for conversation in conversations:
            conversation['source']['completeness'] = 'partial_text' if details.partial else 'text_only'
            if details.warnings:
                conversation['source']['warnings'] = list(details.warnings)
    blob = build_package(conversations, title, description, source=source)
    package = read_package(blob)
    if format_name == 'contexthub-json' and platform != 'auto':
        from .platforms import package_platforms
        if any(value != platform for value in package_platforms(package)):
            raise PackageError('Selected platform conflicts with normalized ContextHub metadata')
    return blob, package, _report(package, format_name, mode, details)
