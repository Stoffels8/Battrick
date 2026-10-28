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

    # Battrick uses table.scorecard — batting tables have "First Innings of:" in first row
    innings = []
    for table in soup.select('table.scorecard'):
        first_row_text = table.find('tr').get_text(' ', strip=True) if table.find('tr') else ''
        if 'First Innings of' not in first_row_text and 'Second Innings of' not in first_row_text:
            continue  # skip FOW and bowling tables

        batters = []
        runs = 0
        wickets = 0
        overs = 0.0
        team_name = ''

        # Extract team name from the innings header link
        team_link = table.select_one('tr:first-child a[href*="office.asp"]')
        if team_link:
            team_name = team_link.get_text(strip=True)

        for row in table.find_all('tr'):
            cells = row.find_all('td')
            if not cells:
                continue
            cell_texts = [td.get_text(strip=True) for td in cells]
            joined = ' '.join(cell_texts)

            # Total row: colspan=3 cell "Total (42.2 overs, 10 wickets)" then runs cell
            if joined.lower().startswith('total'):
                # Extract overs and wickets from the text
                m_overs = re.search(r'([\d]+\.[\d]+|[\d]+)\s*overs', joined)
                m_wkts = re.search(r'(\d+)\s*wickets?', joined)
                if m_overs:
                    overs = float(m_overs.group(1))
                if m_wkts:
                    wickets = int(m_wkts.group(1))
                # Runs: find the first cell that is purely a number (not the "Total (...)" cell)
                for cell in cells:
                    txt = cell.get_text(strip=True)
                    if re.match(r'^\d+$', txt):
                        runs = int(txt)
                        break
                continue

            # Batter row — first cell contains playerID link (not the inndetails link)
            player_link = row.find('a', href=re.compile(r'playerdetails\.asp\?playerID=(\d+)'))
            if not player_link:
                continue
            pid_match = re.search(r'playerID=(\d+)', player_link['href'])
            if not pid_match:
                continue

            # Runs are in the 4th cell (index 3), right-aligned
            batter_runs = 0
            if len(cells) >= 4:
                try:
                    batter_runs = int(cells[3].get_text(strip=True))
                except ValueError:
                    pass

            batters.append({
                'player_id': int(pid_match.group(1)),
                'name': player_link.get_text(strip=True),
                'runs': batter_runs
            })

        if batters or runs > 0:
            innings.append({
                'team': team_name,
                'batters': batters,
                'runs': runs,
                'wickets': wickets,
                'overs': overs
            })

    if len(innings) < 2:
        return jsonify({'error': 'Could not parse two innings from this match page'}), 400

    # Battrick shows home team bats first
    home = innings[0]
    away = innings[1]

    return jsonify({
        'ok': True,
        'home_team': home.get('team', ''),
        'away_team': away.get('team', ''),
        'home_runs': home['runs'],
        'home_wickets': home['wickets'],
        'home_overs': home['overs'],
        'home_batting': home['batters'],
        'away_runs': away['runs'],
        'away_wickets': away['wickets'],
        'away_overs': away['overs'],
        'away_batting': away['batters'],
    })


# ── Health check ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'status': 'CLOD proxy running', 'endpoints': ['/fetch-squad', '/fetch-match']})


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
