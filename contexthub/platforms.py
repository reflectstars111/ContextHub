"""Stable source categories derived from package claims, never authentication.

The categories describe the application that a conversation came from. A model
provider alone is not necessarily an application: ``openai`` does not distinguish
ChatGPT, Codex, or another client, and is deliberately left as ``other``.
"""

PLATFORMS = ('codex', 'chatgpt', 'claude', 'deepseek', 'other')

_ALIASES = {
    'codex': 'codex', 'codex-cli': 'codex', 'codex cli': 'codex',
    'openai codex': 'codex', 'openai-codex': 'codex',
    'chatgpt': 'chatgpt', 'chat-gpt': 'chatgpt', 'chat gpt': 'chatgpt',
    'openai chatgpt': 'chatgpt',
    'claude': 'claude', 'claude-code': 'claude', 'claude code': 'claude',
    'anthropic claude': 'claude',
    'deepseek': 'deepseek', 'deep-seek': 'deepseek', 'deep seek': 'deepseek',
    'deepseek-harness': 'deepseek', 'deepseek harness': 'deepseek', 'dsh': 'deepseek',
    'other': 'other',
}
_SOURCE_FIELDS = ('platform', 'adapter', 'provider', 'platform_claimed')


def normalize_platform(value):
    """Return one canonical category, accepting only explicit known aliases."""
    if not isinstance(value, str):
        return 'other'
    return _ALIASES.get(value.strip().casefold(), 'other')


def _source_platform(source):
    if not isinstance(source, dict):
        return 'other'
    for field in _SOURCE_FIELDS:
        value = source.get(field)
        if isinstance(value, str) and value.strip().casefold() in _ALIASES:
            return normalize_platform(value)
    return 'other'


def package_platforms(package):
    """Classify each conversation, retaining mixed and unknown source claims.

    Library metadata is a fallback only for conversations with no application
    source claim. It must not overwrite an unknown per-conversation provider.
    All source fields, including ``platform_claimed``, remain unverified labels.
    """
    library_source = package.get('library', {}).get('source', {})
    categories = set()
    for conversation in package.get('conversations', []):
        source = conversation.get('source', {})
        has_claim = isinstance(source, dict) and any(
            isinstance(source.get(field), str) and source[field].strip()
            for field in _SOURCE_FIELDS
        )
        categories.add(_source_platform(source if has_claim else library_source))
    if not categories:
        categories.add(_source_platform(library_source))
    return [platform for platform in PLATFORMS if platform in categories]
