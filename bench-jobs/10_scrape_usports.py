"""
Read box scores from the official U SPORTS site (en.usports.ca) and write rows in the same format as 01_scrape_usportshoops.py,
so 07_grade.py works on the output unchanged.

  python 10_scrape_usports.py files  --out data/usports_2026-27.csv  page1.html page2.html ...   (box score pages you saved)
  python 10_scrape_usports.py season --out data/usports_2026-27.csv  --schedule schedule.html [--since 2026-11-01]
        (reads every box score linked from a saved schedule page, fetching each one over the web)

Only FINAL games are kept. A game still marked live is skipped. Every game is checked: player points must equal the team score.
"""
import argparse, os, re, sys, time, importlib.util
import pandas as pd, requests
from bs4 import BeautifulSoup

_here = os.path.dirname(os.path.abspath(__file__))
_s = importlib.util.spec_from_file_location('sc', os.path.join(_here, '01_scrape_usportshoops.py'))
sc = importlib.util.module_from_spec(_s); _s.loader.exec_module(sc)       # reuse the team-name list
BASE = 'https://en.usports.ca'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; TheBench/1.0; +mailto:' + os.environ.get('BENCH_CONTACT', 'not provided') + ')', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'Accept-Language': 'en-CA,en;q=0.9'}
SEASON = '2026-27'

def _ma(s):
    m = re.match(r'\s*(\d+)\s*-\s*(\d+)', s or ''); return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
def _i(s):
    s = (s or '').strip(); return int(s) if re.fullmatch(r'-?\d+', s) else 0
def _pct(a, b): return round(a / b, 4) if b else 0.0
def _dt(txt):
    m = re.search(r'([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})', txt); return pd.to_datetime(f'{m.group(1)} {m.group(2)} {m.group(3)}').strftime('%Y-%m-%d')

def parse_boxscore(html, gid, final_only=True):
    soup = BeautifulSoup(html, 'lxml'); T = soup.find_all('table')
    head = [[c.get_text(' ', strip=True) for c in r.find_all(['th', 'td'])] for r in T[0].find_all('tr')]
    status = head[0][0]
    if final_only and status.lower() != 'final': raise ValueError(f'{gid}: not final (status "{status}")')
    away_n, home_n = head[1][0], head[2][0]; away_pts, home_pts = int(head[1][-1]), int(head[2][-1])
    date = location = None
    for t in T:
        for r in t.find_all('tr'):
            c = [x.get_text(' ', strip=True) for x in r.find_all(['th', 'td'])]
            if c and c[0].lower().startswith('date'): date = _dt(' '.join(c))
            if c and c[0].lower().startswith('location'): location = c[1]
    if not date: raise ValueError(f'{gid}: no date found')
    teams = {}
    for t in T[1:3]:
        cap = t.find('caption'); name = cap.get_text(' ', strip=True) if cap else None
        rows = []
        for r in t.find_all('tr'):
            c = [x.get_text(' ', strip=True) for x in r.find_all(['th', 'td'])]
            m = re.match(r'^(\d+)\s+(.+)$', c[0]) if c else None
            if m and len(c) >= 14 and not c[0].startswith('TM '): rows.append((m.group(1), m.group(2), c))
        teams[name] = rows
    full = {n: sc.resolve(n) for n in (away_n, home_n)}
    out = []
    for n, opp, ha, tp, op in ((away_n, home_n, 'Away', away_pts, home_pts), (home_n, away_n, 'Home', home_pts, away_pts)):
        rows = teams[n]
        if final_only and sum(int(r[2][-1] or 0) for r in rows) != tp: raise ValueError(f'{gid}: {n} player points do not add up to {tp}')
        for k, (jer, name, c) in enumerate(rows):
            fgm, fga = _ma(c[2]); tpm, tpa = _ma(c[3]); ftm, fta = _ma(c[4]); pts = _i(c[13])
            out.append({'Season': SEASON, 'Date': date, 'Location': location, 'Team': full[n] or n, 'Opponent': full[opp] or opp, 'Abbr': '',
              'Jersey': float(jer), 'PlayerName': name, 'StarterFlag': k < 5, 'Mins': _i(c[1]), '3PT_Pct': _pct(tpm, tpa), 'FG_Pct': _pct(fgm, fga),
              'FT_Pct': _pct(ftm, fta), 'Reb_O': _i(c[5]), 'Reb_D': _i(c[6]), 'Reb_T': _i(c[7]), 'PF': _i(c[12]), 'AST': _i(c[8]), 'TO': _i(c[11]),
              'BLK': _i(c[10]), 'STL': _i(c[9]), 'Pts': pts, '3PTM': tpm, '3PTA': tpa, 'FGM': fgm, 'FGA': fga, 'FTM': ftm, 'FTA': fta,
              'TS_Pct': _pct(pts, 2 * (fga + .44 * fta)), 'eFG_Pct': _pct(fgm + .5 * tpm, fga), 'HomeAway': ha, 'TeamPoints': tp, 'OppPoints': op,
              'GameId': gid, 'Status': status, 'GameType': 'Regular season league', 'IsUSports': bool(full[n] and full[opp])})
    return out

def schedule_links(html):
    return list(dict.fromkeys(re.findall(r'/sports/mbkb/' + SEASON + r'/boxscores/(\d{8}_\w+)\.xml', html)))

def main(a):
    rows, bad = [], []
    if a.mode == 'files':
        for f in a.pages:
            gid = os.path.basename(f)
            try: rows += parse_boxscore(open(f, encoding='utf-8', errors='ignore').read(), gid)
            except Exception as e: bad.append((gid, str(e))); print('  SKIPPED', gid, e)
    else:
        ids = [g for g in schedule_links(open(a.schedule, encoding='utf-8', errors='ignore').read()) if not a.since or pd.to_datetime(g[:8]) >= pd.to_datetime(a.since)]
        print(len(ids), 'box scores to check')
        for g in ids:
            try:
                r = requests.get(f'{BASE}/sports/mbkb/{SEASON}/boxscores/{g}.xml', headers=HEADERS, timeout=30); r.raise_for_status()
                rows += parse_boxscore(r.text, g)
            except Exception as e: bad.append((g, str(e)[:300])); print('  skipped', g, str(e)[:160])
            time.sleep(2)
    if not rows: print('No final games could be read.', bad[:3]); sys.exit(1)
    df = pd.DataFrame(rows); os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True); df.to_csv(a.out, index=False)
    print(f'Saved {len(df)} player rows from {df.GameId.nunique()} games to {a.out}; {len(bad)} skipped')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('mode', choices=['files', 'season']); ap.add_argument('--out', required=True)
    ap.add_argument('--schedule'); ap.add_argument('--since'); ap.add_argument('pages', nargs='*'); main(ap.parse_args())
