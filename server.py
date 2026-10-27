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

    # Find innings tables — Battrick match pages have two batting scorecards
    innings = []
    for table in soup.select('table'):
        rows = table.find_all('tr')
        if len(rows) < 3:
            continue
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(['th', 'td'])]
        if not any('batsman' in h or 'batter' in h or 'batting' in h for h in headers):
            continue

        batters = []
        runs = 0
        wickets = 0
        overs = 0.0

        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all('td')]
            if not cells:
                continue

            # Totals row
            text = ' '.join(cells).lower()
            if 'total' in text or 'extras' in text:
                # Try to extract total runs and wickets
                for cell in cells:
                    m = re.search(r'(\d+)\s*/\s*(\d+)', cell)
                    if m:
                        runs = int(m.group(1))
                        wickets = int(m.group(2))
                # Overs
                for cell in cells:
                    m = re.search(r'(\d+)\.(\d)', cell)
                    if m:
                        overs = float(f"{m.group(1)}.{m.group(2)}")
                continue

            # Player row — look for player link
            link = row.find('a', href=re.compile(r'playerID=(\d+)'))
            if not link:
                continue
            pid_match = re.search(r'playerID=(\d+)', link['href'])
            if not pid_match:
                continue

            # Runs scored — usually 3rd or 4th cell
            batter_runs = 0
            for cell in cells[2:6]:
                if re.match(r'^\d+\*?$', cell.strip()):
                    batter_runs = int(cell.replace('*', ''))
                    break

            batters.append({
                'player_id': int(pid_match.group(1)),
                'name': link.get_text(strip=True),
                'runs': batter_runs
            })

        if batters or runs > 0:
            innings.append({
                'batters': batters,
                'runs': runs,
                'wickets': wickets,
                'overs': overs
            })

    if len(innings) < 2:
        return jsonify({'error': 'Could not parse two innings from this match page'}), 400

    # Determine home/away from page title or team names
    # Battrick shows home team first
    home = innings[0]
    away = innings[1]

    # Try to get team names from page
    home_team = ''
    away_team = ''
    title_el = soup.find('h1') or soup.find('title')
    if title_el:
        title = title_el.get_text(strip=True)
        m = re.search(r'(.+?)\s+v[s]?\s+(.+)', title, re.IGNORECASE)
        if m:
            home_team = m.group(1).strip()
            away_team = m.group(2).strip()

    return jsonify({
        'ok': True,
        'home_team': home_team,
        'away_team': away_team,
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
