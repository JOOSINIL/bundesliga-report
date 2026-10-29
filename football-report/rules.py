"""자료 수집 규칙.

규칙은 프롬프트로 지시하는 데서 끝나지 않고, check_coverage()가 실제 검색·열람 기록을 검사해서
기준에 못 미치면 run.py가 Claude에게 빠진 범주를 다시 조사하라고 돌려보냅니다.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# ------------------------------------------------------------------ 기준 ----
MIN_SEARCHES = int(os.environ.get("MIN_SEARCHES") or 12)      # 경기당 최소 검색 횟수
MIN_DOMAINS = int(os.environ.get("MIN_DOMAINS") or 8)         # 정보를 가져온 서로 다른 사이트 수
MIN_CATEGORIES = int(os.environ.get("MIN_CATEGORIES") or 6)   # 아래 8개 범주 중 채워야 할 수
REQUIRED_CATEGORIES = [c for c in (os.environ.get("REQUIRED_CATEGORIES") or "D,E,F").split(",") if c]
MAX_FOLLOWUPS = int(os.environ.get("MAX_FOLLOWUPS") or 2)     # 기준 미달 시 다시 조사시키는 횟수

CATEGORIES = {
    "A": "공식 자료 (리그·구단 공식 경기 리포트, 공식 라인업)",
    "B": "현지어 언론",
    "C": "영어권·기타 해외 언론",
    "D": "전술 분석 사이트·개인 블로그",
    "E": "유튜브 영상 (경기 분석·하이라이트·기자회견)",
    "F": "팬 커뮤니티 (Reddit 경기 스레드, 팬 포럼, 국내 커뮤니티)",
    "G": "통계·라이브스코어 사이트",
    "H": "감독·선수 인터뷰, 기자회견 기사",
}

LEAGUE_SOURCES = {
    "PL": {
        "local_lang": "영어",
        "official": "premierleague.com, 구단 공식 사이트",
        "press": "BBC Sport, Sky Sports, The Guardian, The Athletic, The Telegraph, The Times, ESPN UK, Evening Standard",
        "tactics": "Opta Analyst, Total Football Analysis, Coaches' Voice, Spielverlagerung, Between the Posts, Substack·Medium 개인 분석 글, 구단별 팬 블로그(SB Nation 등)",
        "community": "Reddit r/soccer·r/PremierLeague·구단 서브레딧 post-match thread, 에펨코리아·인벤 해외축구 게시판",
    },
    "PD": {
        "local_lang": "스페인어",
        "official": "laliga.com, 구단 공식 사이트",
        "press": "Marca, AS, Mundo Deportivo, Sport, Relevo, El País, El Mundo, Cadena SER, COPE",
        "tactics": "Total Football Analysis, Spielverlagerung, Ecos del Balón, Martí Perarnau(Perarnau Magazine), Substack·Medium·블로그 분석 글",
        "community": "Reddit r/LaLiga·r/soccer·구단 서브레딧 post-match thread, 스페인 팬 포럼, 에펨코리아·인벤 해외축구 게시판",
    },
    "BL1": {
        "local_lang": "독일어",
        "official": "bundesliga.com, 구단 공식 사이트",
        "press": "kicker, Sportschau, Sport1, Spox, Bild, FAZ, Süddeutsche Zeitung, 지역 신문(Ruhr Nachrichten, Abendzeitung 등)",
        "tactics": "Spielverlagerung, Miasanrot, Schwatzgelb, 11Freunde, Total Football Analysis, Substack·Medium·블로그 분석 글",
        "community": "Reddit r/Bundesliga·r/soccer·구단 서브레딧 post-match thread, 독일 팬 포럼, 에펨코리아·인벤 해외축구 게시판",
    },
}

# 주소만 보고 범주를 확실히 알 수 있는 사이트. Claude가 붙인 범주보다 우선합니다.
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "dailymotion.com", "vimeo.com", "tv.naver.com")
_COMMUNITY_HOSTS = ("reddit.com", "fmkorea.com", "inven.co.kr", "dcinside.com", "ruliweb.com", "clien.net",
                    "bigsoccer.com", "redcafe.net", "transfermarkt.de/forum", "forocoches.com", "mediavida.com")
_BLOG_HOSTS = ("substack.com", "medium.com", "wordpress.com", "blogspot.com", "blog.naver.com", "tistory.com",
               "brunch.co.kr", "spielverlagerung", "totalfootballanalysis.com", "theanalyst.com", "coachesvoice.com",
               "betweentheposts.net", "sbnation.com", "miasanrot.de", "schwatzgelb.de", "ecosdelbalon.com",
               "perarnau", "velog.io", "ghost.io")
_STATS_HOSTS = ("fbref.com", "whoscored.com", "sofascore.com", "fotmob.com", "flashscore", "soccerway.com",
                "understat.com", "footystats.org", "aiscore.com", "scores24.live", "livescore.com", "espn.com/soccer/match",
                "transfermarkt", "kicker.de/.*/spieldaten", "besoccer.com", "statmuse.com")


def domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def site_key(url: str) -> str:
    """서로 다른 '사이트'를 셀 때 쓰는 키. 블로그 플랫폼과 유튜브는 작성자(채널·블로그) 단위로 셉니다."""
    d = domain(url)
    path = urlparse(url).path.strip("/").split("/")
    if d.endswith(("substack.com", "blogspot.com", "wordpress.com", "tistory.com")):
        return d
    if d in ("medium.com", "blog.naver.com", "brunch.co.kr", "velog.io") and path and path[0]:
        return f"{d}/{path[0]}"
    if d in ("youtube.com", "m.youtube.com", "youtu.be"):
        return "youtube:" + url  # 영상마다 다른 출처로 셉니다
    if d.endswith("reddit.com") and len(path) >= 2 and path[0] == "r":
        return f"reddit/{path[1].lower()}"
    parts = d.split(".")
    if len(parts) > 2 and parts[-2] in ("co", "com", "org", "ac", "or", "go", "ne"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


def classify(url: str, claimed: str | None) -> str:
    u = url.lower()
    if any(h in u for h in _VIDEO_HOSTS):
        return "E"
    if any(h in u for h in _COMMUNITY_HOSTS):
        return "F"
    if any(h in u for h in _BLOG_HOSTS):
        return "D"
    claimed = (claimed or "").strip().upper()[:1]
    if claimed in CATEGORIES:
        return claimed
    if any(re.search(h, u) for h in _STATS_HOSTS):
        return "G"
    return "C"


def research_system(league_code: str) -> str:
    s = LEAGUE_SOURCES[league_code]
    cats = "\n".join(f"  {k}. {v}" for k, v in CATEGORIES.items())
    return f"""너는 축구 경기 자료 수집가다. 주어진 한 경기에 대해 웹 검색(web_search)과 페이지 열람(web_fetch)으로 자료를 최대한 넓게 모은다.

[가장 중요한 규칙: 넓게 수집하기]
- 통계 사이트 한두 곳을 보고 멈추지 않는다. 아래 8개 범주를 모두 검색해 본다.
{cats}
- 검색은 최소 {MIN_SEARCHES}회 한다. 정보를 가져온 서로 다른 사이트가 최소 {MIN_DOMAINS}곳이어야 한다.
  8개 범주 중 최소 {MIN_CATEGORIES}개에서 실제로 쓴 출처가 있어야 하고, D(블로그)·E(유튜브)·F(커뮤니티)는 반드시 검색한다.
- 검색어는 세 가지 언어로 쓴다: 현지어({s['local_lang']}), 영어, 한국어. 같은 검색어를 표현만 바꿔 반복하지 않는다.
- 이 리그에서 먼저 찾아볼 곳:
  공식: {s['official']}
  언론: {s['press']}
  전술 분석·블로그: {s['tactics']}
  커뮤니티: {s['community']}
- 유튜브: 경기 분석 영상, 하이라이트, 감독 기자회견 영상을 검색한다. 영상은 직접 볼 수 없으므로 검색 결과와 페이지의 제목·설명·요약에서 정보를 얻는다.
- 라인업·경기 리포트·통계·분석 글처럼 중요한 페이지는 web_fetch로 열어 본문을 읽는다. 열 수 있는 주소는 검색 결과에 나온 주소뿐이다.
- 기준을 채우지 못한 채 끝내면 프로그램이 검사해서 다시 조사하라고 돌려보낸다.

[모아야 할 자료]
1. 두 팀 출전 명단: 선발 11명(포지션·등번호), 벤치 명단, 교체 기록(분, 들어간 선수, 나간 선수)
2. 결장 선수: 부상·징계·질병으로 빠진 선수와 사유
3. 포메이션과 경기 중 대형 변화
4. 주요 전술: 압박(높이·강도), 내려앉음(수비 블록 높이), 미들싸움(중원 장악 여부와 이유)
5. 공격 전술, 수비 전술, 공격 루트(왼쪽·중앙·오른쪽 비중, 숫자가 있으면 숫자)
6. 공격 지표: 총공격(Attacks), 위험한공격(Dangerous attacks), 슛, 유효슛, 점유율, xG
7. 포지션별 선수 경기 기록: 출전 시간, 골, 도움, 슛, 유효슛, 키패스, 태클, 가로채기, 선방, 평점
8. 경기 흐름: 시간대별(0-15, 16-30, 31-45+, 46-60, 61-75, 76-90+) 흐름, 주요 장면, 교체·전술 변화가 준 영향
9. 감독·선수 발언 중 전술과 관련된 내용

[정확성]
- 출처에서 확인한 내용만 쓴다. 숫자는 반드시 어느 사이트 값인지 적는다. 사이트마다 값이 다르면 모두 적는다.
- 찾지 못한 항목은 '찾지 못함'이라고 쓰고 지어내지 않는다. 포메이션을 선발 명단으로 판단했다면 '(추정)'을 붙인다.
- 기사 문장을 길게 옮기지 말고 한국어로 요약한다.

[출력 형식]
항목 1~9 순서로 수집 노트를 한국어로 쓴다. 각 내용 뒤에 출처 사이트 이름을 괄호로 붙인다.
맨 끝에 실제로 정보를 가져온 출처 목록을 아래 형식의 json 코드 블록으로 붙인다. 검색만 하고 쓰지 않은 페이지는 넣지 않는다.
```json
[{{"url": "https://...", "category": "A~H 중 하나", "lang": "언어", "used_for": "이 출처에서 가져온 내용"}}]
```"""


def research_prompt(m: dict, extra: str = "") -> str:
    lines = [
        f"리그: {m['league_name']} {m['season_label']} 시즌 {m['matchday']}라운드",
        f"경기: {m['home']} {m['home_score']}:{m['away_score']} {m['away']} (홈: {m['home']})",
        f"킥오프: {m['kickoff_utc']} (UTC)",
    ]
    if m.get("ht_home") is not None:
        lines.append(f"전반 스코어: {m['ht_home']}:{m['ht_away']}")
    if m.get("goals"):
        lines.append("득점 기록: " + "; ".join(m["goals"]))
    if extra:
        lines.append(extra)
    lines.append("\n이 경기의 자료를 규칙대로 넓게 수집해서 수집 노트와 출처 목록을 써줘.")
    return "\n".join(lines)


# ------------------------------------------------------------- 검사 ----
@dataclass
class Coverage:
    searches: int = 0
    fetches: int = 0
    sites: list = field(default_factory=list)          # 정보를 가져온 서로 다른 사이트
    categories: dict = field(default_factory=dict)     # 범주별 출처 수
    sources: list = field(default_factory=list)        # 확인된 출처 목록
    unverified: int = 0                                 # 실제 검색·열람 기록에 없는 주소 수
    log_found: bool = False
    problems: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {"searches": self.searches, "fetches": self.fetches, "sites": len(self.sites),
                "categories": self.categories, "unverified": self.unverified, "problems": self.problems,
                "passed": self.ok}


def parse_source_log(text: str) -> tuple[list[dict], bool]:
    blocks = re.findall(r"```json\s*(\[.*?\])\s*```", text, re.S)
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and isinstance(d.get("url"), str)], True
    return [], False


def strip_source_log(text: str) -> str:
    return re.sub(r"```json\s*\[.*?\]\s*```\s*$", "", text.strip(), flags=re.S).strip()


def check_coverage(seen_urls: set[str], searches: int, fetches: int, text: str) -> Coverage:
    cov = Coverage(searches=searches, fetches=fetches)
    log, cov.log_found = parse_source_log(text)
    seen_sites = {site_key(u) for u in seen_urls}
    seen_domains = {domain(u) for u in seen_urls}
    sites = []
    for item in log:
        url = item["url"].strip()
        if not re.match(r"https?://", url):
            continue
        if url not in seen_urls and site_key(url) not in seen_sites and domain(url) not in seen_domains:
            cov.unverified += 1  # 검색·열람 기록에 없는 주소는 출처로 인정하지 않습니다
            continue
        cat = classify(url, item.get("category"))
        cov.sources.append({"url": url, "category": cat, "lang": str(item.get("lang") or "")[:20],
                            "used_for": str(item.get("used_for") or "")[:200]})
        cov.categories[cat] = cov.categories.get(cat, 0) + 1
        if site_key(url) not in sites:
            sites.append(site_key(url))
    cov.sites = sites

    if not cov.log_found:
        cov.problems.append("출처 목록(json 코드 블록)이 없다")
    if searches < MIN_SEARCHES:
        cov.problems.append(f"검색이 {searches}회뿐이다 (최소 {MIN_SEARCHES}회)")
    if len(sites) < MIN_DOMAINS:
        cov.problems.append(f"정보를 가져온 사이트가 {len(sites)}곳뿐이다 (최소 {MIN_DOMAINS}곳)")
    if len(cov.categories) < MIN_CATEGORIES:
        cov.problems.append(f"출처 범주가 {len(cov.categories)}개뿐이다 (최소 {MIN_CATEGORIES}개)")
    missing_req = [c for c in REQUIRED_CATEGORIES if c not in cov.categories]
    if missing_req:
        cov.problems.append("반드시 들어가야 할 범주가 빠졌다: " + ", ".join(f"{c}({CATEGORIES[c]})" for c in missing_req))
    return cov


def followup_message(cov: Coverage, league_code: str) -> str:
    s = LEAGUE_SOURCES[league_code]
    missing = [f"{k}. {v}" for k, v in CATEGORIES.items() if k not in cov.categories]
    return (
        "[프로그램 점검 결과: 수집 기준 미달]\n"
        + "\n".join(f"- {p}" for p in cov.problems)
        + ("\n아직 출처가 없는 범주:\n" + "\n".join(f"  {m}" for m in missing) if missing else "")
        + (f"\n- 출처 목록에 실제 검색·열람 기록에 없는 주소가 {cov.unverified}개 있어서 제외했다." if cov.unverified else "")
        + "\n\n빠진 범주를 중심으로 새로운 검색어(현지어 "
        + s["local_lang"]
        + "·영어·한국어)로 추가 검색하고, 중요한 페이지는 web_fetch로 열어 확인해라. "
        "블로그는 Substack·Medium·네이버 블로그·티스토리, 유튜브는 경기 분석·하이라이트·기자회견, "
        "커뮤니티는 Reddit post-match thread·국내 해외축구 게시판을 찾아봐라.\n"
        "그다음 이전 내용과 새로 찾은 내용을 합쳐서 수집 노트 전체와 출처 목록(json)을 처음부터 다시 완성해서 출력해라."
    )
