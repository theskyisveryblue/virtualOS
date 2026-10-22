"""Text admission for old and updated guest keyboard implementations."""
LEGACY = frozenset("abcdefghijklmnopqrstuvwxyz0123456789 =-[]';\\,./`")


def needs_ascii_keyboard(text):
    if not isinstance(text, str) or len(text) > 512:
        raise ValueError('guest text exceeds supported bound')
    if any(ord(ch) not in range(32, 127) and ch not in '\n\r\t' for ch in text):
        raise ValueError('guest keyboard supports ASCII text only')
    return any(ch not in LEGACY for ch in text)
