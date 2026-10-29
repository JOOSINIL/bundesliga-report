"""경기 일정과 결과(뼈대 데이터).

- 분데스리가: OpenLigaDB (무료, 키 없음)
- 프리미어리그, 라리가: football-data.org 무료 요금제 (무료 가입 후 받은 토큰이 필요)
경기 결과·득점만 여기서 받고, 나머지 자료는 모두 웹 조사로 모읍니다.
"""

from __future__ import annotations

import os
import time

import requests

LEAGUES = {
    "PL": {"name": "프리미어리그", "short": "EPL", "source": "football-data"},
    "PD": {"name": "라리가", "short": "LALIGA", "source": "football-data"},
    "BL1": {"name": "분데스리가", "short": "BUNDESLIGA", "source": "openligadb"},
}

KO_NAMES = {
    # 프리미어리그
    "Arsenal FC": "아스널", "Aston Villa FC": "애스턴 빌라", "AFC Bournemouth": "본머스", "Brentford FC": "브렌트퍼드",
    "Brighton & Hove Albion FC": "브라이턴", "Burnley FC": "번리", "Chelsea FC": "첼시", "Crystal Palace FC": "크리스털 팰리스",
    "Everton FC": "에버턴", "Fulham FC": "풀럼", "Leeds United FC": "리즈 유나이티드", "Liverpool FC": "리버풀",
    "Manchester City FC": "맨체스터 시티", "Manchester United FC": "맨체스터 유나이티드", "Newcastle United FC": "뉴캐슬",
    "Nottingham Forest FC": "노팅엄 포리스트", "Sunderland AFC": "선덜랜드", "Tottenham Hotspur FC": "토트넘",
    "West Ham United FC": "웨스트햄", "Wolverhampton Wanderers FC": "울버햄튼", "Ipswich Town FC": "입스위치",
    "Leicester City FC": "레스터 시티", "Southampton FC": "사우샘프턴", "Coventry City FC": "코번트리",
    "Sheffield United FC": "셰필드 유나이티드", "Middlesbrough FC": "미들즈브러", "Norwich City FC": "노리치",
    "Hull City AFC": "헐 시티", "Watford FC": "왓퍼드", "West Bromwich Albion FC": "웨스트브로미치",
    # 라리가
    "Real Madrid CF": "레알 마드리드", "FC Barcelona": "바르셀로나", "Club Atlético de Madrid": "아틀레티코 마드리드",
    "Athletic Club": "아틀레틱 빌바오", "Villarreal CF": "비야레알", "Real Betis Balompié": "레알 베티스",
    "Real Sociedad de Fútbol": "레알 소시에다드", "Sevilla FC": "세비야", "Valencia CF": "발렌시아",
    "RC Celta de Vigo": "셀타 비고", "CA Osasuna": "오사수나", "Getafe CF": "헤타페", "Rayo Vallecano de Madrid": "라요 바예카노",
    "RCD Mallorca": "마요르카", "Girona FC": "지로나", "Deportivo Alavés": "알라베스", "RCD Espanyol de Barcelona": "에스파뇰",
    "UD Las Palmas": "라스팔마스", "CD Leganés": "레가네스", "Real Valladolid CF": "바야돌리드", "Elche CF": "엘체",
    "Levante UD": "레반테", "Real Oviedo": "레알 오비에도", "Racing Santander": "라싱 산탄데르", "Real Racing Club de Santander": "라싱 산탄데르",
    "SD Eibar": "에이바르", "Granada CF": "그라나다", "Cádiz CF": "카디스", "UD Almería": "알메리아", "Deportivo La Coruña": "데포르티보",
    "RC Deportivo La Coruña": "데포르티보", "Sporting Gijón": "스포르팅 히혼", "Real Sporting de Gijón": "스포르팅 히혼",
    # 분데스리가
    "FC Bayern München": "바이에른 뮌헨", "Borussia Dortmund": "도르트문트", "Bayer 04 Leverkusen": "레버쿠젠",
    "RB Leipzig": "라이프치히", "VfB Stuttgart": "슈투트가르트", "Eintracht Frankfurt": "프랑크푸르트", "SC Freiburg": "프라이부르크",
    "TSG Hoffenheim": "호펜하임", "Borussia Mönchengladbach": "묀헨글라트바흐", "SV Werder Bremen": "베르더 브레멘",
    "1. FSV Mainz 05": "마인츠", "FC Augsburg": "아우크스부르크", "1. FC Union Berlin": "우니온 베를린", "1. FC Köln": "쾰른",
    "Hamburger SV": "함부르크", "FC Schalke 04": "샬케", "SC Paderborn 07": "파더보른", "SV 07 Elversberg": "엘퍼스베르크",
    "VfL Wolfsburg": "볼프스부르크", "FC St. Pauli": "장크트 파울리", "1. FC Heidenheim 1846": "하이덴하임", "VfL Bochum": "보훔",
    "Holstein Kiel": "홀슈타인 킬", "Hertha BSC": "헤르타 베를린", "Fortuna Düsseldorf": "뒤셀도르프",
    "1. FC Kaiserslautern": "카이저슬라우테른", "Hannover 96": "하노버", "1. FC Nürnberg": "뉘른베르크",
    "Karlsruher SC": "카를스루에", "SV Darmstadt 98": "다름슈타트",
}


def ko(name: str) -> str:
    return KO_NAMES.get(name, name)


def season_label(season: int) -> str:
    return f"{season}/{str(season + 1)[-2:]}"


def _get(url: str, headers: dict | None = None, tries: int = 4):
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=headers or {}, timeout=30)
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == tries - 1:
                raise
            print(f"  요청 재시도({attempt + 1}): {e}", flush=True)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"요청 실패: {url}")


def _base(code: str, season: int) -> dict:
    lg = LEAGUES[code]
    return {"league": code, "league_name": lg["name"], "season": season, "season_label": season_label(season)}


def _openligadb(season: int) -> list[dict]:
    data = _get(f"https://api.openligadb.de/getmatchdata/bl1/{season}")
    out = []
    for m in data:
        results = m.get("matchResults") or []
        final = [r for r in results if r.get("resultTypeID") == 2] or results
        ht = [r for r in results if r.get("resultTypeID") == 1]
        finished = bool(m.get("matchIsFinished")) and final
        goals = []
        for g in m.get("goals") or []:
            tags = (" (PK)" if g.get("isPenalty") else "") + (" (자책골)" if g.get("isOwnGoal") else "")
            minute = f" {g['matchMinute']}'" if g.get("matchMinute") else ""
            goals.append(f"{g.get('scoreTeam1')}-{g.get('scoreTeam2')} {g.get('goalGetterName') or '?'}{minute}{tags}")
        out.append({
            **_base("BL1", season),
            "key": f"BL1-{m['matchID']}",
            "matchday": m["group"]["groupOrderID"],
            "kickoff_utc": m.get("matchDateTimeUTC"),
            "home": m["team1"]["teamName"], "away": m["team2"]["teamName"],
            "finished": bool(finished),
            "home_score": final[-1]["pointsTeam1"] if finished else None,
            "away_score": final[-1]["pointsTeam2"] if finished else None,
            "ht_home": ht[-1]["pointsTeam1"] if finished and ht else None,
            "ht_away": ht[-1]["pointsTeam2"] if finished and ht else None,
            "goals": goals,
            "result_source": "https://www.openligadb.de",
        })
    return out


def _football_data(code: str, season: int, token: str) -> list[dict]:
    data = _get(f"https://api.football-data.org/v4/competitions/{code}/matches?season={season}",
                headers={"X-Auth-Token": token})
    out = []
    for m in data.get("matches", []):
        ft = (m.get("score") or {}).get("fullTime") or {}
        ht = (m.get("score") or {}).get("halfTime") or {}
        finished = m.get("status") == "FINISHED" and ft.get("home") is not None
        out.append({
            **_base(code, season),
            "key": f"{code}-{m['id']}",
            "matchday": m.get("matchday") or 0,
            "kickoff_utc": m.get("utcDate"),
            "home": m["homeTeam"]["name"], "away": m["awayTeam"]["name"],
            "finished": finished,
            "home_score": ft.get("home") if finished else None,
            "away_score": ft.get("away") if finished else None,
            "ht_home": ht.get("home") if finished else None,
            "ht_away": ht.get("away") if finished else None,
            "goals": [],
            "result_source": "https://www.football-data.org",
        })
    return out


def fetch_all(codes: list[str], season: int) -> tuple[list[dict], list[str]]:
    """모든 리그의 경기 목록과, 가져오지 못한 리그에 대한 안내를 돌려줍니다."""
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
    matches, notes = [], []
    for i, code in enumerate(codes):
        try:
            if LEAGUES[code]["source"] == "openligadb":
                matches += _openligadb(season)
            elif not token:
                notes.append(f"{LEAGUES[code]['name']}: FOOTBALL_DATA_TOKEN이 없어 건너뜀")
                continue
            else:
                if i:
                    time.sleep(7)  # 무료 요금제는 1분에 10회까지
                matches += _football_data(code, season, token)
        except Exception as e:  # 한 리그가 실패해도 나머지는 진행
            notes.append(f"{LEAGUES[code]['name']}: 경기 목록을 가져오지 못함 ({e})")
    return matches, notes
