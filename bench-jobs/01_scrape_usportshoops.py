"""
Scrape U SPORTS Hoops (usportshoops.ca) box scores and the upcoming schedule.

  python 01_scrape_usportshoops.py games    --season 2025-26 --out data/games_2025-26.csv
  python 01_scrape_usportshoops.py games    --season 2026-27 --out data/games_2026-27.csv
  python 01_scrape_usportshoops.py schedule --season 2026-27 --out data/schedule_2026-27.csv
  add --limit 5 to test on 5 games first.

Output columns match your existing CSV, plus GameId, GameType, IsUSports. Raw pages are cached in html_cache/
so re-runs only fetch new games. Every game is checked: player points must equal the team score.
"""
import argparse, os, re, sys, time, collections
import pandas as pd, requests
from bs4 import BeautifulSoup

BASE = 'https://usportshoops.ca'
LISTING = BASE + '/history/seasongames.php?Gender=MBB&Season={season}'
REPORT = BASE + '/history/show-game-report.php?Gender=MBB&Season={season}&Gameid={gid}'
UPCOMING = BASE + '/mbb2026/gamesupcoming.php'
HEADERS = {'User-Agent': 'TheBench-data-pipeline (contact: ' + os.environ.get('BENCH_CONTACT', 'not provided') + ')'}

SHORT_TO_FULL = {
 'Acadia':'Acadia Axemen','Alberta':'Alberta Golden Bears','Algoma':'Algoma Thunderbirds',"Bishop's":"Bishop's Gaiters",
 'Brandon':'Brandon Bobcats','Brock':'Brock Badgers','Calgary':'Calgary Dinos','Cape Breton':'Cape Breton Capers',
 'Carleton':'Carleton Ravens','Concordia':'Concordia Stingers','Dalhousie':'Dalhousie Tigers','Guelph':'Guelph Gryphons',
 'Lakehead':'Lakehead Thunderwolves','Laurentian':'Laurentian Voyageurs','Laval':'Laval Rouge et Or',
 'Lethbridge':'Lethbridge Pronghorns','MacEwan':'MacEwan Griffins','Manitoba':'Manitoba Bisons','McGill':'McGill Redbirds',
 'McMaster':'McMaster Marauders','Memorial':'Memorial Sea-Hawks','Mount Royal':'Mount Royal University Cougars',
 'Nipissing':'Nipissing Lakers','Ontario Tech':'Ontario Tech Ridgebacks','Ottawa':'Ottawa Gee Gees',"Queen's":"Queen's Gaels",
 'Regina':'Regina Cougars',"Saint Mary's":"Saint Mary's Huskies",'Saskatchewan':'Saskatchewan Huskies',
 'StFX':'St. Francis Xavier X-Men','TMU':'TMU Bold','Thompson Rivers':'Thompson Rivers Wolfpack','Toronto':'Toronto Varsity Blues',
 'Trinity Western':'Trinity Western Spartans','UBC':'UBC Thunderbirds','UBCO':'UBC Okanagan Heat','UFV':'UFV Cascades',
 'UNB':'UNB Reds','UNBC':'UNBC Timberwolves','UPEI':'UPEI Panthers','UQAM':'UQAM Citadins','Victoria':'Victoria Vikes',
 'Waterloo':'Waterloo Warriors','Western':'Western Mustangs','Laurier':'Wilfrid Laurier Golden Hawks','Windsor':'Windsor Lancers',
 'Winnipeg':'Winnipeg Wesmen','York':'York Lions'}
ALIASES = {'UBCOKN':'UBCO','TMUnow':'TMU','WLUteam':'Laurier','Queens':"Queen's",'Bishops':"Bishop's",'Saint Marys':"Saint Mary's"}
FULL = set(SHORT_TO_FULL.values())

def resolve(name):
    """Map any site spelling to the team names used in your existing data. Returns None for non-U SPORTS teams."""
    n = (name or '').strip(); n = SHORT_TO_FULL.get(ALIASES.get(n, n), n)
    if n in FULL: return n
    if n.startswith('Toronto Metropolitan'): return 'TMU Bold'
    for k in sorted(SHORT_TO_FULL, key=len, reverse=True):
        if (name or '').startswith(k + ' '): return SHORT_TO_FULL[k]
    return None

def get(url, cache_path=None, delay=1.5):
    if cache_path and os.path.exists(cache_path):
        return open(cache_path, encoding='utf-8').read()
    r = requests.get(url, headers=HEADERS, timeout=30); r.raise_for_status()
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True); open(cache_path, 'w', encoding='utf-8').write(r.text)
    time.sleep(delay); return r.text

def game_ids(season):
    html = get(LISTING.format(season=season))     # always refetch: new games appear daily
    return list(dict.fromkeys(re.findall(r'Gameid=(M\d{8}[A-Z]{6})', html)))

def row_text(tr):
    return re.sub(r'\s+', ' ', tr.get_text(' ', strip=True)).strip()

def ab(s):
    m = re.fullmatch(r'(\d+)-(\d+)', s or ''); return (int(m[1]), int(m[2])) if m else (None, None)

_PAT = [r'\d+', r'\d+-\d+', r'\d+(?:\.\d+)?', r'\d+-\d+', r'\d+(?:\.\d+)?', r'\d+-\d+', r'\d+(?:\.\d+)?'] + [r'\d+'] * 9

def parse_player(text):
    """A player row ends in 16 stat values: mins, 3pt made-att, 3pt%, fg made-att, fg%, ft made-att, ft%,
    off reb, def reb, tot reb, PF, A, TO, Blk, Stl, Pts. Some exhibition games have no minutes (15 values).
    Header rows and other text never match this shape."""
    tok = text.replace('|', ' ').split()
    for n, pats in ((16, _PAT), (15, _PAT[1:])):
        if len(tok) < n + 1: continue
        st = tok[-n:]
        if all(re.fullmatch(pt, x) for pt, x in zip(pats, st)):
            if n == 15: st = [''] + st
            left = tok[:-n]; starter = '*' in left; left = [x for x in left if x != '*']
            jersey = left.pop(0) if len(left) > 1 and re.fullmatch(r'\d+', left[0]) else ''
            return dict(Jersey=jersey, name=' '.join(left), starter=starter, st=st)
    return None

class NoStats(Exception): pass

def _parse_report(html, gid, season, parser):
    soup = BeautifulSoup(html, parser); meta, scores, rows, team, sample, alltext = {}, {}, [], None, [], []
    for tr in soup.find_all('tr'):
        if tr.find('tr'): continue                      # skip layout rows that wrap other rows
        text = row_text(tr)
        if not text: continue
        alltext.append(text)
        m = re.match(r'^(Date|Location|Game):\s*(.+)$', text)
        if m: meta[m[1]] = m[2].strip(); continue
        pl = parse_player(text)
        if pl:
            if team is None or 'Totals' in pl['name'] or pl['name'].lower().startswith('team') or not pl['name']: continue
            mins, a3, _, afg, _, aft, _, o, d, r, pf, ast, to, blk, stl, pts = pl['st']
            (m3, a3_), (fgm, fga), (ftm, fta) = ab(a3), ab(afg), ab(aft)
            rows.append(dict(team=team, Jersey=pl['Jersey'], PlayerName=pl['name'], StarterFlag=pl['starter'], Mins=(int(mins) if mins != '' else None),
                             tpm=m3, tpa=a3_, FGM=fgm, FGA=fga, FTM=ftm, FTA=fta, Reb_O=int(o), Reb_D=int(d), Reb_T=int(r),
                             PF=int(pf), AST=int(ast), TO=int(to), BLK=int(blk), STL=int(stl), Pts=int(pts)))
            continue
        m = re.fullmatch(r'(.+?) (\d{1,3})', text)       # team label with its score, e.g. "Brandon Bobcats 101"
        if m and '|' not in text and not text.startswith(('Player', '*')) and 'Totals' not in text:
            team = m[1]; scores.setdefault(team, int(m[2]))
    if not rows:
        ratio = [t for t in alltext if len(re.findall(r'\b\d+-\d+\b', t)) >= 3]
        if len(ratio) >= 4: raise ValueError(f'{gid}: page has stat-like rows but none could be read. Examples: {[t[:160] for t in ratio[:4]]}')
        raise NoStats(f'{gid}: no player stats on this page')
    sample = [t[:110] for t in alltext[:6]]
    teams = list(dict.fromkeys(r['team'] for r in rows))
    if len(teams) != 2 or 'Date' not in meta:
        raise ValueError(f'{gid}: found {len(rows)} players, teams {teams}, meta {list(meta)}. First rows of page: {sample}')
    for tm in teams:
        got = sum(r['Pts'] for r in rows if r['team'] == tm)
        if scores.get(tm) is None or got != scores[tm]:
            raise ValueError(f'{gid}: {tm} players sum to {got}, team score {scores.get(tm)}')
    score = {tm: scores[tm] for tm in teams}
    date = pd.to_datetime(meta['Date']).strftime('%Y-%m-%d')
    out = []
    for r in rows:
        opp = [t for t in teams if t != r['team']][0]; fn = resolve(r['team'])
        p = {**r}; p3m, p3a = p.pop('tpm'), p.pop('tpa'); tm = p.pop('team')
        fga, fta = p['FGA'], p['FTA']; pct = lambda a, b: round(100 * a / b, 1) if b else 0.0
        out.append({'Season': season, 'Date': date, 'Location': meta.get('Location'), 'Team': fn or tm, 'Opponent': resolve(opp) or opp,
            'Abbr': None, 'Jersey': p['Jersey'], 'PlayerName': p['PlayerName'], 'StarterFlag': p['StarterFlag'], 'Mins': p['Mins'],
            '3PT_Pct': pct(p3m, p3a), 'FG_Pct': pct(p['FGM'], fga), 'FT_Pct': pct(p['FTM'], fta),
            'Reb_O': p['Reb_O'], 'Reb_D': p['Reb_D'], 'Reb_T': p['Reb_T'], 'PF': p['PF'], 'AST': p['AST'], 'TO': p['TO'],
            'BLK': p['BLK'], 'STL': p['STL'], 'Pts': p['Pts'], '3PTM': p3m, '3PTA': p3a, 'FGM': p['FGM'], 'FGA': fga,
            'FTM': p['FTM'], 'FTA': fta,
            'TS_Pct': round(100 * p['Pts'] / (2 * (fga + 0.44 * fta)), 2) if (fga + fta) else 0.0,
            'eFG_Pct': round(100 * (p['FGM'] + 0.5 * p3m) / fga, 2) if fga else 0.0,
            'HomeAway': None, 'TeamPoints': score[tm], 'OppPoints': score[opp], 'GameId': gid,
            'GameType': meta.get('Game'), 'IsUSports': fn is not None, '_home_code': gid[9:12], '_team_raw': tm})
    return out

def parse_report(html, gid, season):
    err = None
    for parser in ('html.parser', 'lxml'):
        try: return _parse_report(html, gid, season, parser)
        except Exception as e:
            if e.__class__.__name__ == 'FeatureNotFound': continue
            err = err or e
    raise err

def fill_home_away(df):
    """Game IDs look like <date><HOME><AWAY>. Learn each team's code from the games it appears in, then mark league games."""
    cand = collections.defaultdict(lambda: None)
    for gid, g in df.groupby('GameId'):
        codes = {gid[9:12], gid[12:15]}
        for t in g.Team.unique(): cand[t] = codes if cand[t] is None else cand[t] & codes
    code = {t: list(c)[0] for t, c in cand.items() if c and len(c) == 1}
    league = df.GameType.fillna('').str.contains('Regular season league')
    home = df.apply(lambda r: ('Home' if code.get(r.Team) == r._home_code else 'Away') if (league[r.name] and r.Team in code) else None, axis=1)
    df['HomeAway'] = home; df['Abbr'] = df.Team.map(code)
    return df.drop(columns=['_home_code', '_team_raw'])

def run_games(season, out, limit, cache, game=None, add=None):
    ids = [game] if game else (game_ids(season)[-limit:] if limit else game_ids(season))
    if add: ids += [g.strip() for g in add.split(',') if g.strip() and g.strip() not in ids]   # games the site's list has not picked up yet
    print(f'{len(ids)} box scores to read for {season}' + (' (the most recent ones)' if limit and not game else ''))
    rows, bad, nostats = [], [], 0
    for i, gid in enumerate(ids, 1):
        try: rows += parse_report(get(REPORT.format(season=season, gid=gid), os.path.join(cache, season, gid + '.html')), gid, season)
        except NoStats: nostats += 1
        except Exception as e: bad.append((gid, str(e)[:600])); print('  SKIPPED', gid, str(e)[:400])
        if i % 25 == 0: print(f'  {i}/{len(ids)}')
    if not rows:
        print(f'No games could be read ({nostats} pages had no box score). First error:', bad[0] if bad else 'none'); sys.exit(1)
    df = fill_home_away(pd.DataFrame(rows)); os.makedirs(os.path.dirname(out) or '.', exist_ok=True); df.to_csv(out, index=False)
    print(f'Saved {len(df)} player rows from {df.GameId.nunique()} games to {out}; {len(bad)} games failed, {nostats} had no box score')
    unk = sorted(df.loc[~df.IsUSports, 'Team'].unique()); print('Non-U SPORTS teams seen:', unk[:15], '...' if len(unk) > 15 else '')
    if bad: pd.DataFrame(bad, columns=['GameId', 'error']).to_csv(out.replace('.csv', '_errors.csv'), index=False)

def run_schedule(season, out):
    from urllib.parse import unquote
    soup = BeautifulSoup(get(UPCOMING), 'html.parser'); y0 = int(season[:4]); rows, date = [], None
    for tr in soup.find_all('tr'):
        if tr.find('tr'): continue
        a = tr.find('a', href=re.compile(r'matchup\.php'))
        if not a: continue
        text = row_text(tr)
        m = re.match(r'^((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} \d{1,2})\s+(.*)$', text)
        if m: date, text = m[1], m[2]                      # rows after the first on a day leave the date blank
        if date is None: continue
        q = dict(re.findall(r'Team(one|two)=([^&]+)', a['href'])); home, away = unquote(q.get('one', '')), unquote(q.get('two', ''))
        if home == 'Unknown' and away == 'Unknown': continue   # playoff placeholders not decided yet
        loc = re.match(r"^(.+?, [A-Z]{2}|Unknown)\b\s*(\*|p)?", text)
        mon = pd.to_datetime(date + ' 2000', format='%a %b %d %Y').month
        d = pd.to_datetime(f"{date} {y0 if mon >= 8 else y0 + 1}", format='%a %b %d %Y')
        after = text.split('prior games', 1)[1].strip() if 'prior games' in text else ''
        rows.append({'Date': d.strftime('%Y-%m-%d'), 'Location': loc[1] if loc else None, 'League': bool(loc and loc[2] == '*'),
                     'Home': resolve(home), 'Away': resolve(away), 'HomeRaw': home, 'AwayRaw': away, 'Comment': after or None})
    if not rows: print('No upcoming games could be read.'); sys.exit(1)
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    df = pd.DataFrame(rows); df.to_csv(out, index=False); print(f'Saved {len(df)} upcoming games to {out}')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('mode', choices=['games', 'schedule']); ap.add_argument('--season', required=True)
    ap.add_argument('--out', required=True); ap.add_argument('--limit', type=int); ap.add_argument('--game'); ap.add_argument('--add', help='extra Gameid(s), comma separated, for games missing from the site list'); ap.add_argument('--cache', default='html_cache')
    a = ap.parse_args()
    run_games(a.season, a.out, a.limit, a.cache, a.game, a.add) if a.mode == 'games' else run_schedule(a.season, a.out)
