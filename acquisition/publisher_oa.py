"""Extract public PDF links from an article already evidenced as open access."""
from html.parser import HTMLParser
from urllib.parse import urljoin,urlparse
import re,html

class Links(HTMLParser):
    def __init__(self):super().__init__();self.urls=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta' and (a.get('name') or a.get('property') or '').lower() in ['citation_pdf_url','wkhealth_pdf_url','dc.identifier.pdf','eprints.document_url'] and a.get('content'):self.urls.append(a['content'])
        if tag=='a' and a.get('href') and re.search(r'\.pdf(?:[?#]|$)|/article/(?:download|pdf)/',a['href'],re.I):self.urls.append(a['href'])
def pdf_links(text,base):
    parser=Links();parser.feed(text);urls=[]
    for link in parser.urls:
        url=urljoin(base,html.unescape(link));host=urlparse(url).hostname or ''
        if urlparse(url).scheme in ['http','https'] and not any(word in host.lower() for word in ['sci-hub','scihub','libgen','annas-archive','z-lib','localhost']):urls.append(url)
    return list(dict.fromkeys(urls))[:4]
