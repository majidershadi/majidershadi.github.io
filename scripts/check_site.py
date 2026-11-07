#!/usr/bin/env python3
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import sys
import json
import xml.etree.ElementTree as ET
root=Path(__file__).resolve().parents[1]
errors=[]
base='https://majidershadi.github.io'
canonicals=set()
titles=set()
descriptions=set()
html_files=list(root.rglob('*.html'))
for f in html_files:
    soup=BeautifulSoup(f.read_text(encoding='utf-8'),'html.parser')
    if not soup.title or not soup.title.get_text(strip=True): errors.append(f'{f}: missing title')
    if not soup.find('meta',attrs={'name':'description'}): errors.append(f'{f}: missing description')
    canonical=soup.find_all('link',rel='canonical')
    rel=f.relative_to(root).as_posix()
    expected=base+('/' if rel=='index.html' else '/'+rel.removesuffix('index.html'))
    if len(canonical)!=1 or canonical[0].get('href')!=expected:
        errors.append(f'{f}: expected one canonical URL {expected}')
    else:
        canonicals.add(expected)
    if len(soup.find_all('h1'))!=1: errors.append(f'{f}: expected one main heading')
    for value,seen,label in [(soup.title.get_text(strip=True) if soup.title else '',titles,'title'),
                             ((soup.find('meta',attrs={'name':'description'}) or {}).get('content',''),descriptions,'description')]:
        if not value.strip() or value in seen: errors.append(f'{f}: empty or duplicate {label}')
        seen.add(value)
    schemas=[]
    for script in soup.find_all('script',type='application/ld+json'):
        try:
            data=json.loads(script.string or script.get_text())
            schemas.extend(data.get('@graph',[data]))
        except (ValueError,AttributeError): errors.append(f'{f}: invalid JSON-LD')
    if rel.startswith('articles/') and rel!='articles/index.html':
        articles=[x for x in schemas if x.get('@type') in ['Article','TechArticle','BlogPosting']]
        breadcrumbs=[x for x in schemas if x.get('@type')=='BreadcrumbList']
        if len(articles)!=1: errors.append(f'{f}: expected one article schema')
        if len(breadcrumbs)!=1: errors.append(f'{f}: expected one breadcrumb schema')
        elif breadcrumbs[0]['itemListElement'][-1].get('item')!=expected:
            errors.append(f'{f}: breadcrumb must end at the canonical URL')
        if not soup.find('nav',attrs={'aria-label':'Breadcrumb'}): errors.append(f'{f}: missing visible breadcrumb')
        for article in articles:
            if article.get('mainEntityOfPage')!=expected or not article.get('author',{}).get('url'):
                errors.append(f'{f}: article identity or author URL is missing')
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
try:
    sitemap=ET.parse(root/'sitemap.xml')
    urls=[node.text for node in sitemap.findall('.//{*}loc')]
    expected_urls=canonicals-{base+'/404.html'}
    if len(urls)!=len(set(urls)) or set(urls)!=expected_urls:
        errors.append(f'sitemap URLs differ from indexable canonicals: {set(urls)^expected_urls}')
    feed=ET.parse(root/'articles/feed.xml')
    for link in feed.findall('./channel/item/link'):
        if link.text not in expected_urls: errors.append(f'RSS link is not a canonical page: {link.text}')
except ET.ParseError as exc:
    errors.append(f'invalid sitemap or RSS XML: {exc}')
if errors:
    print('\n'.join(errors)); sys.exit(1)
print(f'OK: {len(html_files)} HTML pages and core files validated')
