"""
CLOD Battrick Proxy Server
Two endpoints only:
  POST /fetch-squad  { "url": "https://www.battrick.org/nl/squad.asp?teamID=1841" }
  POST /fetch-match  { "url": "https://www.battrick.org/nl/matchinfo.asp?matchID=32036072" }
No database. No state. Just fetches Battrick and returns JSON.
"""

import re
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)  # Allow requests from any origin (your local HTML file)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# ── Squad fetch ────────────────────────────────────────────────────────────────

@app.route('/fetch-squad', methods=['POST'])
def fetch_squad():
    url = (request.json or {}).get('url', '').strip()
    if not url or 'battrick.org' not in url:
        return jsonify({'error': 'Invalid URL'}), 400

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return jsonify({'error': f'Could not fetch squad page: {e}'}), 502

    soup = BeautifulSoup(resp.text, 'lxml')
    players = []

    # Battrick squad page uses div.player with CSS spans for each field
    for player_div in soup.select('div.player'):
        # Name + player ID
        name_link = player_div.select_one('span.player_name a[href*="playerID="]')
        if not name_link:
            continue
        pid_match = re.search(r'playerID=(\d+)', name_link['href'])
        if not pid_match:
            continue
        player_id = int(pid_match.group(1))
        name = name_link.get_text(strip=True)

        # Wage: span.player_wage contains e.g. "£144,233"
        wage_span = player_div.select_one('span.player_wage')
        if not wage_span:
            continue
        wage_text = wage_span.get_text(strip=True).replace(',', '').replace('£', '').replace('$', '').strip()
        try:
            wage = int(wage_text)
        except ValueError:
            continue

        # BTR: span.player_btr contains e.g. "176,162"
        btr_span = player_div.select_one('span.player_btr')
        btr = 0
        if btr_span:
            btr_text = btr_span.get_text(strip=True).replace(',', '').strip()
            try:
                btr = int(btr_text)
            except ValueError:
                btr = 0

        players.append({
            'player_id': player_id,
            'name': name,
            'wage': wage,
            'btr': btr,
            'eligible': wage < 1000
        })

    if not players:
        return jsonify({'error': 'No players found — check the URL is a Battrick squad page'}), 400

    # Strength: top 15 eligible (wage < 1000), score = wage * (1 + btr/100000)
    eligible = [p for p in players if p['eligible']]
    eligible.sort(key=lambda p: p['wage'] * (1 + p['btr'] / 100000), reverse=True)
    top15 = eligible[:15]
    strength = round(
        sum(p['wage'] * (1 + p['btr'] / 100000) for p in top15) / len(top15), 2
    ) if top15 else 0

    return jsonify({
        'ok': True,
        'players': players,
        'eligible_count': len(eligible),
        'strength': strength
    })


# ── Match fetch ────────────────────────────────────────────────────────────────

@app.route('/fetch-match', methods=['POST'])
def fetch_match():
    url = (request.json or {}).get('url', '').strip()
    if not url or 'battrick.org' not in url:
        return jsonify({'error': 'Invalid URL'}), 400

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return jsonify({'error': f'Could not fetch match page: {e}'}), 502

    soup = BeautifulSoup(resp.text, 'lxml')

    # Battrick uses table.scorecard for batting, bowling, and FOW tables.
    # Batting tables: first row contains "First Innings of:" or "Second Innings of:"
    # Bowling tables: immediately follow the batting table for the same innings;
    #   their header row contains column headings like "Overs", "Mdns", "Runs", "Wkts"
    #   (no innings label in the first row).
    # FOW tables: also no innings label; we distinguish bowling from FOW by header content.

    innings = []
    all_tables = soup.select('table.scorecard')

    def parse_int(txt):
        try: return int(txt.strip())
        except: return 0

    def parse_float(txt):
        try: return float(txt.strip())
        except: return 0.0

    i = 0
    while i < len(all_tables):
        table = all_tables[i]
        first_row_text = table.find('tr').get_text(' ', strip=True) if table.find('tr') else ''

        if 'First Innings of' not in first_row_text and 'Second Innings of' not in first_row_text:
            i += 1
            continue

        # ── Batting table ──────────────────────────────────────────────────────
        batters = []
        runs = 0
        wickets = 0
        overs = 0.0
        team_name = ''

        team_link = table.select_one('tr:first-child a[href*="office.asp"]')
        if team_link:
            team_name = team_link.get_text(strip=True)

        for row in table.find_all('tr'):
            cells = row.find_all('td')
            if not cells:
                continue
            cell_texts = [td.get_text(strip=True) for td in cells]
            joined = ' '.join(cell_texts)

            if joined.lower().startswith('total'):
                m_overs = re.search(r'([\d]+\.[\d]+|[\d]+)\s*overs', joined)
                m_wkts = re.search(r'(\d+)\s*wickets?', joined)
                if m_overs: overs = float(m_overs.group(1))
                if m_wkts: wickets = int(m_wkts.group(1))
                for cell in cells:
                    txt = cell.get_text(strip=True)
                    if re.match(r'^\d+$', txt):
                        runs = int(txt)
                        break
                continue

            player_link = row.find('a', href=re.compile(r'playerdetails\.asp\?playerID=(\d+)'))
            if not player_link:
                continue
            pid_match = re.search(r'playerID=(\d+)', player_link['href'])
            if not pid_match:
                continue

            batter_runs = 0
            if len(cells) >= 4:
                try: batter_runs = int(cells[3].get_text(strip=True))
                except: pass

            batters.append({
                'player_id': int(pid_match.group(1)),
                'name': player_link.get_text(strip=True),
                'runs': batter_runs
            })

        # ── Bowling table (next scorecard table after this batting table) ──────
        bowlers = []
        # Look ahead: next table(s) until we hit another innings table or run out
        j = i + 1
        while j < len(all_tables):
            next_table = all_tables[j]
            next_first = next_table.find('tr').get_text(' ', strip=True) if next_table.find('tr') else ''
            # If it's a new innings table, stop
            if 'First Innings of' in next_first or 'Second Innings of' in next_first:
                break
            # Check if this looks like a bowling table: header contains Overs/Mdns/Wkts
            header_text = next_first.lower()
            if 'overs' in header_text or 'mdns' in header_text or 'wkts' in header_text:
                # Parse bowler rows — each has a playerID link
                for row in next_table.find_all('tr'):
                    cells = row.find_all('td')
                    if not cells:
                        continue
                    player_link = row.find('a', href=re.compile(r'playerdetails\.asp\?playerID=(\d+)'))
                    if not player_link:
                        continue
                    pid_match = re.search(r'playerID=(\d+)', player_link['href'])
                    if not pid_match:
                        continue
                    # Bowling columns (after name): Overs, Mdns, Runs, Wkts, NB, WD
                    # First cell is the name cell; numeric cells follow
                    nums = []
                    for cell in cells[1:]:
                        txt = cell.get_text(strip=True)
                        try:
                            nums.append(float(txt))
                        except:
                            nums.append(None)
                    # Battrick bowling columns: Overs, Mdns, Runs, Wkts, NB, WD
                    def gn(idx, as_int=True):
                        v = nums[idx] if idx < len(nums) and nums[idx] is not None else 0
                        return int(v) if as_int else float(v)
                    b_overs   = gn(0, as_int=False)
                    b_maidens = gn(1)
                    b_runs    = gn(2)
                    b_wickets = gn(3)
                    b_nb      = gn(4)
                    b_wd      = gn(5)
                    bowlers.append({
                        'player_id': int(pid_match.group(1)),
                        'name': player_link.get_text(strip=True),
                        'overs':   b_overs,
                        'maidens': b_maidens,
                        'runs':    b_runs,
                        'wickets': b_wickets,
                        'nb':      b_nb,
                        'wd':      b_wd,
                    })
                j += 1
                break  # Only one bowling table per innings
            j += 1

        if batters or runs > 0:
            innings.append({
                'team':    team_name,
                'batters': batters,
                'bowlers': bowlers,
                'runs':    runs,
                'wickets': wickets,
                'overs':   overs
            })
        i = j  # advance past any tables we consumed

    if len(innings) < 2:
        return jsonify({'error': 'Could not parse two innings from this match page'}), 400

    home = innings[0]
    away = innings[1]

    return jsonify({
        'ok': True,
        'home_team':    home.get('team', ''),
        'away_team':    away.get('team', ''),
        'home_runs':    home['runs'],
        'home_wickets': home['wickets'],
        'home_overs':   home['overs'],
        'home_batting': home['batters'],
        'home_bowling': home['bowlers'],
        'away_runs':    away['runs'],
        'away_wickets': away['wickets'],
        'away_overs':   away['overs'],
        'away_batting': away['batters'],
        'away_bowling': away['bowlers'],
    })


# ── Health check ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'status': 'CLOD proxy running', 'endpoints': ['/fetch-squad', '/fetch-match']})


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
