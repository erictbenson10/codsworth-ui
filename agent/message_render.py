"""Readable terminal message blocks without terminal escape sequences or network I/O."""
import re
import unicodedata


def highlight_metrics(spans):
    """Accent numbers and issue/question IDs in supporting summaries only."""
    result = []
    pattern = r'(?<![\w.])(?:#[0-9]+|q[a-f0-9]{6,}|[0-9]+(?:[.,][0-9]+)*(?:[kKmM%hs])?)(?!\w)'
    for text,style in spans:
        if style != 'body':
            result.append((text,style))
            continue
        start = 0
        for match in re.finditer(pattern,text):
            if match.start() > start:
                result.append((text[start:match.start()],style))
            token = match.group()
            result.append((token,'inline_code' if token.startswith(('#','q')) else 'metric'))
            start = match.end()
        if start < len(text):
            result.append((text[start:],style))
    return result


def cell_width(character):
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1


def clean(text):
    return ''.join(c for c in str(text) if c in '\n\t' or (c.isprintable() and unicodedata.category(c) != 'Cs'))


def wrap_cells(text, width, continuation=''):
    """Wrap at word boundaries where possible, preserving every character."""
    width=max(1,width)
    remaining=clean(text).expandtabs(4)
    lines=[]
    while remaining:
        used=0
        stop=0
        for i,character in enumerate(remaining):
            cells=cell_width(character)
            if used+cells > width:
                break
            used+=cells
            stop=i+1
        if stop == len(remaining):
            lines.append(remaining)
            break
        if not stop:
            # A two-cell glyph cannot fit in a one-cell viewport.
            lines.append('?')
            remaining=remaining[1:]
            continue
        split=remaining.rfind(' ',0,stop+1)
        if split > len(continuation) and split >= stop//2:
            stop=split+1
        lines.append(remaining[:stop])
        remaining=continuation+remaining[stop:]
    return lines or ['']


def inline(text):
    text=re.sub(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)',r'\1 (\2)',text)
    text=re.sub(r'`([^`\n]+)`',r'\1',text)
    text=re.sub(r'\*\*([^*\n]+)\*\*',r'\1',text)
    return text


def inline_spans(text, role='body'):
    """Keep inline emphasis as attributes instead of discarding the markup."""
    text=re.sub(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)',r'\1 (\2)',text)
    spans=[]
    start=0
    for match in re.finditer(r'`([^`]+)`|\*\*([^*]+)\*\*|__([^_]+)__',text):
        if match.start()>start:
            spans.append((text[start:match.start()],role))
        spans.append((next(value for value in match.groups() if value is not None),
                      'inline_code' if match[1] is not None else 'strong'))
        start=match.end()
    if start<len(text):spans.append((text[start:],role))
    return spans or [('',role)]


def wrap_styled(text, width, role, continuation='', markup=True):
    spans=inline_spans(clean(text).expandtabs(4),role) if markup else [(clean(text).expandtabs(4),role)]
    plain=''.join(part for part,_ in spans)
    styles=[style for part,style in spans for _ in part]
    offset=0
    rows=[]
    for index,line in enumerate(wrap_cells(plain,width,continuation)):
        prefix=len(continuation) if index else 0
        consumed=len(line)-prefix
        row_styles=[role]*prefix+styles[offset:offset+consumed]
        parts=[]
        for char,style in zip(line,row_styles):
            if parts and parts[-1][1]==style:
                parts[-1]=(parts[-1][0]+char,style)
            else:parts.append((char,style))
        rows.append((line,role,parts))
        offset+=consumed
    return rows


def render_message(text, width, styled=False, code_indent=0):
    """Yield (text, role) lines; roles are body, heading, code, muted."""
    width=max(4,width)
    fenced=False
    fence_char=None
    output=[]
    for raw in clean(text).splitlines() or ['']:
        fence=re.match(r'^\s*(`{3,}|~{3,})(.*)$',raw)
        if fence and (not fenced or fence[1][0] == fence_char):
            if fenced:
                fenced=False
                output.append(('', 'muted', []))
            else:
                fenced=True
                fence_char=fence[1][0]
                language=fence[2].strip()
                output.extend(wrap_styled(language or 'Code',max(4,width-code_indent),'muted',markup=False))
            continue
        if fenced:
            output.extend(wrap_styled(raw,max(4,width-code_indent),'code',markup=False))
            continue
        heading=re.match(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*$',raw)
        if heading:
            output.extend(wrap_styled(heading[1],width,'heading'))
            continue
        text=raw
        bullet=re.match(r'^(\s*)(?:[-*+]\s+|\d+[.)]\s+)(.*)$',text)
        if bullet:
            prefix=text[:len(text)-len(bullet[2])]
            indent=' '*min(sum(cell_width(c) for c in prefix),width//3)
        else:
            indent=''
        output.extend(wrap_styled(text,width,'body',indent))
    rows=trim_blank_runs(output)
    return rows if styled else [(line,role) for line,role,_ in rows]


def trim_blank_runs(lines):
    """Drop leading and trailing blank rows, and collapse runs to one.

    A reply that begins '\n\n\nI must object' is common -- the model paces its
    own prose -- and rendered verbatim it puts three dead rows between the
    speaker label and the first thing they say. Vertical space is the scarcest
    thing on a 40-row window; one blank line is a paragraph break, three is a
    hole."""
    trimmed, blank = [], False
    for row in lines:
        line, role = row[:2]
        if line.strip() or role == 'code':
            trimmed.append(row)
            blank = False
        elif trimmed and not blank:
            trimmed.append(row)
            blank = True
    while trimmed and not trimmed[-1][0].strip() and trimmed[-1][1] != 'code':
        trimmed.pop()
    return trimmed
