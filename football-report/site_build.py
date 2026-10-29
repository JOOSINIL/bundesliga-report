"""리포트 페이지용 데이터(site/data)와 엑셀용 CSV(exports/)를 만듭니다.

페이지는 리그·라운드별로 나뉜 JSON 파일을 필요할 때 불러옵니다. 바뀐 파일만 다시 게시하면 되도록
site/manifest.json에 파일별 해시를 적어 둡니다.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import markdown

import fixtures

KST = dt.timezone(dt.timedelta(hours=9))


def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if not p.exists() or p.read_text(encoding="utf-8") != text:
        p.write_text(text, encoding="utf-8")


def safe_html(md_text: str) -> str:
    """모델이 쓴 마크다운을 HTML로 바꿉니다. 태그는 막고 http(s) 링크만 남깁니다."""
    out = markdown.markdown((md_text or "").replace("<", "&lt;"), extensions=["sane_lists"])
    out = re.sub(r'<a href="(?!https?://)[^"]*">(.*?)</a>', r"\1", out)
    return out.replace("<a href=", '<a target="_blank" rel="noopener" href=')


def _public_match(m: dict) -> dict:
    keep = ("key", "league", "matchday", "kickoff_utc", "home", "away", "home_score", "away_score", "ht_home", "ht_away",
            "goals")
    return {k: m.get(k) for k in keep} | {"home_ko": fixtures.ko(m["home"]), "away_ko": fixtures.ko(m["away"])}


def build(root: Path, codes: list[str], season: int, state: dict, monthly_budget: float, notes: list[str]) -> None:
    data, site, exports = root / "data", root / "site", root / "exports"
    fx = _read(data / "fixtures.json", {"matches": []})
    leagues_index = []
    for code in codes:
        lg = fixtures.LEAGUES[code]
        all_m = [m for m in fx["matches"] if m["league"] == code]
        finished = [m for m in all_m if m["finished"]]
        docs = {}
        for p in (data / code / "matches").glob("*.json"):
            d = _read(p, None)
            if d and d.get("record") and d["match"].get("season", season) == season:
                docs[d["match"]["key"]] = d

        # 라운드별 파일
        rounds = []
        by_md: dict[int, list] = {}
        for m in finished:
            by_md.setdefault(m["matchday"], []).append(m)
        for md, ms in sorted(by_md.items()):
            items = []
            for m in sorted(ms, key=lambda x: x["kickoff_utc"] or ""):
                d = docs.get(m["key"])
                item = {"match": _public_match(m), "record": None}
                if d:
                    item.update({"record": d["record"], "sources": d["sources"], "coverage": d["coverage"],
                                 "analyzed_at": d["analyzed_at"]})
                items.append(item)
            _write(site / "data" / code / f"md{md:02d}.json", {"league": code, "matchday": md, "matches": items})
            rounds.append({"md": md, "count": len(ms), "analyzed": sum(1 for x in items if x["record"])})

        # 팀 파일
        teams_doc = _read(data / code / "teams.json", {})
        squads = _read(data / code / "squads.json", {})
        names = sorted({t for m in all_m for t in (m["home"], m["away"])}, key=fixtures.ko)
        teams = []
        for t in names:
            games = []
            for m in sorted((m for m in finished if t in (m["home"], m["away"])), key=lambda x: x["kickoff_utc"] or ""):
                home = m["home"] == t
                gf, ga = (m["home_score"], m["away_score"]) if home else (m["away_score"], m["home_score"])
                games.append({"key": m["key"], "md": m["matchday"], "opp": fixtures.ko(m["away"] if home else m["home"]),
                              "ha": "H" if home else "A", "gf": gf, "ga": ga, "analyzed": m["key"] in docs})
            info = teams_doc.get(t) or {}
            sq = squads.get(t) or {}
            teams.append({"name": t, "ko": fixtures.ko(t), "formation": info.get("formation") or "?",
                          "summary_html": safe_html(info.get("body", "")) if info.get("body") else "",
                          "summary_updated": info.get("updated"), "games": games,
                          "squad": {"players": sq.get("players", []), "coach": sq.get("coach"),
                                    "sources": sq.get("sources", []), "updated": sq.get("updated")}})
        _write(site / "data" / code / "teams.json", {"league": code, "teams": teams})

        leagues_index.append({
            "code": code, "name": lg["name"], "season_label": fixtures.season_label(season),
            "finished": len(finished), "analyzed": sum(1 for m in finished if m["key"] in docs),
            "latest_md": max((r["md"] for r in rounds), default=0), "rounds": rounds,
            "note": next((n for n in notes if n.startswith(lg["name"])), None),
        })
        _export_csv(exports, code, season, finished, docs)

    month = dt.datetime.now(KST).strftime("%Y-%m")
    last_run = (state.get("runs") or [{}])[-1]
    _write(site / "data" / "index.json", {
        "generated_at": dt.datetime.now(KST).isoformat(timespec="minutes"),
        "season_label": fixtures.season_label(season),
        "leagues": leagues_index,
        "budget": {"month": month, "spent": round(state.get("ledger", {}).get(month, 0.0), 2), "limit": monthly_budget},
        "last_run": {k: last_run.get(k) for k in ("finished", "analyzed", "failed", "usd", "stopped")},
    })
    _manifest(site)


def _manifest(site: Path) -> None:
    files = {}
    for p in sorted(site.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            files[p.relative_to(site).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    (site / "manifest.json").write_text(json.dumps({"files": files}, indent=1), encoding="utf-8")


def _export_csv(exports: Path, code: str, season: int, finished: list[dict], docs: dict) -> None:
    exports.mkdir(parents=True, exist_ok=True)
    mrows, prows, lrows = [], [], []
    for m in sorted(finished, key=lambda x: x["kickoff_utc"] or ""):
        d = docs.get(m["key"])
        if not d:
            continue
        r = d["record"]
        for side in ("home", "away"):
            t, opp = r[side], ("away" if side == "home" else "home")
            ind = t["indicators"]
            gf, ga = (m["home_score"], m["away_score"]) if side == "home" else (m["away_score"], m["home_score"])
            mrows.append({
                "시즌": fixtures.season_label(season), "라운드": m["matchday"], "날짜(UTC)": (m["kickoff_utc"] or "")[:10],
                "팀": m[side], "상대": m[opp], "홈/원정": "홈" if side == "home" else "원정", "득점": gf, "실점": ga,
                "포메이션": t["formation"] or "", "압박": t["pressing"]["level"], "내려앉음(블록 높이)": t["block"]["level"],
                "미들싸움": t["midfield"]["result"], "총공격": ind["attacks"]["value"], "위험한공격": ind["dangerous_attacks"]["value"],
                "슛": ind["shots"]["value"], "유효슛": ind["shots_on_target"]["value"], "점유율": ind["possession"]["value"],
                "xG": ind["xg"]["value"], "공격 루트 왼쪽%": t["attack_routes"]["left"], "중앙%": t["attack_routes"]["center"],
                "오른쪽%": t["attack_routes"]["right"], "공격 전술": t["attack_tactics"], "수비 전술": t["defense_tactics"],
                "출처 사이트 수": d["coverage"].get("sites"), "경기 ID": m["key"],
            })
            for p in t["player_stats"]:
                prows.append({"라운드": m["matchday"], "날짜(UTC)": (m["kickoff_utc"] or "")[:10], "팀": m[side], "상대": m[opp],
                              "선수": p["name"], "포지션": p["group"], "출전(분)": p["minutes"], "골": p["goals"],
                              "도움": p["assists"], "슛": p["shots"], "유효슛": p["shots_on_target"], "키패스": p["key_passes"],
                              "태클": p["tackles"], "가로채기": p["interceptions"], "선방": p["saves"], "평점": p["rating"],
                              "출처": p["source"] or "", "경기 ID": m["key"]})
            for role, plist in (("선발", t["starting_xi"]), ("벤치", t["bench"])):
                for p in plist:
                    lrows.append({"라운드": m["matchday"], "팀": m[side], "상대": m[opp], "구분": role, "선수": p["name"],
                                  "등번호": p["number"], "포지션": p["position"] or p["group"], "경기 ID": m["key"]})
            for a in t["absentees"]:
                lrows.append({"라운드": m["matchday"], "팀": m[side], "상대": m[opp], "구분": f"결장({a['reason']})",
                              "선수": a["name"], "등번호": None, "포지션": a["detail"] or "", "경기 ID": m["key"]})
    for name, rows in (("matches", mrows), ("players", prows), ("lineups", lrows)):
        path = exports / f"{code}_{name}.csv"
        if not rows:
            continue
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
