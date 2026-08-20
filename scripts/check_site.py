#!/usr/bin/env python3
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import sys
root=Path(__file__).resolve().parents[1]
errors=[]
html_files=list(root.rglob('*.html'))
for f in html_files:
    soup=BeautifulSoup(f.read_text(encoding='utf-8'),'html.parser')
    if not soup.title or not soup.title.get_text(strip=True): errors.append(f'{f}: missing title')
    if not soup.find('meta',attrs={'name':'description'}): errors.append(f'{f}: missing description')
    for a in soup.find_all('a',href=True):
        href=a['href']
        if href.startswith(('http://','https://','mailto:','#')): continue
        path=href.split('#',1)[0].split('?',1)[0]
        if not path: continue
        target=(root/path.lstrip('/')) if path.startswith('/') else (f.parent/path)
        if target.is_dir(): target=target/'index.html'
        if not target.exists(): errors.append(f'{f}: broken internal link {href}')
    for img in soup.find_all('img',src=True):
        src=img['src']; target=root/src.lstrip('/') if src.startswith('/') else f.parent/src
        if not target.exists(): errors.append(f'{f}: missing image {src}')
        if not img.get('alt'): errors.append(f'{f}: image missing alt {src}')
# expected core files
for rel in ['index.html','articles/index.html','articles/building-secure-splunk-apps/index.html','articles/smartpath-dns/index.html','evidence/appinspect-ucc-compatibility/index.html','assets/site.css','assets/og-building-secure-splunk-apps.png','robots.txt','sitemap.xml','.nojekyll']:
    if not (root/rel).exists(): errors.append(f'missing {rel}')
if errors:
    print('\n'.join(errors)); sys.exit(1)
print(f'OK: {len(html_files)} HTML pages and core files validated')
