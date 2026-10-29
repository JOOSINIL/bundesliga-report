"""수집 노트를 정해진 형식의 경기 기록으로 바꿀 때 쓰는 도구 정의와 정리 함수."""

from __future__ import annotations

import re

LEVELS = ["높음", "중간", "낮음", "확인 안 됨"]
MID_RESULTS = ["우세", "대등", "열세", "확인 안 됨"]
GROUPS = ["GK", "DF", "MF", "FW"]
ABSENCE = ["부상", "징계", "질병", "기타", "미확인"]
EVENT_TYPES = ["골", "슈팅", "큰 기회", "카드", "교체", "VAR", "부상", "전술 변화", "기타"]
INDICATORS = ["attacks", "dangerous_attacks", "shots", "shots_on_target", "possession", "xg"]
PLAYER_STATS = ["minutes", "goals", "assists", "shots", "shots_on_target", "key_passes", "tackles",
                "interceptions", "saves", "rating"]

_nullable_str = {"type": ["string", "null"]}
_nullable_num = {"type": ["number", "null"]}

_player = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "number": {"type": ["integer", "null"]},
        "position": {"type": ["string", "null"], "description": "GK, CB, RB, LB, RWB, LWB, DM, CM, AM, RW, LW, ST 등"},
        "group": {"type": "string", "enum": GROUPS},
    },
    "required": ["name", "group"],
}
_sourced = {
    "type": "object",
    "properties": {
        "value": _nullable_num,
        "source": {"type": ["string", "null"], "description": "이 값을 확인한 출처 URL. 값이 없으면 null"},
        "note": {"type": ["string", "null"], "description": "출처마다 값이 다르면 다른 값과 출처"},
    },
    "required": ["value", "source"],
}
_level = {
    "type": "object",
    "properties": {"level": {"type": "string", "enum": LEVELS}, "detail": {"type": "string"}},
    "required": ["level", "detail"],
}
_team = {
    "type": "object",
    "properties": {
        "formation": {"type": ["string", "null"], "description": "선발 포메이션, 예: 4-2-3-1. 모르면 null"},
        "formation_note": {"type": ["string", "null"], "description": "'(추정)' 여부, 경기 중 대형 변화"},
        "starting_xi": {"type": "array", "items": _player},
        "bench": {"type": "array", "items": _player, "description": "벤치에 등록된 선수 전원"},
        "substitutions": {"type": "array", "items": {
            "type": "object",
            "properties": {"minute": _nullable_str, "player_in": {"type": "string"},
                           "player_out": _nullable_str, "note": _nullable_str},
            "required": ["player_in"]}},
        "absentees": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "reason": {"type": "string", "enum": ABSENCE},
                           "detail": _nullable_str},
            "required": ["name", "reason"]}},
        "pressing": {**_level, "description": "압박의 높이와 강도"},
        "block": {**_level, "description": "수비 블록 높이. '낮음'은 깊게 내려앉았다는 뜻"},
        "midfield": {
            "type": "object",
            "properties": {"result": {"type": "string", "enum": MID_RESULTS}, "detail": {"type": "string"}},
            "required": ["result", "detail"],
            "description": "미들싸움(중원 경합) 결과와 이유"},
        "attack_tactics": {"type": "string"},
        "defense_tactics": {"type": "string"},
        "attack_routes": {
            "type": "object",
            "properties": {"left": _nullable_num, "center": _nullable_num, "right": _nullable_num,
                           "source": _nullable_str, "detail": {"type": "string"}},
            "required": ["detail"],
            "description": "공격 방향 비중(%). 숫자를 찾지 못하면 null, 설명만 쓴다"},
        "indicators": {
            "type": "object",
            "properties": {k: _sourced for k in INDICATORS},
            "required": INDICATORS,
            "description": "attacks=총공격, dangerous_attacks=위험한공격, possession=점유율(%)"},
        "player_stats": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "group": {"type": "string", "enum": GROUPS},
                           **{k: _nullable_num for k in PLAYER_STATS}, "source": _nullable_str},
            "required": ["name", "group"]}},
    },
    "required": ["formation", "starting_xi", "bench", "substitutions", "absentees", "pressing", "block",
                 "midfield", "attack_tactics", "defense_tactics", "attack_routes", "indicators", "player_stats"],
}

MATCH_TOOL = {
    "name": "save_match_record",
    "description": "수집 노트를 정해진 형식의 경기 기록으로 저장한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "home": _team,
            "away": _team,
            "flow_summary": {"type": "string", "description": "경기 전체 흐름 요약 4~6문장"},
            "flow": {"type": "array", "description": "시간대별 흐름. 0-15, 16-30, 31-45+, 46-60, 61-75, 76-90+",
                     "items": {"type": "object", "properties": {
                         "period": {"type": "string"}, "summary": {"type": "string"},
                         "events": {"type": "array", "items": {"type": "object", "properties": {
                             "minute": _nullable_str,
                             "team": {"type": ["string", "null"], "enum": ["home", "away", None]},
                             "type": {"type": "string", "enum": EVENT_TYPES},
                             "description": {"type": "string"}},
                             "required": ["type", "description"]}}},
                         "required": ["period", "summary", "events"]}},
            "tactical_points": {"type": "array", "items": {"type": "string"},
                                "description": "이 경기에서 눈에 띈 전술 포인트 2~4개"},
            "data_gaps": {"type": "array", "items": {"type": "string"},
                          "description": "찾지 못한 자료 (예: 위험한공격 수치, 원정팀 벤치 명단)"},
        },
        "required": ["home", "away", "flow_summary", "flow", "tactical_points", "data_gaps"],
    },
}

SQUAD_TOOL = {
    "name": "save_squad",
    "description": "팀의 이번 시즌 등록 선수단을 저장한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "players": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "number": {"type": ["integer", "null"]},
                               "group": {"type": "string", "enum": GROUPS},
                               "position": _nullable_str, "nationality": _nullable_str, "note": _nullable_str},
                "required": ["name", "group"]}},
            "coach": _nullable_str,
            "sources": {"type": "array", "items": {"type": "string"}, "description": "확인한 출처 URL"},
            "note": _nullable_str,
        },
        "required": ["players", "sources"],
    },
}

WRITE_SYSTEM = """너는 축구 데이터 정리 담당이다. 수집 노트만 근거로 save_match_record 도구를 한 번 호출해 경기 기록을 저장한다.
규칙:
- 노트에 없는 내용은 만들지 않는다. 숫자가 없으면 value와 source를 null로 두고 data_gaps에 적는다.
- 숫자에는 노트에 적힌 출처 URL을 source로 붙인다. 노트의 출처 목록에 있는 URL만 쓴다.
- 선수 이름은 노트에 나온 원래 표기(로마자)를 쓴다. 그 밖의 설명은 한국어 '~다'체로 쓴다.
- 포메이션을 선발 명단으로 판단했다면 formation_note에 '(추정)'이라고 적는다.
- 홈팀은 home, 원정팀은 away에 넣는다. 경기 흐름의 team도 home/away로 적는다."""

SQUAD_SYSTEM = """너는 축구 자료 수집가다. 주어진 팀의 이번 시즌 1군 등록 선수단(등번호, 이름, 포지션)을 웹 검색으로 확인한다.
구단 공식 사이트, 리그 공식 사이트, 위키백과, Transfermarkt 등 최소 두 곳 이상을 비교한다.
확인이 끝나면 save_squad 도구를 한 번 호출해 저장한다. 확인하지 못한 값은 null로 둔다. 설명은 한국어로 쓴다."""


# ------------------------------------------------------------- 정리 ----
def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", "."))
        return float(m.group()) if m else None
    return None


def _str(v, limit=1200):
    if v is None:
        return None
    s = str(v).strip()
    return s[:limit] if s else None


def _url(v):
    s = _str(v, 500)
    return s if s and re.match(r"https?://", s) else None


def _choice(v, options, default):
    return v if v in options else default


def _players(items):
    out = []
    for p in items or []:
        if not isinstance(p, dict) or not _str(p.get("name")):
            continue
        num = _num(p.get("number"))
        out.append({"name": _str(p["name"], 80), "number": int(num) if num is not None else None,
                    "position": _str(p.get("position"), 12), "group": _choice(p.get("group"), GROUPS, "MF")})
    return out


def _team(t: dict) -> dict:
    t = t if isinstance(t, dict) else {}
    ind = t.get("indicators") if isinstance(t.get("indicators"), dict) else {}
    routes = t.get("attack_routes") if isinstance(t.get("attack_routes"), dict) else {}

    def level(x, opts, key):
        x = x if isinstance(x, dict) else {}
        return {key: _choice(x.get(key), opts, "확인 안 됨"), "detail": _str(x.get("detail")) or ""}

    def sourced(x):
        x = x if isinstance(x, dict) else {}
        val, src = _num(x.get("value")), _url(x.get("source"))
        if val is None:
            src = None
        return {"value": val, "source": src, "note": _str(x.get("note"), 300)}

    stats = []
    for p in t.get("player_stats") or []:
        if isinstance(p, dict) and _str(p.get("name")):
            row = {"name": _str(p["name"], 80), "group": _choice(p.get("group"), GROUPS, "MF"),
                   "source": _url(p.get("source"))}
            row.update({k: _num(p.get(k)) for k in PLAYER_STATS})
            stats.append(row)
    stats.sort(key=lambda r: GROUPS.index(r["group"]))

    subs = []
    for s in t.get("substitutions") or []:
        if isinstance(s, dict) and _str(s.get("player_in")):
            subs.append({"minute": _str(s.get("minute"), 10), "player_in": _str(s["player_in"], 80),
                         "player_out": _str(s.get("player_out"), 80), "note": _str(s.get("note"), 200)})
    absent = []
    for a in t.get("absentees") or []:
        if isinstance(a, dict) and _str(a.get("name")):
            absent.append({"name": _str(a["name"], 80), "reason": _choice(a.get("reason"), ABSENCE, "미확인"),
                           "detail": _str(a.get("detail"), 200)})
    return {
        "formation": _str(t.get("formation"), 20),
        "formation_note": _str(t.get("formation_note"), 300),
        "starting_xi": _players(t.get("starting_xi")),
        "bench": _players(t.get("bench")),
        "substitutions": subs,
        "absentees": absent,
        "pressing": level(t.get("pressing"), LEVELS, "level"),
        "block": level(t.get("block"), LEVELS, "level"),
        "midfield": level(t.get("midfield"), MID_RESULTS, "result"),
        "attack_tactics": _str(t.get("attack_tactics")) or "",
        "defense_tactics": _str(t.get("defense_tactics")) or "",
        "attack_routes": {k: _num(routes.get(k)) for k in ("left", "center", "right")}
        | {"source": _url(routes.get("source")), "detail": _str(routes.get("detail")) or ""},
        "indicators": {k: sourced(ind.get(k)) for k in INDICATORS},
        "player_stats": stats,
    }


def normalize_match(raw: dict, m: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    rec = {"home": _team(raw.get("home")), "away": _team(raw.get("away"))}
    # 골은 경기 결과 데이터에서 그대로 가져옵니다.
    rec["home"]["indicators"]["goals"] = {"value": m["home_score"], "source": m.get("result_source"), "note": None}
    rec["away"]["indicators"]["goals"] = {"value": m["away_score"], "source": m.get("result_source"), "note": None}
    flow = []
    for f in raw.get("flow") or []:
        if not isinstance(f, dict):
            continue
        events = []
        for e in f.get("events") or []:
            if isinstance(e, dict) and _str(e.get("description")):
                events.append({"minute": _str(e.get("minute"), 10),
                               "team": e.get("team") if e.get("team") in ("home", "away") else None,
                               "type": _choice(e.get("type"), EVENT_TYPES, "기타"),
                               "description": _str(e["description"], 400)})
        flow.append({"period": _str(f.get("period"), 20) or "", "summary": _str(f.get("summary")) or "",
                     "events": events})
    rec["flow"] = flow
    rec["flow_summary"] = _str(raw.get("flow_summary"), 2000) or ""
    rec["tactical_points"] = [x for x in (_str(p, 400) for p in raw.get("tactical_points") or []) if x][:6]
    rec["data_gaps"] = [x for x in (_str(p, 200) for p in raw.get("data_gaps") or []) if x][:20]
    return rec


def normalize_squad(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    players = []
    for p in raw.get("players") or []:
        if isinstance(p, dict) and _str(p.get("name")):
            num = _num(p.get("number"))
            players.append({"name": _str(p["name"], 80), "number": int(num) if num is not None else None,
                            "group": _choice(p.get("group"), GROUPS, "MF"), "position": _str(p.get("position"), 20),
                            "nationality": _str(p.get("nationality"), 40), "note": _str(p.get("note"), 120)})
    players.sort(key=lambda p: (GROUPS.index(p["group"]), p["number"] if p["number"] is not None else 999))
    return {"players": players, "coach": _str(raw.get("coach"), 80),
            "sources": [u for u in (_url(s) for s in raw.get("sources") or []) if u][:10],
            "note": _str(raw.get("note"), 300)}
