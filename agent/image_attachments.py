"""Local image drafts and bounded inline transport; paths are never shell commands."""
import base64
import binascii
from functools import lru_cache
import os
from pathlib import Path
import re
import shlex
import stat
import struct
from urllib.parse import unquote, urlsplit

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
# Base64 plus prompt/context must fit Hermes's 10 MB request limit.
MAX_TOTAL_BYTES = 6 * 1024 * 1024
MAX_ENCODED_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic', '.heif', '.avif', '.bmp', '.tif', '.tiff', '.svg'}
TOKEN = re.compile(r'''(?<!\S)(?=['"]?(?:/|~/|\./|\.\./|file://))(?:[^\s'"\\]|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+''')


def local_path(value):
    if value.startswith('file://'):
        parsed = urlsplit(value)
        if parsed.netloc not in ('', 'localhost') or parsed.query or parsed.fragment:
            return None
        value = unquote(parsed.path)
    if not value.startswith(('/', '~/', './', '../')) or any(ord(c) < 32 for c in value):
        return None
    path = Path(value).expanduser()
    return path if path.suffix.lower() in IMAGE_SUFFIXES else None


@lru_cache(maxsize=16)
def image_references(draft):
    """Return (start, end, path) for explicit image paths, without touching disk."""
    references = []
    for match in TOKEN.finditer(draft):
        try:
            parts = shlex.split(match.group())
            path = local_path(parts[0]) if len(parts) == 1 else None
        except ValueError:
            continue
        if path is not None:
            references.append((match.start(), match.end(), path))
    return tuple(references)


def normalize_drop(text):
    """Quote raw single Finder paths as well as shell-escaped multi-file drops."""
    stripped = text.strip()
    path = local_path(stripped)
    if path is not None:
        try:
            if path.is_file():
                return shlex.quote(str(path))
        except OSError:
            pass  # Keep the draft editable; sending will report the read error.
    references = image_references(text)
    remainder = text
    for start, end, _ in reversed(references):
        remainder = remainder[:start] + remainder[end:]
    if references and not remainder.strip():
        return ' '.join(shlex.quote(str(path)) for _, _, path in references)
    return text


def image_mime(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    raise ValueError('Use a PNG, JPEG, GIF or WebP image.')


def decode_image(data, mime_type):
    if len(data) > MAX_ENCODED_BYTES:
        raise ValueError('Each image must be 4 MiB or smaller.')
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('Invalid image encoding.') from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('Each image must be 4 MiB or smaller.')
    if image_mime(raw) != mime_type:
        raise ValueError('Image content does not match its media type.')
    return raw


def _open_regular(path):
    """Nonblocking open that rejects special files before a read can hang."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    handle = os.fdopen(descriptor, 'rb')
    try:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Choose a regular image file.')
        return handle, info
    except Exception:
        handle.close()
        raise


def read_image(path):
    try:
        handle, info = _open_regular(path)
    except OSError:
        raise ValueError(f'Cannot read image {path.name}. Check that it still exists and is readable.') from None
    with handle:
        if info.st_size > MAX_IMAGE_BYTES:
            raise ValueError('Each image must be 4 MiB or smaller.')
        data = handle.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Each image must be 4 MiB or smaller.')
    return dict(name=path.name, mime_type=image_mime(data), data=base64.b64encode(data).decode('ascii')), len(data)


def is_command(draft):
    """Absolute dropped paths start with / too; they are messages, not commands."""
    return draft.lstrip().startswith('/') and not any(start == 0 for start, _, _ in image_references(draft.lstrip()))


def resolved_references(draft, cwd):
    """Image paths from a draft, resolved against cwd, without reading bytes."""
    resolved = []
    for start, end, path in image_references(draft):
        resolved.append((start, end, path if path.is_absolute() else Path(cwd) / path))
    return tuple(resolved)


def attachment_hint(draft):
    count = len(image_references(draft))
    if not count:
        return ''
    noun = 'image' if count == 1 else 'images'
    return f'{count} {noun} · Enter send · delete path to detach'


def _jpeg_size(data):
    index = 2
    while index + 9 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return struct.unpack('>HH', data[index + 5:index + 9])[::-1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 4 > len(data):
            break
        length = struct.unpack('>H', data[index + 2:index + 4])[0]
        if length < 2:
            break
        index += 2 + length
    return None


def _webp_size(data):
    kind = data[12:16]
    if kind == b'VP8X' and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], 'little')
        height = 1 + int.from_bytes(data[27:30], 'little')
        return width, height
    if kind == b'VP8 ' and len(data) >= 30:
        return (data[26] | ((data[27] & 0x3F) << 8), data[28] | ((data[29] & 0x3F) << 8))
    if kind == b'VP8L' and len(data) >= 25:
        bits = int.from_bytes(data[21:25], 'little')
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def image_size(data):
    """Pixel size from a bounded header read; None when the format is not parsed."""
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        return struct.unpack('>II', data[16:24])
    if data.startswith((b'GIF87a', b'GIF89a')) and len(data) >= 10:
        return struct.unpack('<HH', data[6:10])
    if data.startswith(b'\xff\xd8\xff'):
        return _jpeg_size(data)
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return _webp_size(data)
    return None


def format_bytes(size):
    if size < 1024:
        return f'{size}B'
    if size < 1024 * 1024:
        value = size / 1024
        return f'{value:.0f}k' if value >= 10 else f'{value:.1f}k'
    value = size / (1024 * 1024)
    return f'{value:.0f}M' if value >= 10 else f'{value:.1f}M'


def preview_card(path):
    """Composer card for a draft image. Reads a header only; never raises."""
    empty = dict(name=path.name, path=path, width=None, height=None, size=None,
                 mime=None, error=None)
    try:
        info = path.stat()
    except OSError:
        return dict(empty, error='missing')
    if not stat.S_ISREG(info.st_mode):
        return dict(empty, error='not a file')
    return dict(_preview_cached(str(path), info.st_mtime_ns, info.st_size), path=path, name=path.name)


@lru_cache(maxsize=16)
def _preview_cached(path_str, _mtime_ns, size):
    card = dict(width=None, height=None, size=size, mime=None, error=None)
    try:
        handle, _ = _open_regular(Path(path_str))
        with handle:
            data = handle.read(65536)
    except (OSError, ValueError):
        return dict(card, size=None, error='missing')
    try:
        card['mime'] = image_mime(data[:16] if len(data) > 16 else data)
    except ValueError:
        return dict(card, error='not an image')
    pixels = image_size(data)
    if pixels:
        card['width'], card['height'] = pixels
    return card


def preview_label(card):
    """One-line caption used next to a thumbnail or on its own."""
    if card.get('error'):
        return f'{card["name"]} · {card["error"]}'
    parts = [card['name']]
    if card.get('width') and card.get('height'):
        parts.append(f'{card["width"]}×{card["height"]}')
    if card.get('mime'):
        parts.append(card['mime'].split('/', 1)[-1].upper())
    if card.get('size'):
        parts.append(format_bytes(card['size']))
    return ' · '.join(parts)


def prepare_message(draft, cwd):
    references = image_references(draft)
    if len(references) > MAX_IMAGES:
        raise ValueError('Attach up to 4 images per message.')
    images, total, chunks, position = [], 0, [], 0
    for start, end, path in references:
        image, size = read_image(path if path.is_absolute() else Path(cwd) / path)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError('Images must total 6 MiB or less per message.')
        images.append(image)
        chunks.append(draft[position:start])
        position = end
    chunks.append(draft[position:])
    text = ''.join(chunks).strip()
    if images:
        text = text or 'Describe the attached images.'
    return text, images
