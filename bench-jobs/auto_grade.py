"""
Grades finished games automatically from the OFFICIAL U SPORTS site (en.usports.ca). Run on a timer (GitHub Actions, every 30 minutes).

What it does, in order:
  1. Looks in Supabase for published board lines that have no result yet, for games dated today or the last 3 days.
  2. Reads the box score of each of those days' games from en.usports.ca. A game is only used once the site marks it FINAL
     and the player points add up to the final score (the scraper refuses anything else).
  3. Saves actual + result (OVER / UNDER / VOID) on each line, then grades the slips for that day (same rules as the Admin page).
  4. Saves each final score on the games table (for the Calendar tab).

Rules (same as 07_grade.py): a player who did not play (0 minutes, or not in the box score) is VOID. Lines end in .5, so no pushes.
A line is NEVER touched until its game is final, so a live or postponed game just waits. Nothing is deleted. Safe to run again and again.

  python auto_grade.py              grade for real
  python auto_grade.py --dry-run    show what it would do, change nothing
"""
import argparse, difflib, os, re, sys, time, unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import importlib.util

_here = os.path.dirname(os.path.abspath(__file__))
_s = importlib.util.spec_from_file_location('us', os.path.join(_here, '10_scrape_usports.py'))
us = importlib.util.module_from_spec(_s); _s.loader.exec_module(us)

STAT_KEY = {'Points': 'Pts', 'Rebounds': 'Reb_T'}

def norm(n):
    n = unicodedata.normalize('NFKD', str(n or '')).encode('ascii', 'ignore').decode().lower()
    if ',' in n: a, b = n.split(',', 1); n = b + ' ' + a                      # "Last, First" -> "First Last"
    n = re.sub(r'[^a-z ]', ' ', n)
    return ' '.join(sorted(n.split()))

def find_player(line, rows):
    """Box score row for this board line, or None. Name first, then jersey + a close name."""
    want = norm(line['player']); team = [r for r in rows if r['Team'] == line['team']]
    for r in team:
        if norm(r['PlayerName']) == want: return r
    best = max(team, key=lambda r: difflib.SequenceMatcher(None, norm(r['PlayerName']), want).ratio(), default=None)
    if best and difflib.SequenceMatcher(None, norm(best['PlayerName']), want).ratio() >= 0.88: return best
    j = str(line.get('jersey') or '').strip()
    if j:
        same = [r for r in team if str(int(float(r['Jersey']))) == j.lstrip('0') or str(r['Jersey']) == j]
        if len(same) == 1 and difflib.SequenceMatcher(None, norm(same[0]['PlayerName']), want).ratio() >= 0.6: return same[0]
    return None

def grade_lines(lines, rows):
    """lines: board lines with no result. rows: final box-score rows. Returns [(line, actual, result, note)] for lines whose team has a FINAL game in rows."""
    teams_final = {r['Team'] for r in rows}; out = []
    for ln in lines:
        if ln['team'] not in teams_final: continue                            # game not final (or not found): wait
        r = find_player(ln, rows)
        if r is None: out.append((ln, None, 'VOID', 'not in the box score')); continue
        if float(r['Mins'] or 0) <= 0: out.append((ln, None, 'VOID', '0 minutes')); continue
        actual = float(r[STAT_KEY[ln['stat']]]); out.append((ln, actual, 'OVER' if actual > float(ln['line']) else 'UNDER', ''))
    return out

# ----------------------------------------------------------------------------------------------------------- Supabase
def _h():
    key = os.environ['SUPABASE_SERVICE_KEY']
    return {'apikey': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}
def _u(): return os.environ['SUPABASE_URL'].rstrip('/') + '/rest/v1/'
def _chk(r):
    if r.status_code >= 300: raise RuntimeError(f'Supabase said {r.status_code}: {r.text[:300]}')
    return r

def open_lines(since, today):
    q = f"board_lines?select=id,slate_date,player,team,opponent,jersey,stat,line,status&result=is.null&status=eq.published&slate_date=gte.{since}&slate_date=lte.{today}&limit=2000"
    return _chk(requests.get(_u() + q, headers=_h(), timeout=30)).json()

def final_rows_for(day8, sched_html):
    ids = [g for g in us.schedule_links(sched_html) if g[:8] == day8]; rows, skipped = [], []
    for g in ids:
        try:
            r = requests.get(f'{us.BASE}/sports/mbkb/{us.SEASON}/boxscores/{g}.xml', headers=us.HEADERS, timeout=30); r.raise_for_status()
            rows += us.parse_boxscore(r.text, g)                              # raises unless FINAL and points add up
        except Exception as e: skipped.append((g, str(e)[:120]))
        time.sleep(1.5)
    return rows, skipped, len(ids)

def main(a):
    now = datetime.now(ZoneInfo('America/Toronto')); today = now.strftime('%Y-%m-%d'); since = (now - timedelta(days=3)).strftime('%Y-%m-%d')
    lines = open_lines(since, today); print(now.isoformat(timespec='minutes'), len(lines), 'published lines waiting for a result')
    if not lines: return
    page = requests.get(f'{us.BASE}/sports/mbkb/{us.SEASON}/schedule', headers=us.HEADERS, timeout=30); page.raise_for_status()
    done_days = set(); total = 0
    for day in sorted({l['slate_date'] for l in lines}):
        day_lines = [l for l in lines if l['slate_date'] == day]
        rows, skipped, n = final_rows_for(day.replace('-', ''), page.text)
        print(f'{day}: {len(day_lines)} lines, {n} games on the schedule, {len({r["GameId"] for r in rows})} final; waiting/skipped: {[g for g, _ in skipped][:8]}')
        graded = grade_lines(day_lines, rows)
        for ln, actual, res, note in graded:
            print(f"   {res:5} {ln['team']} | {ln['player']} {ln['stat']} {ln['line']} -> {actual}{('  (' + note + ')') if note else ''}")
            if not a.dry_run:
                _chk(requests.patch(_u() + f"board_lines?id=eq.{ln['id']}", headers=_h(), json={'actual': actual, 'result': res}, timeout=30))
        total += len(graded)
        if graded and not a.dry_run:
            _chk(requests.patch(_u() + f"board_lines?slate_date=eq.{day}&status=eq.struck&result=is.null", headers=_h(), json={'result': 'VOID'}, timeout=30))
            _chk(requests.post(_u() + 'rpc/grade_slips', headers=_h(), json={'p_slate': day}, timeout=60)); done_days.add(day)
        if not a.dry_run:                                                     # final scores for the Calendar tab
            seen = set()
            for r in rows:
                if r['GameId'] in seen or r['HomeAway'] != 'Away': continue
                seen.add(r['GameId'])
                _chk(requests.patch(_u() + f"games?game_date=eq.{r['Date']}&away=eq.{requests.utils.quote(r['Team'])}&home=eq.{requests.utils.quote(r['Opponent'])}",
                     headers=_h(), json={'away_score': int(r['TeamPoints']), 'home_score': int(r['OppPoints']), 'is_final': True}, timeout=30))
    print(f"{'DRY RUN: ' if a.dry_run else ''}graded {total} lines; slips graded for {sorted(done_days) or 'no days'}")

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--dry-run', action='store_true'); main(ap.parse_args())
