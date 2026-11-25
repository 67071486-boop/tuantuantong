from .config import ServiceError

ID_START = 1_000_000_000
ID_END = 2_000_000_000
DEFAULT_PREFERENCE = {'pinned': False, 'remark': '', 'marked_unread': False}


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ServiceError('Invalid core fields')
    return value


def text(value, maximum, name, blank=True):
    if not isinstance(value, str) or len(value) > maximum or (not blank and not value) or '\x00' in value:
        raise ServiceError('Invalid ' + name)
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ServiceError('Invalid ' + name) from None
    return value
