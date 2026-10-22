"""분데스리가 전술 리포트 생성기.

매 라운드가 끝나면 GitHub Actions에서 실행됩니다.
1. OpenLigaDB(무료)에서 이번 시즌 경기 결과를 가져오고
2. 아직 분석하지 않은 경기를 Claude API(웹 검색)로 분석해 data/matches.json에 쌓고
3. 팀별로 시즌 전술을 종합해 reports/latest.html 을 만듭니다.
Claude 예약 작업이 reports/latest.html 을 가져가 Claude 안의 리포트 페이지를 업데이트합니다.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import markdown
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
STATE_FILE = DATA / "matches.json"
KST = dt.timezone(dt.timedelta(hours=9))

# ---------------------------------------------------------------- 설정 ----
MODEL = os.environ.get("MODEL") or "claude-sonnet-5-5"
MAX_SEARCHES = int(os.environ.get("MAX_SEARCHES") or 5)
WORKERS = int(os.environ.get("WORKERS") or 2)
MAX_NEW_MATCHES = int(os.environ.get("MAX_NEW_MATCHES") or 0)  # 0 = 제한 없음
FORCE_REPORT = (os.environ.get("FORCE_REPORT") or "").lower() in ("1", "true", "yes")
LEAGUE = "bl1"

# 웹 검색 도구: 최신 버전(동적 필터링)을 먼저 쓰고, 거절되면 기본 버전으로 바꿉니다.
SEARCH_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES},
    {"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES},
]

# 1백만 토큰당 달러 (입력, 출력, 캐시 쓰기, 캐시 읽기). 비용 표시는 대략치입니다.
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.10),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
SEARCH_PRICE = 0.01  # 검색 1회당 달러

KO_NAMES = {
    "FC Bayern München": "바이에른 뮌헨",
    "Borussia Dortmund": "도르트문트",
    "Bayer 04 Leverkusen": "레버쿠젠",
    "RB Leipzig": "라이프치히",
    "VfB Stuttgart": "슈투트가르트",
    "Eintracht Frankfurt": "프랑크푸르트",
    "SC Freiburg": "프라이부르크",
    "TSG Hoffenheim": "호펜하임",
    "Borussia Mönchengladbach": "묀헨글라트바흐",
    "SV Werder Bremen": "베르더 브레멘",
    "1. FSV Mainz 05": "마인츠",
    "FC Augsburg": "아우크스부르크",
    "1. FC Union Berlin": "우니온 베를린",
    "1. FC Köln": "쾰른",
    "Hamburger SV": "함부르크",
    "FC Schalke 04": "샬케",
    "SC Paderborn 07": "파더보른",
    "SV 07 Elversberg": "엘퍼스베르크",
    "VfL Wolfsburg": "볼프스부르크",
    "FC St. Pauli": "장크트 파울리",
    "1. FC Heidenheim 1846": "하이덴하임",
    "VfL Bochum": "보훔",
    "Holstein Kiel": "홀슈타인 킬",
    "Hertha BSC": "헤르타 베를린",
    "Fortuna Düsseldorf": "뒤셀도르프",
    "1. FC Kaiserslautern": "카이저슬라우테른",
    "Hannover 96": "하노버",
    "1. FC Nürnberg": "뉘른베르크",
    "Karlsruher SC": "카를스루에",
    "SV Darmstadt 98": "다름슈타트",
}

_print_lock = threading.Lock()
_state_lock = threading.Lock()
_tool_index = 0  # SEARCH_TOOLS 중 현재 쓰는 것


def log(*args):
    with _print_lock:
        print(*args, flush=True)


def current_season(today: dt.date | None = None) -> int:
    today = today or dt.datetime.now(KST).date()
    return today.year if today.month >= 7 else today.year - 1


def ko(name: str) -> str:
    return KO_NAMES.get(name, name)


def slug(name: str) -> str:
    s = name.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"\b(fc|sc|sv|vfb|vfl|tsg|fsv|rb|bsc|04|05|07|96|98|1846|1)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "team"


# ------------------------------------------------------------ 경기 데이터 ----
def fetch_season_matches(season: int) -> list[dict]:
    url = f"https://api.openligadb.de/getmatchdata/{LEAGUE}/{season}"
    for attempt in range(4):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log(f"OpenLigaDB 요청 실패({attempt + 1}/4): {e}")
            time.sleep(5 * (attempt + 1))
    raise SystemExit("OpenLigaDB에서 경기 데이터를 가져오지 못했습니다.")


def final_score(m: dict) -> tuple[int, int] | None:
    results = m.get("matchResults") or []
    final = [r for r in results if r.get("resultTypeID") == 2 or r.get("resultName") == "Endergebnis"]
    pick = final[-1] if final else (max(results, key=lambda r: r.get("resultOrderID", 0)) if results else None)
    if pick and pick.get("pointsTeam1") is not None:
        return int(pick["pointsTeam1"]), int(pick["pointsTeam2"])
    goals = m.get("goals") or []
    if goals:
        return int(goals[-1]["scoreTeam1"]), int(goals[-1]["scoreTeam2"])
    return None


def simplify(m: dict) -> dict | None:
    if not m.get("matchIsFinished"):
        return None
    score = final_score(m)
    if score is None:
        return None
    goals = []
    for g in m.get("goals") or []:
        tags = []
        if g.get("isPenalty"):
            tags.append("PK")
        if g.get("isOwnGoal"):
            tags.append("자책")
        minute = g.get("matchMinute")
        goals.append(
            f"{g.get('scoreTeam1')}-{g.get('scoreTeam2')} {g.get('goalGetterName') or '?'}"
            + (f" {minute}'" if minute else "")
            + (f" ({', '.join(tags)})" if tags else "")
        )
    return {
        "id": m["matchID"],
        "matchday": m["group"]["groupOrderID"],
        "kickoff_utc": m.get("matchDateTimeUTC"),
        "home": m["team1"]["teamName"],
        "away": m["team2"]["teamName"],
        "home_score": score[0],
        "away_score": score[1],
        "goals": goals,
    }


# ------------------------------------------------------------- Claude API ----
class Usage:
    def __init__(self):
        self.input = self.output = self.cache_write = self.cache_read = self.searches = 0
        self._lock = threading.Lock()

    def add(self, u):
        if u is None:
            return
        with self._lock:
            self.input += u.input_tokens or 0
            self.output += u.output_tokens or 0
            self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
            self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
            stu = getattr(u, "server_tool_use", None)
            self.searches += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0

    def dollars(self) -> float:
        pi, po, pw, pr = PRICES.get(MODEL, PRICES["claude-sonnet-5-5"])
        tok = (self.input * pi + self.output * po + self.cache_write * pw + self.cache_read * pr) / 1e6
        return round(tok + self.searches * SEARCH_PRICE, 2)


USAGE = Usage()


def make_client():
    import anthropic

    return anthropic.Anthropic(max_retries=6, timeout=600)


def final_text_and_sources(content) -> tuple[str, list[dict]]:
    """마지막 도구 사용 이후의 텍스트와 그 텍스트가 인용한 출처를 꺼냅니다."""
    parts, sources = [], []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
            for c in getattr(block, "citations", None) or []:
                url = getattr(c, "url", None)
                if url:
                    sources.append({"url": url, "title": getattr(c, "title", None) or url})
        else:
            parts, sources = [], []
    seen, uniq = set(), []
    for s in sources:
        if s["url"] not in seen:
            seen.add(s["url"])
            uniq.append(s)
    return "".join(parts).strip(), uniq


def ask(client, system: str, prompt: str, *, search: bool, max_tokens: int = 4000) -> tuple[str, list[dict]]:
    global _tool_index
    import anthropic

    messages = [{"role": "user", "content": prompt}]
    busy_retries = 0
    while True:
        kwargs = dict(model=MODEL, max_tokens=max_tokens, system=system, messages=messages)
        if search:
            kwargs["tools"] = [SEARCH_TOOLS[_tool_index]]
        try:
            for _ in range(8):  # pause_turn 이어가기
                resp = client.messages.create(**kwargs)
                USAGE.add(resp.usage)
                if resp.stop_reason == "pause_turn":
                    kwargs["messages"] = kwargs["messages"] + [{"role": "assistant", "content": resp.content}]
                    continue
                break
            text, sources = final_text_and_sources(resp.content)
            if not text:
                raise RuntimeError(f"빈 응답 (stop_reason={resp.stop_reason})")
            return text, sources
        except anthropic.BadRequestError as e:
            if search and _tool_index + 1 < len(SEARCH_TOOLS) and "search" in str(e).lower():
                _tool_index += 1
                log(f"웹 검색 도구를 {SEARCH_TOOLS[_tool_index]['type']} 로 바꿔 다시 시도합니다.")
                continue
            raise
        except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
            # 사용량 한도(429)나 서버 혼잡(5xx)은 잠시 쉬었다가 다시 시도합니다.
            busy_retries += 1
            if busy_retries > 4:
                raise
            wait = 60 * busy_retries
            log(f"API가 바빠서 {wait}초 기다립니다: {type(e).__name__}")
            time.sleep(wait)


ANALYSIS_SYSTEM = """너는 분데스리가 전술 분석가다. 주어진 경기를 웹 검색으로 조사해 두 팀의 전술을 정리한다.
찾아볼 자료: 경기 리포트와 라인업(kicker.de, bundesliga.com, sportschau.de, transfermarkt 등), 전술 분석 기사, 감독 인터뷰.
규칙:
- 출처에서 확인한 내용만 쓴다. 확인하지 못한 항목은 '확인되지 않음'이라고 쓴다.
- 기사 문장을 그대로 옮기지 말고 한국어로 짧게 요약해 다시 쓴다. 인용문은 쓰지 않는다.
- 아래 형식만 출력한다. 앞뒤에 인사말이나 설명을 붙이지 않는다.

### {홈팀}
- 포메이션: 선발 대형, 경기 중 바뀌었다면 언제 무엇으로
- 공격 시: 빌드업 방식, 주 공격 루트, 반복된 패턴 (2~3문장)
- 수비 시: 압박 높이와 방식, 수비 블록 대형 (2~3문장)
- 상대 맞춤 대응: 이 상대 때문에 평소와 다르게 한 것 (없으면 '특이사항 없음', 모르면 '확인되지 않음')
- 핵심 선수: 2~3명과 역할
### {원정팀}
(같은 형식)
### 경기 흐름
- 2~3문장: 흐름, 교체·전술 변화가 결과에 준 영향"""

TEAM_SYSTEM = """너는 분데스리가 전술 분석가다. 한 팀의 이번 시즌 경기별 전술 메모를 받아 시즌 전술 리포트를 쓴다.
규칙:
- 메모에 있는 내용만 근거로 쓴다. 메모끼리 다르면 더 자주 나온 쪽을 기본으로 보고 차이를 설명한다.
- 메모에는 상대 팀 내용도 섞여 있다. 요청한 팀에 대해서만 쓴다.
- 한국어로, 아래 형식만 출력한다.

FORMATION: 가장 자주 쓴 기본 포메이션 하나 (예: 4-2-3-1, 모르면 ?)
#### 기본 전술
2~3문장: 주 포메이션과 변형, 팀의 전체적인 스타일
#### 공격할 때
- 3~4개 항목
#### 수비할 때
- 3~4개 항목
#### 상대별 맞춤 전술
- vs 상대팀 (N라운드): 무엇을 바꿨고 결과는 어땠는지
(맞춤 전술이 확인된 경기만. 없으면 '- 뚜렷한 맞춤 전술은 아직 확인되지 않음')
#### 최근 흐름
1~2문장: 가장 최근 경기에서 달라진 점"""

ROUND_SYSTEM = """너는 분데스리가 전술 분석가다. 한 라운드의 경기별 전술 메모를 받아 리그 전체에서 눈에 띈 전술 포인트를 정리한다.
메모에 있는 내용만 쓰고, 한국어로 3~5개 항목의 목록만 출력한다. 각 항목은 1~2문장."""


def match_header(m: dict, season: int) -> str:
    return (
        f"{season}/{str(season + 1)[-2:]} 분데스리가 {m['matchday']}라운드, {m['kickoff_utc'][:10]} "
        f"{m['home']} {m['home_score']}:{m['away_score']} {m['away']}"
    )


def analyze_match(client, m: dict, season: int) -> dict:
    goals = "; ".join(m["goals"]) or "없음"
    prompt = (
        f"{match_header(m, season)}\n득점: {goals}\n\n"
        f"홈팀은 {m['home']}, 원정팀은 {m['away']}다. 이 경기의 두 팀 전술을 정리해줘."
    )
    text, sources = ask(client, ANALYSIS_SYSTEM, prompt, search=True, max_tokens=4000)
    return {"notes": text, "sources": sources[:8], "analyzed_at": dt.datetime.now(KST).isoformat(timespec="minutes")}


def summarize_team(client, team: str, matches: list[dict], season: int) -> dict:
    memos = []
    for m in matches:
        memos.append(f"## {match_header(m, season)}\n{m['notes']}")
    prompt = f"팀: {team}\n\n" + "\n\n".join(memos) + f"\n\n위 메모를 바탕으로 {team}의 시즌 전술 리포트를 써줘."
    text, _ = ask(client, TEAM_SYSTEM, prompt, search=False, max_tokens=3000)
    formation = "?"
    fm = re.match(r"\s*FORMATION:\s*(.+)", text)
    if fm:
        formation = fm.group(1).strip().split()[0] if fm.group(1).strip() else "?"
        text = text[fm.end():].lstrip("\n")
    return {"formation": formation, "body": text, "based_on": [m["id"] for m in matches]}


def summarize_round(client, matches: list[dict], season: int) -> str:
    memos = "\n\n".join(f"## {match_header(m, season)}\n{m['notes']}" for m in matches)
    text, _ = ask(client, ROUND_SYSTEM, memos + "\n\n이 라운드의 전술 포인트를 정리해줘.", search=False, max_tokens=1500)
    return text


# ----------------------------------------------------------------- 상태 ----
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"season": None, "matches": {}, "teams": {}, "rounds": {}, "runs": []}


def save_state(state: dict):
    DATA.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ----------------------------------------------------------------- HTML ----
def safe_md(text: str) -> str:
    """모델 출력은 HTML 태그를 막고 http(s) 링크만 남깁니다."""
    out = markdown.markdown(text.replace("<", "&lt;"), extensions=["sane_lists"])
    out = re.sub(r'<a href="(?!https?://)[^"]*">(.*?)</a>', r"\1", out)
    return out.replace("<a href=", '<a target="_blank" rel="noopener" href=')


def pitch_svg(formation: str) -> str:
    """기본 포메이션을 작은 전술판으로 그립니다 (왼쪽 골키퍼, 오른쪽으로 공격)."""
    nums = [int(x) for x in re.findall(r"\d", formation)] if re.fullmatch(r"\d(-\d){2,4}", formation or "") else []
    if not nums or sum(nums) != 10:
        return ""
    w, h = 150, 100
    dots = [(12, h / 2)]
    lines = len(nums)
    for i, n in enumerate(nums):
        x = 34 + i * (w - 52) / max(lines - 1, 1)
        for j in range(n):
            y = h * (j + 1) / (n + 1)
            dots.append((x, y))
    circles = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.2"/>' for x, y in dots)
    return (
        f'<svg class="pitch" viewBox="0 0 {w} {h}" role="img" aria-label="기본 포메이션 {html.escape(formation)}">'
        f'<rect class="grass" x="0" y="0" width="{w}" height="{h}" rx="4"/>'
        f'<g class="chalk"><rect x="3" y="3" width="{w - 6}" height="{h - 6}" fill="none"/>'
        f'<line x1="{w / 2}" y1="3" x2="{w / 2}" y2="{h - 3}"/><circle cx="{w / 2}" cy="{h / 2}" r="12" fill="none"/>'
        f'<rect x="3" y="{h / 2 - 22}" width="20" height="44" fill="none"/>'
        f'<rect x="{w - 23}" y="{h / 2 - 22}" width="20" height="44" fill="none"/></g>'
        f'<g class="players">{circles}</g></svg>'
    )


CSS = """
/* Layout: one reading column; results and team index first, then one section per team with its base formation drawn as a small tactics board */
:root{
  --bg:#f3f5f1; --surface:#ffffff; --ink:#17201a; --muted:#59675e; --line:#d6ddd5;
  --accent:#2a6a4c; --accent-soft:#e2ede6; --grass:#2f6f50; --chalk:#f4f8f5;
  --win:#2a6a4c; --loss:#a1442f;
  --f-display:"Black Han Sans","Apple SD Gothic Neo","Malgun Gothic",sans-serif;
  --f-body:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif;
  --f-mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0f1511; --surface:#161e19; --ink:#e3eae5; --muted:#98a79e; --line:#28342c;
  --accent:#7cc49e; --accent-soft:#1c2a22; --grass:#1f4d37; --chalk:#e8f1ec;
  --win:#7cc49e; --loss:#e08a72; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0f1511; --surface:#161e19; --ink:#e3eae5; --muted:#98a79e; --line:#28342c;
  --accent:#7cc49e; --accent-soft:#1c2a22; --grass:#1f4d37; --chalk:#e8f1ec;
  --win:#7cc49e; --loss:#e08a72; color-scheme:dark}
body{background:var(--bg);color:var(--ink);font-family:var(--f-body);font-size:15.5px;line-height:1.7;word-break:keep-all;overflow-wrap:break-word;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.wrap{max-width:760px;margin:0 auto;padding-block:28px 64px;display:flex;flex-direction:column;gap:40px}
h1,h2{font-family:var(--f-display);font-weight:400;letter-spacing:.01em;text-wrap:balance;margin:0;line-height:1.2}
h1{font-size:clamp(2rem,7vw,2.8rem)}
h2{font-size:1.55rem}
a{color:var(--accent)}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
.eyebrow{margin:0 0 6px;color:var(--accent);font-weight:600;font-size:.9rem;letter-spacing:.04em}
.meta{margin:10px 0 0;color:var(--muted);font-size:.88rem;font-variant-numeric:tabular-nums}
.sec-title{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.sec-title span{color:var(--muted);font-size:.85rem}
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
table{border-collapse:collapse;width:100%;min-width:340px;font-size:.92rem}
td{padding:9px 12px;border-top:1px solid var(--line);vertical-align:middle}
tr:first-child td{border-top:0}
td.date{color:var(--muted);font-family:var(--f-mono);font-size:.8rem;white-space:nowrap;width:1%}
td.home{text-align:right}
td.score{font-family:var(--f-mono);font-weight:600;text-align:center;white-space:nowrap;width:1%;font-variant-numeric:tabular-nums}
.points{margin-top:14px}
.points ul{margin:0;padding-left:1.2em;display:flex;flex-direction:column;gap:6px}
.index{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.index a{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:9px 12px;border:1px solid var(--line);
  border-radius:8px;background:var(--surface);color:var(--ink);text-decoration:none;font-weight:600;font-size:.92rem;min-width:0}
.index a:hover{border-color:var(--accent)}
.index small{font-family:var(--f-mono);color:var(--muted);font-weight:500;font-size:.78rem;white-space:nowrap}
.team{display:flex;flex-direction:column;gap:14px;padding-top:28px;border-top:2px solid var(--ink)}
.team-head{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
.team-head .name{display:flex;flex-direction:column;gap:6px;min-width:0}
.team-head .de{color:var(--muted);font-size:.85rem}
.form{display:inline-block;font-family:var(--f-mono);font-weight:600;font-size:.85rem;color:var(--accent);
  background:var(--accent-soft);border-radius:999px;padding:2px 10px;width:max-content}
.pitch{width:150px;max-width:100%;height:auto;flex:none}
.pitch .grass{fill:var(--grass)}
.pitch .chalk *{stroke:var(--chalk);stroke-opacity:.55;stroke-width:1}
.pitch .players circle{fill:var(--chalk)}
.record{display:flex;flex-wrap:wrap;gap:6px;font-family:var(--f-mono);font-size:.78rem}
.record span{border:1px solid var(--line);border-radius:6px;padding:1px 7px;color:var(--muted);white-space:nowrap}
.record .w{color:var(--win);border-color:var(--win)}
.record .l{color:var(--loss);border-color:var(--loss)}
.body{min-width:0}
.body h4{margin:18px 0 4px;font-size:.95rem;color:var(--accent)}
.body h4:first-child{margin-top:0}
.body p{margin:0 0 6px;max-width:65ch}
.body ul{margin:0;padding-left:1.2em;max-width:65ch}
.body li{margin:3px 0}
details{font-size:.85rem;color:var(--muted)}
summary{cursor:pointer;width:max-content}
details ul{margin:8px 0 0;padding-left:1.2em;word-break:break-all}
.top{font-size:.85rem;width:max-content}
footer{color:var(--muted);font-size:.82rem;border-top:1px solid var(--line);padding-top:16px}
footer p{margin:0 0 4px}
@media (max-width:480px){td{padding:8px 7px}table{font-size:.86rem}td.date{font-size:.72rem}}
@media (prefers-reduced-motion:no-preference){.index a{transition:border-color .15s}}
"""


def render_html(state: dict, season: int, report_id: str, run_cost: float) -> str:
    matches = sorted(state["matches"].values(), key=lambda m: (m["matchday"], m["kickoff_utc"]))
    done = [m for m in matches if m.get("notes")]
    last_md = max((m["matchday"] for m in done), default=0)
    last_round = [m for m in done if m["matchday"] == last_md]
    teams = sorted(state["teams"].items(), key=lambda kv: ko(kv[0]))
    season_label = f"{season}/{str(season + 1)[-2:]}"
    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    e = html.escape

    rows = []
    for m in last_round:
        d = dt.datetime.fromisoformat(m["kickoff_utc"].replace("Z", "+00:00")).astimezone(KST)
        rows.append(
            f'<tr><td class="date">{d:%m.%d}</td><td class="home">{e(ko(m["home"]))}</td>'
            f'<td class="score">{m["home_score"]} : {m["away_score"]}</td><td>{e(ko(m["away"]))}</td></tr>'
        )
    round_points = state["rounds"].get(str(last_md), {}).get("body", "")

    index = "".join(
        f'<a href="#{slug(t)}"><span>{e(ko(t))}</span><small>{e(info["formation"])}</small></a>' for t, info in teams
    )

    sections = []
    for t, info in teams:
        tm = [m for m in done if t in (m["home"], m["away"])]
        chips = []
        for m in tm:
            home = m["home"] == t
            gf, ga = (m["home_score"], m["away_score"]) if home else (m["away_score"], m["home_score"])
            cls = "w" if gf > ga else "l" if gf < ga else ""
            opp = ko(m["away"] if home else m["home"])
            chips.append(f'<span class="{cls}" title="{m["matchday"]}라운드">{gf}:{ga} {e(opp)}</span>')
        srcs, seen = [], set()
        for m in reversed(tm):
            for s in m.get("sources", []):
                if s["url"] not in seen and re.match(r"https?://", s["url"]):
                    seen.add(s["url"])
                    srcs.append(f'<li><a target="_blank" rel="noopener" href="{e(s["url"])}">{e(s["title"])}</a></li>')
        sections.append(
            f'<section class="team" id="{slug(t)}">'
            f'<div class="team-head"><div class="name"><h2>{e(ko(t))}</h2><span class="de">{e(t)}</span>'
            f'<span class="form">{e(info["formation"])}</span></div>{pitch_svg(info["formation"])}</div>'
            f'<div class="record">{"".join(chips)}</div>'
            f'<div class="body">{safe_md(info["body"])}</div>'
            + (f'<details><summary>출처 {len(srcs[:12])}개</summary><ul>{"".join(srcs[:12])}</ul></details>' if srcs else "")
            + '<a class="top" href="#teams">↑ 팀 목록</a></section>'
        )

    return f"""<title>분데스리가 전술 리포트</title>
<meta name="claude:viewport-fit-sides" content="cover">
<meta name="report-id" content="{e(report_id)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Mono:wght@500;600&family=IBM+Plex+Sans+KR:wght@400;600&display=swap">
<style>{CSS}</style>
<main class="wrap">
<header>
<p class="eyebrow">{season_label} 시즌 · 1–{last_md}라운드 반영</p>
<h1>분데스리가 전술 리포트</h1>
<p class="meta">업데이트 {now} KST · 분석한 경기 {len(done)}개 · 이번 업데이트 API 비용 약 ${run_cost:.2f}</p>
</header>
<section id="latest">
<div class="sec-title"><h2>{last_md}라운드 결과</h2><span>한국 시간 기준</span></div>
<div class="tbl"><table>{"".join(rows)}</table></div>
{f'<div class="points body"><h4>이번 라운드 전술 포인트</h4>{safe_md(round_points)}</div>' if round_points else ""}
</section>
<section id="teams">
<div class="sec-title"><h2>팀별 전술</h2><span>팀 이름 · 기본 포메이션</span></div>
<nav class="index" aria-label="팀 목록">{index}</nav>
</section>
{"".join(sections)}
<footer>
<p>경기 결과: OpenLigaDB (ODbL). 전술 분석: Claude API가 경기 리포트와 분석 기사를 웹에서 찾아 요약한 내용입니다.</p>
<p>자료에서 확인하지 못한 부분은 '확인되지 않음'으로 표시했습니다. 각 팀의 출처 목록에서 원문을 볼 수 있습니다.</p>
</footer>
</main>
"""


# ----------------------------------------------------------------- 실행 ----
def main():
    season = current_season()
    state = load_state()
    if state.get("season") not in (None, season):
        archive = DATA / f"matches_{state['season']}.json"
        STATE_FILE.replace(archive)
        log(f"새 시즌 시작: 지난 시즌 데이터를 {archive.name} 로 옮겼습니다.")
        state = load_state()
    state["season"] = season

    raw = fetch_season_matches(season)
    finished = [s for s in (simplify(m) for m in raw) if s]
    for m in finished:
        key = str(m["id"])
        old = state["matches"].get(key, {})
        state["matches"][key] = {**m, **{k: old[k] for k in ("notes", "sources", "analyzed_at") if k in old}}
    todo = [m for m in state["matches"].values() if not m.get("notes")]
    todo.sort(key=lambda m: m["kickoff_utc"])
    if MAX_NEW_MATCHES:
        todo = todo[:MAX_NEW_MATCHES]
    log(f"{season}/{season + 1} 시즌: 끝난 경기 {len(finished)}개, 새로 분석할 경기 {len(todo)}개")

    if not todo and not FORCE_REPORT:
        log("새 경기가 없어 리포트를 그대로 둡니다.")
        save_state(state)
        return

    client = make_client()
    changed_teams: set[str] = set()
    failed = 0

    def work(m):
        return m, analyze_match(client, m, season)

    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(work, m) for m in todo]
        for f in cf.as_completed(futures):
            try:
                m, res = f.result()
            except Exception as ex:  # 한 경기가 실패해도 나머지는 계속
                failed += 1
                log(f"분석 실패: {ex}")
                continue
            with _state_lock:
                state["matches"][str(m["id"])].update(res)
                changed_teams.update((m["home"], m["away"]))
                save_state(state)
            log(f"분석 완료: {m['matchday']}R {m['home']} {m['home_score']}:{m['away_score']} {m['away']}")

    done = [m for m in state["matches"].values() if m.get("notes")]
    all_teams = {t for m in done for t in (m["home"], m["away"])}
    if FORCE_REPORT:
        changed_teams |= all_teams
    changed_teams |= {t for t in all_teams if t not in state["teams"]}

    def team_job(t):
        tm = sorted((m for m in done if t in (m["home"], m["away"])), key=lambda m: m["kickoff_utc"])
        return t, summarize_team(client, t, tm, season)

    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for f in cf.as_completed([pool.submit(team_job, t) for t in sorted(changed_teams & all_teams)]):
            try:
                t, res = f.result()
                state["teams"][t] = res
                log(f"팀 종합 완료: {t}")
            except Exception as ex:
                failed += 1
                log(f"팀 종합 실패: {ex}")
    save_state(state)

    if done:
        last_md = max(m["matchday"] for m in done)
        round_matches = sorted((m for m in done if m["matchday"] == last_md), key=lambda m: m["kickoff_utc"])
        ids = [m["id"] for m in round_matches]
        if state["rounds"].get(str(last_md), {}).get("based_on") != ids or FORCE_REPORT:
            try:
                state["rounds"][str(last_md)] = {"body": summarize_round(client, round_matches, season), "based_on": ids}
            except Exception as ex:
                log(f"라운드 요약 실패: {ex}")

    if not state["teams"]:
        save_state(state)
        raise SystemExit("분석된 팀이 없어 리포트를 만들지 못했습니다.")

    run_cost = USAGE.dollars()
    report_id = dt.datetime.now(KST).strftime("%Y%m%d-%H%M")
    page = render_html(state, season, report_id, run_cost)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "latest.html").write_text(page, encoding="utf-8")
    last_md = max(m["matchday"] for m in done)
    (REPORTS / "archive").mkdir(exist_ok=True)
    (REPORTS / "archive" / f"{season}-md{last_md:02d}.html").write_text(page, encoding="utf-8")
    meta = {
        "report_id": report_id,
        "season": season,
        "latest_matchday": last_md,
        "matches_analyzed": len(done),
        "generated_at_kst": dt.datetime.now(KST).isoformat(timespec="minutes"),
        "run_cost_usd": run_cost,
    }
    (REPORTS / "latest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    state["runs"].append({**meta, "new_matches": len(todo) - failed, "failed": failed,
                          "tokens_in": USAGE.input, "tokens_out": USAGE.output, "searches": USAGE.searches})
    save_state(state)
    log(f"리포트 완료: {last_md}라운드까지, 이번 실행 비용 약 ${run_cost:.2f} (검색 {USAGE.searches}회)")
    if failed:
        log(f"실패 {failed}건은 다음 실행 때 다시 시도합니다.")


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("MOCK"):
        sys.exit("ANTHROPIC_API_KEY 가 없습니다. GitHub 저장소의 Secrets에 등록했는지 확인해 주세요.")
    main()
