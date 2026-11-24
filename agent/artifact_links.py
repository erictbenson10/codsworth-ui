"""Bounded extraction of explicit web links; no fetching or automatic opening."""
import re
import unicodedata
from urllib.parse import urlsplit


def valid_url(value):
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<33 or unicodedata.category(c).startswith('C') or c == '\\' for c in value):
        return False
    try:
        parsed=urlsplit(value)
        return parsed.scheme in ('https','http') and bool(parsed.hostname) and parsed.username is None and parsed.password is None and parsed.port != 0
    except ValueError:
        return False


def extract_links(transcript, limit=50):
    links=[]
    seen=set()
    for _,text in transcript[-100:]:
        for match in re.finditer(r'https?://[^\s<>"\x00-\x1f]+',str(text)[:30000]):
            url=match[0].rstrip('.,;:!?\'')
            # Closing prose/Markdown delimiters are not part of the URL unless balanced.
            for opening,closing in [('(',')'),('[',']'),('{','}')]:
                while url.endswith(closing) and url.count(closing)>url.count(opening):
                    url=url[:-1]
            if valid_url(url) and url not in seen:
                seen.add(url);links.append(url)
                if len(links)>=min(50,max(1,limit)):
                    return links
    return links
