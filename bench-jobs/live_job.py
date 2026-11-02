"""
Keeps the live_stats table in Supabase up to date during games. Run on a timer (GitHub Actions, every 5 minutes).

  python live_job.py                      -> read today's games from the U SPORTS schedule page and update live_stats
  python live_job.py --file page.html     -> test on one saved box score page, print what it would write, change nothing

Needs two secrets in the environment: SUPABASE_URL and SUPABASE_SERVICE_KEY (never put these in a file).
Only touches live_stats. It does not grade slips.
"""
import argparse, json, os, re, sys, time, importlib.util
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

_here = os.path.dirname(os.path.abspath(__file__))
_s = importlib.util.spec_from_file_location('us', os.path.join(_here, '10_scrape_usports.py'))
us = importlib.util.module_from_spec(_s); _s.loader.exec_module(us)

def to_rows(parsed):
    out = []
    for r in parsed:
        if r['Mins'] == 0 and r['Pts'] == 0 and r['Reb_T'] == 0: continue          # did not play (yet)
        out.append({'team': r['Team'], 'player': r['PlayerName'], 'pts': int(r['Pts']), 'reb': int(r['Reb_T']),
                    'is_final': str(r['Status']).lower() == 'final', 'updated_at': datetime.now(timezone.utc).isoformat()})
    return out


def parse_feed(j):
    """Rows from the live feed (JSON). Returns (rows, complete_flag, status_text)."""
    st = j.get('status', {}); done = str(st.get('complete', 'N')).upper() == 'Y'
    pr = st.get('period'); pr = pr[0] if isinstance(pr, list) and pr else pr
    status = 'Final' if done else f"Q{pr} {st.get('clock','')}".strip()
    out = []; scores = {}
    for t in j['team']:
        team = us.sc.resolve(t.get('name')) or t.get('name')
        tot = 0
        for p in t.get('player', []):
            if p.get('name') == 'TEAM' or str(p.get('uni')) == 'TM': continue
            x = p.get('stats') or {}
            tp, tr, mn = int(x.get('tp') or 0), int(x.get('treb') or 0), int(x.get('min') or 0)
            tot += tp
            if str(p.get('gp')) != '1' and not mn and not tp and not tr: continue          # did not play
            out.append({'team': team, 'player': p['name'], 'pts': tp, 'reb': tr, 'is_final': done,
                        'updated_at': datetime.now(timezone.utc).isoformat()})
        sc_ = t.get('linescore', {}).get('score'); sc_ = int(sc_[0] if isinstance(sc_, list) else sc_ or 0)
        scores[team] = sc_
        if tot != sc_: raise ValueError(f"{team}: players add to {tot} but score is {sc_}")      # refuse bad data
    return out, done, status, scores

def fetch_feed(gid):
    page = requests.get(f'{us.BASE}/sports/mbkb/{us.SEASON}/boxscores/{gid}.xml', headers=us.HEADERS, timeout=30); page.raise_for_status()
    e = re.search(r"conf\.gamedayServiceEntryPointUrl\s*=\s*'([^']+)'", page.text); h = re.search(r"conf\.eventIdHashCode\s*=\s*'([^']+)'", page.text)
    if not (e and h): return None                                  # not a live-style page (already final, or not started)
    from urllib.parse import quote
    r = requests.get(us.BASE + e.group(1) + 'h=' + quote(h.group(1), safe=''), headers=us.HEADERS, timeout=30); r.raise_for_status()
    j = r.json(); return json.loads(j['data']) if isinstance(j.get('data'), str) else j

def upsert(rows):
    url, key = os.environ['SUPABASE_URL'].rstrip('/'), os.environ['SUPABASE_SERVICE_KEY']
    h = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
         'Prefer': 'resolution=merge-duplicates,return=minimal'}
    r = requests.post(f'{url}/rest/v1/live_stats?on_conflict=team,player', headers=h, json=rows, timeout=30)
    if r.status_code >= 300: raise RuntimeError(f'Supabase said {r.status_code}: {r.text[:300]}')

def save_scores(d8, scores, done):
    """Write the team scores onto the games row so the Calendar tab can show them (and keep them after the game)."""
    url, key = os.environ['SUPABASE_URL'].rstrip('/'), os.environ['SUPABASE_SERVICE_KEY']
    h = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}
    day = f'{d8[:4]}-{d8[4:6]}-{d8[6:]}'
    g = requests.get(f'{url}/rest/v1/games?game_date=eq.{day}&select=id,away,home', headers=h, timeout=30).json()
    for x in g:
        if x['away'] in scores and x['home'] in scores:
            r = requests.patch(f"{url}/rest/v1/games?id=eq.{x['id']}", headers=h,
                               json={'away_score': scores[x['away']], 'home_score': scores[x['home']], 'is_final': bool(done)}, timeout=30)
            if r.status_code >= 300: print('  could not save score:', r.status_code, r.text[:200])

def main(a):
    if a.json:
        rows, done, status, scores = parse_feed(json.load(open(a.json))); print(status, scores, '|', len(rows), 'rows would be written. First 5:'); [print(' ', r) for r in rows[:5]]; return
    if a.file:
        rows = to_rows(us.parse_boxscore(open(a.file, encoding='utf-8', errors='ignore').read(), os.path.basename(a.file), final_only=False))
        print(len(rows), 'rows would be written. First 5:'); [print(' ', r) for r in rows[:5]]; return
    now = datetime.now(ZoneInfo('America/Toronto')); days = {(now - timedelta(days=d)).strftime('%Y%m%d') for d in (0, 1)}
    page = requests.get(f'{us.BASE}/sports/mbkb/{us.SEASON}/schedule', headers=us.HEADERS, timeout=30); page.raise_for_status()
    ids = [g for g in us.schedule_links(page.text) if g[:8] in days]
    print(now.isoformat(timespec='minutes'), len(ids), 'games on the schedule for today/yesterday')
    total = 0
    for g in ids:
        try:
            feed = fetch_feed(g)
            if feed is None: print(' ', g, 'no live feed on the page (not started, or final: use the box score scraper)'); continue
            rows, done, status, scores = parse_feed(feed)
            if rows: upsert(rows); total += len(rows)
            if scores: save_scores(g[:8], scores, done)
            print(' ', g, status, len(rows), 'players')
        except Exception as e: print('  skipped', g, str(e)[:200])
        time.sleep(1.5)
    print('wrote', total, 'rows')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--file'); ap.add_argument('--json', help='test on a saved live-update JSON file'); main(ap.parse_args())
