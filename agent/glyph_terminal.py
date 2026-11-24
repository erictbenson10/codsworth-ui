"""Render the existing Codsworth PNG animation in terminals; no ASCII redesign.

Kitty/Ghostty use full RGBA PNG frames. Other terminals get xterm-256 half pixels.
Runtime is standard-library-only; the PNG reader supports the repository's RGBA8 assets.
"""
import base64
from functools import lru_cache
import json
import os
from pathlib import Path
import struct
import zlib

PREVIEW_IMAGE_IDS = (32, 33, 34, 35)

ROOT = Path(__file__).resolve().parents[1] / 'glyphs/animations'


def _paeth(a, b, c):
    p = a+b-c
    pa, pb, pc = abs(p-a), abs(p-b), abs(p-c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


@lru_cache(maxsize=8)
def _decode(path):
    raw = Path(path).read_bytes()
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Not a PNG')
    offset, chunks = 8, []
    width = height = None
    while offset < len(raw):
        size = struct.unpack('>I', raw[offset:offset+4])[0]
        kind = raw[offset+4:offset+8]
        data = raw[offset+8:offset+8+size]
        if kind == b'IHDR':
            width, height, depth, color, comp, filt, interlace = struct.unpack('>IIBBBBB', data)
            if (depth, color, comp, filt, interlace) != (8, 6, 0, 0, 0):
                raise ValueError('Glyph assets must be noninterlaced RGBA8 PNG')
        elif kind == b'IDAT':
            chunks.append(data)
        elif kind == b'IEND':
            break
        offset += size+12
    if width is None or not 0 < width*height <= 10_000_000:
        raise ValueError('Invalid glyph dimensions')
    data = zlib.decompress(b''.join(chunks))
    stride = width*4
    if len(data) != height*(stride+1):
        raise ValueError('Invalid glyph pixel payload')
    rows, previous = [], bytearray(stride)
    for y in range(height):
        start = y*(stride+1)
        kind = data[start]
        row = bytearray(data[start+1:start+stride+1])
        if kind not in range(5):
            raise ValueError('Invalid PNG filter')
        if kind:
            for i in range(stride):
                a, b, c = (row[i-4] if i >= 4 else 0), previous[i], (previous[i-4] if i >= 4 else 0)
                predictor = a if kind == 1 else b if kind == 2 else (a+b)//2 if kind == 3 else _paeth(a,b,c)
                row[i] = (row[i]+predictor)&255
        rows.append(bytes(row))
        previous = row
    return width, height, rows


@lru_cache(maxsize=1)
def _manifest():
    return json.loads((ROOT/'manifest.json').read_text())


def _selection(expression, activity, frame_index):
    manifest = _manifest()
    if expression not in manifest['expressions']:
        expression = 'neutral'
    if activity not in manifest['activities']:
        activity = 'idle'
    clip = manifest['clips'][f'{expression}-{activity}']
    return ROOT/clip['src'], manifest['width'], manifest['height'], int(frame_index)%clip['frames']


@lru_cache(maxsize=256)
def frame_rgba(expression='neutral', activity='idle', frame_index=0):
    path, width, height, index = _selection(expression,activity,frame_index)
    sheet_width, sheet_height, rows = _decode(path)
    if sheet_height != height or sheet_width < (index+1)*width:
        raise ValueError('Animation dimensions mismatch')
    return width, height, tuple(row[index*width*4:(index+1)*width*4] for row in rows)


def _chunk(kind, data):
    return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)


@lru_cache(maxsize=256)
def frame_png(expression='neutral', activity='idle', frame_index=0):
    width,height,rows = frame_rgba(expression,activity,frame_index)
    return b'\x89PNG\r\n\x1a\n'+_chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6,0,0,0))+_chunk(b'IDAT',zlib.compress(b''.join(b'\0'+r for r in rows)))+_chunk(b'IEND',b'')


def fit_cells(max_columns, max_rows):
    """96:112 sprite; normal terminal cells are approximately twice as tall as wide."""
    columns = max(1,min(int(max_columns),int(max_rows)*2*96//112))
    return columns,max(1,min(int(max_rows),round(columns*112/96/2)))


def kitty_frame(expression='neutral', activity='idle', frame_index=0, columns=40, rows=24, image_id=31):
    columns,rows = fit_cells(columns,rows)
    data = base64.b64encode(frame_png(expression,activity,frame_index)).decode('ascii')
    pieces = [data[i:i+4096] for i in range(0,len(data),4096)]
    commands=[]
    for i,piece in enumerate(pieces):
        options = f'a=T,f=100,t=d,i={int(image_id)},p=1,c={columns},r={rows},C=1,q=2,' if i == 0 else ''
        commands.append('\x1b_G'+options+f'm={int(i<len(pieces)-1)};'+piece+'\x1b\\')
    return ''.join(commands)


def kitty_clear(image_id=31):
    return f'\x1b_Ga=d,d=I,i={int(image_id)},q=2\x1b\\'


def kitty_at(row, column, payload):
    return f'\x1b7\x1b[{int(row)+1};{int(column)+1}H{payload}\x1b8'


def kitty_file(path, columns=12, rows=6, image_id=32):
    """Place a local image with the Kitty graphics protocol; the terminal scales it."""
    payload = base64.b64encode(os.fsencode(str(path))).decode('ascii')
    return (f'\x1b_Ga=T,t=f,i={int(image_id)},c={max(1,int(columns))},'
            f'r={max(1,int(rows))},C=1,q=2;{payload}\x1b\\')


def kitty_clear_previews():
    return ''.join(kitty_clear(image_id) for image_id in PREVIEW_IMAGE_IDS)


@lru_cache(maxsize=4096)
def _color(rgb):
    levels=(0,95,135,175,215,255)
    cube=tuple(min(range(6),key=lambda i:abs(levels[i]-v)) for v in rgb)
    gray=max(0,min(23,round((sum(rgb)/3-8)/10)))
    cube_error=sum((rgb[i]-levels[cube[i]])**2 for i in range(3))
    gray_error=sum((v-(8+gray*10))**2 for v in rgb)
    return 232+gray if gray_error < cube_error else 16+36*cube[0]+6*cube[1]+cube[2]


@lru_cache(maxsize=256)
def render_cells(expression='neutral',activity='idle',frame_index=0,width=40,max_rows=24,background=(16,18,21)):
    columns,rows=fit_cells(width,max_rows)
    sw,sh,pixels=frame_rgba(expression,activity,frame_index)
    def color(x,y):
        sx=min(sw-1,int((x+.5)*sw/columns)); sy=min(sh-1,int((y+.5)*sh/(rows*2)))
        rgba=pixels[sy][sx*4:sx*4+4];alpha=rgba[3]
        return _color(tuple((rgba[i]*alpha+background[i]*(255-alpha))//255 for i in range(3)))
    return tuple(tuple(('▀',color(x,y*2),color(x,y*2+1)) for x in range(columns)) for y in range(rows))
