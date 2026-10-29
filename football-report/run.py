"""EPL·라리가·분데스리가 경기 자료 수집기. GitHub Actions에서 매일 실행됩니다.

순서
1. 경기 목록과 결과를 받습니다 (fixtures.py).
2. 최근 경기부터, 남는 예산으로 1라운드부터 지난 경기를 채웁니다.
   경기마다: 넓은 웹 조사(rules.py가 기준 검사) → 정해진 형식의 기록으로 정리 (schema.py)
3. 팀별 시즌 전술 요약, 팀별 등록 선수단을 갱신합니다.
4. 리포트 페이지용 데이터(site/)와 엑셀용 CSV(exports/)를 만듭니다.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import fixtures
import llm
import rules
import schema
import site_build

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
KST = dt.timezone(dt.timedelta(hours=9))
PIPELINE_VERSION = 1

LEAGUE_CODES = [c.strip() for c in (os.environ.get("LEAGUES") or "PL,PD,BL1").split(",") if c.strip() in fixtures.LEAGUES]
MONTHLY_BUDGET = float(os.environ.get("MONTHLY_BUDGET_USD") or 190)
CURRENT_DAYS = int(os.environ.get("CURRENT_DAYS") or 10)          # 이 기간 안의 경기는 '최신'으로 먼저 분석
MAX_RUN_MINUTES = float(os.environ.get("MAX_RUN_MINUTES") or 300)  # 이 시간이 지나면 새 경기를 시작하지 않음
MAX_MATCHES = int(os.environ.get("MAX_MATCHES_PER_RUN") or 0)      # 0 = 제한 없음
SQUAD_REFRESH_DAYS = int(os.environ.get("SQUAD_REFRESH_DAYS") or 60)
DEFAULT_MATCH_COST = 1.0
START = time.time()


def log(*a):
    print(*a, flush=True)


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def current_season() -> int:
    t = now_kst().date()
    return t.year if t.month >= 7 else t.year - 1


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def match_path(m: dict) -> Path:
    return DATA / m["league"] / "matches" / f"{m['key']}.json"


# ----------------------------------------------------------------- 예산 ----
class Budget:
    def __init__(self, state: dict):
        self.state = state
        self.month = now_kst().strftime("%Y-%m")
        state.setdefault("ledger", {})

    @property
    def spent(self) -> float:
        return self.state["ledger"].get(self.month, 0.0)

    def add(self, usd: float):
        self.state["ledger"][self.month] = round(self.spent + usd, 4)

    @property
    def avg(self) -> float:
        costs = self.state.get("recent_match_costs") or []
        return sum(costs) / len(costs) if costs else DEFAULT_MATCH_COST

    def record_match_cost(self, usd: float):
        costs = (self.state.get("recent_match_costs") or []) + [round(usd, 4)]
        self.state["recent_match_costs"] = costs[-30:]

    def reserve_for_current(self) -> float:
        """이번 달 남은 기간의 최신 경기 분석에 쓸 돈. 지난 경기 채우기는 이만큼 남겨두고 합니다."""
        t = now_kst().date()
        days_left = calendar.monthrange(t.year, t.month)[1] - t.day
        per_day = len(LEAGUE_CODES) * 9.7 / 7  # 리그당 일주일에 약 9.7경기
        return self.avg * per_day * days_left

    def allows(self, kind: str, cost: float | None = None) -> bool:
        cost = self.avg if cost is None else cost
        if self.spent + cost > MONTHLY_BUDGET:
            return False
        if kind == "current":
            return True
        return self.spent + cost + self.reserve_for_current() <= MONTHLY_BUDGET


def time_left() -> bool:
    return (time.time() - START) / 60 < MAX_RUN_MINUTES


# ------------------------------------------------------------- 경기 분석 ----
def verified_url(url: str | None, sources: list[dict]) -> str | None:
    if not url:
        return None
    keys = {rules.site_key(s["url"]) for s in sources} | {s["url"] for s in sources}
    return url if (url in keys or rules.site_key(url) in keys) else None


def analyze_match(caller: llm.Caller, m: dict) -> dict:
    res = caller.research(rules.research_system(m["league"]), rules.research_prompt(m), m["league"])
    cov = res["coverage"]
    src_lines = "\n".join(f"- [{s['category']}] {s['url']} : {s['used_for']}" for s in cov.sources) or "- (없음)"
    prompt = (
        f"경기: {m['league_name']} {m['season_label']} {m['matchday']}라운드, {m['kickoff_utc']} UTC\n"
        f"홈팀(home): {m['home']}, 원정팀(away): {m['away']}, 최종 스코어 {m['home_score']}:{m['away_score']}"
        + (f", 전반 {m['ht_home']}:{m['ht_away']}" if m.get("ht_home") is not None else "")
        + f"\n\n수집 노트:\n{res['notes']}\n\n확인된 출처 목록:\n{src_lines}\n\n이 내용으로 경기 기록을 저장해줘."
    )
    w = caller.call_tool(schema.WRITE_SYSTEM, prompt, schema.MATCH_TOOL)
    rec = schema.normalize_match(w["input"], m)
    # 숫자의 출처가 확인된 출처 목록에 없으면 출처 표시를 지웁니다 (값은 남기고 메모를 붙임).
    for side in ("home", "away"):
        for k, v in rec[side]["indicators"].items():
            if k != "goals" and v["value"] is not None and not verified_url(v["source"], cov.sources):
                v["source"] = None
                v["note"] = ((v["note"] or "") + " 출처 주소 확인 안 됨").strip()
        for p in rec[side]["player_stats"]:
            p["source"] = verified_url(p["source"], cov.sources)
    return {
        "v": PIPELINE_VERSION,
        "match": m,
        "record": rec,
        "notes": res["notes"],
        "sources": cov.sources,
        "coverage": cov.to_dict() | {"followups": res["followups"]},
        "truncated": res["truncated"] or w["truncated"],
        "model": llm.MODEL,
        "analyzed_at": now_kst().isoformat(timespec="minutes"),
    }


# ------------------------------------------------------------- 팀 요약 ----
TEAM_SYSTEM = """너는 축구 전술 분석가다. 한 팀의 이번 시즌 경기 기록을 받아 시즌 전술 리포트를 쓴다.
규칙:
- 기록에 있는 내용만 근거로 쓴다. 승패와 득실은 함께 주는 '시즌 기록'을 그대로 따른다.
- 휴대폰으로 읽는 리포트다. 전체 1,200자 안팎, 각 항목 1~2문장. 문장은 '~다'체.
- 한국어로, 아래 형식만 출력한다.

FORMATION: 가장 자주 쓴 기본 포메이션 하나 (예: 4-2-3-1, 모르면 ?)
#### 기본 전술
2~3문장: 주 포메이션과 변형, 팀 스타일
#### 주요 전술 경향
- 압박: 1~2문장
- 내려앉음: 1~2문장
- 미들싸움: 1~2문장
#### 공격할 때
- 2~3개 항목 (주 공격 루트 포함)
#### 수비할 때
- 2~3개 항목
#### 상대별 맞춤 전술
- vs 상대팀 (N라운드): 무엇을 바꿨고 결과는 어땠는지 (확인된 경기만. 없으면 '- 뚜렷한 맞춤 전술은 아직 확인되지 않음')
#### 최근 흐름
1~2문장"""


def team_digest(team: str, docs: list[dict]) -> str:
    w = d = l = gf = ga = 0
    lines = []
    for doc in docs:
        m, r = doc["match"], doc["record"]
        side = "home" if m["home"] == team else "away"
        opp = m["away"] if side == "home" else m["home"]
        f, a = (m["home_score"], m["away_score"]) if side == "home" else (m["away_score"], m["home_score"])
        gf, ga = gf + f, ga + a
        w, d, l = w + (f > a), d + (f == a), l + (f < a)
        t = r[side]
        ind = t["indicators"]
        nums = ", ".join(f"{k} {ind[k]['value']}" for k in ("attacks", "dangerous_attacks", "shots", "shots_on_target",
                                                             "possession") if ind[k]["value"] is not None)
        lines.append(
            f"## {m['matchday']}라운드 {'홈' if side == 'home' else '원정'} vs {opp} {f}:{a}\n"
            f"포메이션 {t['formation'] or '?'} {t['formation_note'] or ''}\n"
            f"압박 {t['pressing']['level']}: {t['pressing']['detail']}\n"
            f"수비 블록 높이 {t['block']['level']}: {t['block']['detail']}\n"
            f"미들싸움 {t['midfield']['result']}: {t['midfield']['detail']}\n"
            f"공격: {t['attack_tactics']}\n수비: {t['defense_tactics']}\n공격 루트: {t['attack_routes']['detail']}\n"
            f"지표: {nums or '없음'}\n흐름: {r['flow_summary']}\n전술 포인트: {' / '.join(r['tactical_points'])}"
        )
    head = f"팀: {team}\n시즌 기록: {len(docs)}경기 {w}승 {d}무 {l}패, {gf}득점 {ga}실점\n\n"
    return head + "\n\n".join(lines) + f"\n\n위 기록으로 {team}의 시즌 전술 리포트를 써줘."


def summarize_team(caller: llm.Caller, team: str, docs: list[dict]) -> dict:
    text = caller.text(TEAM_SYSTEM, team_digest(team, docs), max_tokens=6000)
    formation = "?"
    fm = re.search(r"^\s*FORMATION:\s*(.*)$", text, re.M)
    if fm:
        words = fm.group(1).strip().split()
        formation = words[0] if words else "?"
        text = (text[:fm.start()] + text[fm.end():]).strip("\n")
    return {"formation": formation, "body": text, "based_on": [d["match"]["key"] for d in docs],
            "updated": now_kst().isoformat(timespec="minutes")}


def squad_prompt(team: str, m: dict) -> str:
    return (f"팀: {team} ({m['league_name']}) {m['season_label']} 시즌 1군 등록 선수단을 확인해서 "
            f"save_squad로 저장해줘. 오늘 날짜는 {now_kst():%Y-%m-%d}이다. 최근 이적·임대를 반영한 명단이어야 한다.")


# ----------------------------------------------------------------- 실행 ----
def main():
    season = current_season()
    state = read_json(DATA / "state.json", {})
    if state.get("season") not in (None, season):
        log(f"새 시즌({season})을 시작합니다. 지난 시즌 자료는 data/ 아래에 그대로 남겨 둡니다.")
        state = {"ledger": state.get("ledger", {}), "recent_match_costs": state.get("recent_match_costs", [])}
    state["season"] = season
    budget = Budget(state)

    matches, notes = fixtures.fetch_all(LEAGUE_CODES, season)
    for n in notes:
        log("안내:", n)
    write_json(DATA / "fixtures.json", {"season": season, "fetched_at": now_kst().isoformat(timespec="minutes"),
                                        "notes": notes, "matches": matches})
    finished = [m for m in matches if m["finished"]]
    pending = [m for m in finished if read_json(match_path(m), {}).get("v", 0) < PIPELINE_VERSION]
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CURRENT_DAYS)).isoformat()
    current = sorted((m for m in pending if (m["kickoff_utc"] or "") >= cutoff[:19]), key=lambda m: m["kickoff_utc"],
                     reverse=True)  # 가장 최근 경기부터
    backfill = sorted((m for m in pending if (m["kickoff_utc"] or "") < cutoff[:19]),
                      key=lambda m: (m["matchday"], m["kickoff_utc"]))
    log(f"{fixtures.season_label(season)} 시즌 · 끝난 경기 {len(finished)}개 · 분석 대기 {len(pending)}개 "
        f"(최신 {len(current)}, 지난 경기 {len(backfill)}) · 이번 달 사용 ${budget.spent:.2f}/{MONTHLY_BUDGET:.0f}")

    run = {"started": now_kst().isoformat(timespec="minutes"), "analyzed": 0, "failed": 0, "squads": 0,
           "teams": 0, "stopped": None, "usd": 0.0}
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    caller = llm.Caller(llm.make_client(), llm.Meter()) if have_key else None
    if not have_key:
        run["stopped"] = "ANTHROPIC_API_KEY 없음"
    changed_teams: dict[str, set] = {c: set() for c in LEAGUE_CODES}

    def spend_since(before: float) -> float:
        delta = caller.meter.dollars() - before
        budget.add(delta)
        run["usd"] = round(run["usd"] + delta, 4)
        return delta

    def do_match(m: dict, kind: str) -> bool:
        if not budget.allows(kind):
            run["stopped"] = run["stopped"] or ("월 예산 도달" if kind == "current" else "지난 경기 예산 소진 (최신 경기 몫은 남겨둠)")
            return False
        if not time_left() or (MAX_MATCHES and run["analyzed"] >= MAX_MATCHES):
            run["stopped"] = run["stopped"] or "실행 시간 한도"
            return False
        before = caller.meter.dollars()
        log(f"[{kind}] {m['league_name']} {m['matchday']}R {m['home']} {m['home_score']}:{m['away_score']} {m['away']}")
        try:
            doc = analyze_match(caller, m)
        except llm.OutOfCredits:
            spend_since(before)
            run["stopped"] = "API 크레딧 소진"
            raise
        except Exception as e:  # 한 경기가 실패해도 계속
            spend_since(before)
            run["failed"] += 1
            log(f"  실패: {type(e).__name__}: {e}")
            return True
        cost = spend_since(before)
        doc["cost_usd"] = round(cost, 4)
        write_json(match_path(m), doc)
        budget.record_match_cost(cost)
        write_json(DATA / "state.json", state)
        run["analyzed"] += 1
        changed_teams[m["league"]] |= {m["home"], m["away"]}
        cov = doc["coverage"]
        log(f"  완료 ${cost:.2f} · 검색 {cov['searches']}회 · 사이트 {cov['sites']}곳 · 범주 {len(cov['categories'])}개"
            + (" · 기준 미달" if not cov["passed"] else ""))
        return True

    try:
        if caller:
            for m in current:
                if not do_match(m, "current"):
                    break
            # 등록 선수단: 없거나 오래된 팀부터
            for code in LEAGUE_CODES:
                squads = read_json(DATA / code / "squads.json", {})
                teams = sorted({t for m in matches if m["league"] == code for t in (m["home"], m["away"])})
                ref = next((m for m in matches if m["league"] == code), None)
                for team in teams:
                    upd = (squads.get(team) or {}).get("updated", "")
                    age = (now_kst() - dt.datetime.fromisoformat(upd)).days if upd else 9999
                    if age < SQUAD_REFRESH_DAYS:
                        continue
                    if not budget.allows("backfill", 0.4) or not time_left():
                        break
                    before = caller.meter.dollars()
                    try:
                        out = caller.call_tool(schema.SQUAD_SYSTEM, squad_prompt(team, ref), schema.SQUAD_TOOL,
                                               research=True, searches=6, fetches=4)
                        squads[team] = schema.normalize_squad(out["input"]) | {"updated": now_kst().isoformat(timespec="minutes")}
                        write_json(DATA / code / "squads.json", squads)
                        run["squads"] += 1
                        log(f"  등록 선수단: {team} {len(squads[team]['players'])}명")
                    except llm.OutOfCredits:
                        raise
                    except Exception as e:
                        log(f"  등록 선수단 실패 {team}: {e}")
                    spend_since(before)
                    write_json(DATA / "state.json", state)
            for m in backfill:
                if not do_match(m, "backfill"):
                    break
    except llm.OutOfCredits:
        log("API 크레딧이 떨어져서 멈춥니다. 다음 크레딧이 들어오면 이어서 합니다.")

    # 팀 시즌 요약: 이번에 새 기록이 생긴 팀만
    if caller:
        for code in LEAGUE_CODES:
            teams_doc = read_json(DATA / code / "teams.json", {})
            docs_all = [read_json(p, None) for p in sorted((DATA / code / "matches").glob("*.json"))]
            docs_all = [d for d in docs_all if d and d.get("record")]
            todo = changed_teams[code] | {t for d in docs_all for t in (d["match"]["home"], d["match"]["away"])
                                          if t not in teams_doc}
            for team in sorted(todo):
                docs = sorted((d for d in docs_all if team in (d["match"]["home"], d["match"]["away"])),
                              key=lambda d: d["match"]["kickoff_utc"])
                if not docs or not budget.allows("current", 0.1):
                    continue
                before = caller.meter.dollars()
                try:
                    teams_doc[team] = summarize_team(caller, team, docs)
                    run["teams"] += 1
                except llm.OutOfCredits:
                    spend_since(before)
                    break
                except Exception as e:
                    log(f"  팀 요약 실패 {team}: {e}")
                spend_since(before)
            write_json(DATA / code / "teams.json", teams_doc)

    run["finished"] = now_kst().isoformat(timespec="minutes")
    run["usd"] = round(run["usd"], 2)
    state.setdefault("runs", []).append(run)
    state["runs"] = state["runs"][-60:]
    write_json(DATA / "state.json", state)
    site_build.build(ROOT, LEAGUE_CODES, season, state, MONTHLY_BUDGET, notes)
    log(f"끝: 분석 {run['analyzed']}경기 · 실패 {run['failed']} · 선수단 {run['squads']}팀 · 팀 요약 {run['teams']}팀 · "
        f"이번 실행 ${run['usd']:.2f} · 이번 달 ${budget.spent:.2f}" + (f" · 멈춘 이유: {run['stopped']}" if run["stopped"] else ""))


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("MOCK"):
        sys.exit("ANTHROPIC_API_KEY 가 없습니다. GitHub 저장소의 Secrets에 등록했는지 확인해 주세요.")
    main()
