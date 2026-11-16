#!/usr/bin/env python3
"""CONTESTO — raccoglie i feed RSS/Atom delle testate in fonti.json
e aggiorna data/news.json (archivio degli ultimi giorni) e data/stato.json.
Usa solo la libreria standard di Python: gira su GitHub Actions senza installare nulla."""
import json, os, re, html, time, gzip, zlib
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

GIORNI = 7            # quanti giorni tenere in archivio
MAX_PER_FONTE = 150   # articoli massimi per singolo feed in archivio
DESC_MAX = 180        # lunghezza massima del sommario
UA = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Mobile Safari/537.36 CONTESTO-RSS'

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')

def scarica(url, timeout=20):
    req = Request(url, headers={'User-Agent': UA, 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8', 'Accept-Encoding': 'gzip, deflate'})
    with urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = (r.headers.get('Content-Encoding') or '').lower()
    if enc == 'gzip' or raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    elif enc == 'deflate':
        raw = zlib.decompress(raw)
    return raw

def locale(tag):
    return tag.rsplit('}', 1)[-1].lower() if isinstance(tag, str) else ''

def testo(el):
    return ''.join(el.itertext()).strip() if el is not None else ''

def pulisci(s):
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))
    return re.sub(r'\s+', ' ', s).strip()

def data_ts(s):
    s = (s or '').strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace('Z', '+00:00'))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)

def figli(el, *nomi):
    return [c for c in el if locale(c.tag) in nomi]

def primo(el, *nomi):
    for c in el:
        if locale(c.tag) in nomi:
            return c
    return None

def leggi_feed(raw):
    """Restituisce lista di dict {title, url, t, desc} da RSS 2.0, RDF/RSS 1.0 o Atom."""
    raw = re.sub(rb'^[^<]+', b'', raw, count=1)  # BOM o spazi prima dell'XML
    root = ET.fromstring(raw)
    voci = [e for e in root.iter() if locale(e.tag) in ('item', 'entry')]
    out = []
    for it in voci:
        titolo = pulisci(testo(primo(it, 'title')))
        link = ''
        for l in figli(it, 'link'):
            href = l.get('href')
            if href and l.get('rel', 'alternate') == 'alternate':
                link = href; break
            if not href and testo(l):
                link = testo(l); break
        if not link:
            g = primo(it, 'guid')
            if g is not None and testo(g).startswith('http'):
                link = testo(g)
        quando = None
        for nome in ('pubdate', 'published', 'updated', 'date', 'issued'):
            quando = data_ts(testo(primo(it, nome)))
            if quando:
                break
        desc = pulisci(testo(primo(it, 'description', 'summary', 'encoded', 'content')))
        if len(desc) > DESC_MAX:
            desc = desc[:DESC_MAX].rsplit(' ', 1)[0] + '…'
        if titolo and link.startswith('http'):
            out.append({'title': titolo, 'url': link.strip(), 't': quando, 'desc': desc})
    return out

def dominio(url):
    d = urlparse(url).netloc.lower()
    return d[4:] if d.startswith('www.') else d

def lavora(fonte):
    try:
        voci = leggi_feed(scarica(fonte['url']))
        return fonte, voci, None
    except Exception as e:
        return fonte, [], f'{type(e).__name__}: {str(e)[:120]}'

def main():
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(BASE, 'fonti.json'), encoding='utf-8') as f:
        fonti = json.load(f)['fonti']

    archivio = []
    p = os.path.join(DATA, 'news.json')
    if os.path.exists(p):
        try:
            with open(p, encoding='utf-8') as f:
                archivio = json.load(f).get('a', [])
        except Exception:
            archivio = []

    ora = int(time.time() * 1000)
    limite = ora - GIORNI * 86400000
    visti = {a['u'] for a in archivio}
    stato = []

    with ThreadPoolExecutor(max_workers=12) as ex:
        risultati = list(ex.map(lavora, fonti))

    for fonte, voci, errore in risultati:
        nuovi = 0
        for v in voci:
            if v['url'] in visti:
                continue
            t = v['t'] or ora               # senza data: momento della lettura
            if t > ora + 3600000:           # date nel futuro: correggi
                t = ora
            if t < limite:
                continue
            visti.add(v['url'])
            archivio.append({
                'u': v['url'], 'h': v['title'], 'd': v['desc'], 't': t,
                's': fonte['nome'], 'm': dominio(v['url']) or dominio(fonte['url']),
                'g': fonte['gruppo'], 'c': fonte.get('paese', ''), 'l': fonte.get('lingua', '')
            })
            nuovi += 1
        stato.append({'nome': fonte['nome'], 'url': fonte['url'], 'ok': errore is None and len(voci) > 0,
                      'letti': len(voci), 'nuovi': nuovi, 'errore': errore or ('feed vuoto' if not voci else None)})

    # pulizia: tieni solo gli ultimi GIORNI e al massimo MAX_PER_FONTE per testata
    archivio = [a for a in archivio if a['t'] >= limite]
    archivio.sort(key=lambda a: a['t'], reverse=True)
    conta, finale = {}, []
    for a in archivio:
        k = a['s']
        conta[k] = conta.get(k, 0) + 1
        if conta[k] <= MAX_PER_FONTE:
            finale.append(a)

    with open(p, 'w', encoding='utf-8') as f:
        json.dump({'aggiornato': ora, 'giorni': GIORNI, 'a': finale}, f, ensure_ascii=False, separators=(',', ':'))
    with open(os.path.join(DATA, 'stato.json'), 'w', encoding='utf-8') as f:
        json.dump({'aggiornato': ora, 'fonti': stato}, f, ensure_ascii=False, indent=1)

    ok = sum(1 for s in stato if s['ok'])
    print(f'Fonti funzionanti: {ok}/{len(stato)} — articoli in archivio: {len(finale)}')
    for s in stato:
        if not s['ok']:
            print(f"  ✗ {s['nome']}: {s['errore']}")

if __name__ == '__main__':
    main()
