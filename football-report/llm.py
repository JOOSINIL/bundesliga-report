"""Claude API 호출: 웹 조사(검색·열람), 기록 정리(도구 강제 호출), 팀 요약. 비용도 여기서 셉니다."""

from __future__ import annotations

import os
import threading
import time

import rules

MODEL = os.environ.get("MODEL") or "claude-sonnet-5-5"
MAX_SEARCHES_PER_TURN = int(os.environ.get("MAX_SEARCHES_PER_TURN") or 16)
MAX_FETCHES_PER_TURN = int(os.environ.get("MAX_FETCHES_PER_TURN") or 6)

# 1백만 토큰당 달러 (입력, 출력, 캐시 쓰기, 캐시 읽기). 화면에 보이는 비용은 대략치입니다.
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.10),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
SEARCH_PRICE = 0.01


def toolsets(searches: int, fetches: int) -> list[list[dict]]:
    """최신 도구(동적 필터링)를 먼저 쓰고, 거절되면 기본 버전으로 내려갑니다."""
    return [
        [{"type": "web_search_20260209", "name": "web_search", "max_uses": searches},
         {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": fetches, "max_content_tokens": 12000}],
        [{"type": "web_search_20250305", "name": "web_search", "max_uses": searches},
         {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": fetches, "max_content_tokens": 12000}],
        [{"type": "web_search_20250305", "name": "web_search", "max_uses": searches}],
    ]


class OutOfCredits(Exception):
    """API 크레딧이 떨어졌습니다. 다음 달 크레딧이 들어오면 이어서 합니다."""


class Meter:
    """요청 하나하나의 사용량을 모아 달러로 바꿉니다."""

    def __init__(self):
        self.input = self.output = self.cache_write = self.cache_read = 0
        self.searches = self.fetches = 0
        self._lock = threading.Lock()

    def add(self, u):
        if u is None:
            return
        g = lambda o, k: (getattr(o, k, 0) if not isinstance(o, dict) else o.get(k, 0)) or 0  # noqa: E731
        with self._lock:
            self.input += g(u, "input_tokens")
            self.output += g(u, "output_tokens")
            self.cache_write += g(u, "cache_creation_input_tokens")
            self.cache_read += g(u, "cache_read_input_tokens")
            stu = getattr(u, "server_tool_use", None) if not isinstance(u, dict) else u.get("server_tool_use")
            if stu is not None:
                self.searches += g(stu, "web_search_requests")
                self.fetches += g(stu, "web_fetch_requests")

    def dollars(self) -> float:
        pi, po, pw, pr = PRICES.get(MODEL, PRICES["claude-sonnet-5-5"])
        tok = (self.input * pi + self.output * po + self.cache_write * pw + self.cache_read * pr) / 1e6
        return round(tok + self.searches * SEARCH_PRICE, 4)

    def snapshot(self) -> dict:
        return {"input": self.input, "output": self.output, "searches": self.searches,
                "fetches": self.fetches, "usd": round(self.dollars(), 4)}


def make_client():
    import anthropic

    return anthropic.Anthropic(max_retries=8, timeout=900)


def _g(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def final_text(blocks) -> str:
    parts = []
    for b in blocks:
        if _g(b, "type") == "text":
            parts.append(_g(b, "text") or "")
        else:
            parts = []
    return "".join(parts).strip()


def seen_urls(blocks) -> set[str]:
    """Claude가 실제로 본 검색 결과·열람 페이지·인용 주소."""
    urls = set()
    for b in blocks:
        t = _g(b, "type")
        if t == "web_search_tool_result":
            res = _g(b, "content")
            if isinstance(res, list):
                urls |= {_g(r, "url") for r in res if _g(r, "url")}
        elif t == "web_fetch_tool_result":
            res = _g(b, "content")
            if _g(res, "url"):
                urls.add(_g(res, "url"))
        elif t == "text":
            for c in _g(b, "citations") or []:
                if _g(c, "url"):
                    urls.add(_g(c, "url"))
    return urls


def tool_counts(blocks) -> tuple[int, int]:
    s = f = 0
    for b in blocks:
        if _g(b, "type") == "server_tool_use":
            name = _g(b, "name")
            s += name == "web_search"
            f += name == "web_fetch"
    return s, f


class Caller:
    def __init__(self, client, meter: Meter):
        self.client = client
        self.meter = meter
        self.tool_level = 0

    def _create(self, **kwargs):
        import anthropic

        busy = 0
        while True:
            try:
                resp = self.client.messages.create(model=MODEL, **kwargs)
                self.meter.add(resp.usage)
                return resp
            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as e:
                busy += 1
                if busy > 5:
                    raise
                wait = 60 * busy
                print(f"  API가 바빠서 {wait}초 기다립니다: {type(e).__name__}", flush=True)
                time.sleep(wait)
            except (anthropic.BadRequestError, anthropic.PermissionDeniedError) as e:
                if "credit" in str(e).lower() and "balance" in str(e).lower():
                    raise OutOfCredits(str(e)) from e
                raise

    def _turn(self, system, messages, tools, max_tokens):
        """pause_turn을 이어 붙여 한 번의 답(어시스턴트 차례)을 끝까지 받습니다."""
        blocks, stop = [], None
        for _ in range(12):
            msgs = messages + ([{"role": "assistant", "content": blocks}] if blocks else [])
            resp = self._create(system=system, messages=msgs, tools=tools, max_tokens=max_tokens)
            blocks.extend(resp.content)
            stop = resp.stop_reason
            if stop != "pause_turn":
                break
        return blocks, stop

    def research(self, system: str, prompt: str, league: str, *, searches=None, fetches=None,
                 enforce=True, max_tokens=16000) -> dict:
        """규칙대로 넓게 조사하고, 기준 미달이면 빠진 범주를 다시 조사시킵니다."""
        import anthropic

        searches = searches or MAX_SEARCHES_PER_TURN
        fetches = fetches or MAX_FETCHES_PER_TURN
        messages = [{"role": "user", "content": prompt}]
        all_blocks, followups, cov = [], 0, None
        searches0, fetches0 = self.meter.searches, self.meter.fetches
        while True:
            try:
                blocks, stop = self._turn(system, messages, toolsets(searches, fetches)[self.tool_level], max_tokens)
            except anthropic.BadRequestError as e:
                if self.tool_level + 1 < len(toolsets(1, 1)):
                    self.tool_level += 1
                    print(f"  도구 요청이 거절돼서 한 단계 낮은 도구로 바꿉니다: {str(e)[:160]}", flush=True)
                    continue
                raise
            all_blocks.extend(blocks)
            text = final_text(blocks)
            s, f = tool_counts(all_blocks)
            # 응답에 담긴 도구 기록과 요금 기록 중 큰 값을 씁니다 (동적 필터링에서는 한쪽만 보일 수 있음).
            s = max(s, self.meter.searches - searches0)
            f = max(f, self.meter.fetches - fetches0)
            cov = rules.check_coverage(seen_urls(all_blocks), s, f, text)
            if not enforce or cov.ok or followups >= rules.MAX_FOLLOWUPS:
                break
            followups += 1
            print(f"  수집 기준 미달 → 추가 조사 {followups}회차: {'; '.join(cov.problems)}", flush=True)
            messages = messages + [{"role": "assistant", "content": blocks},
                                   {"role": "user", "content": rules.followup_message(cov, league)}]
        if not text:
            raise RuntimeError(f"조사 결과가 비었습니다 (stop_reason={stop})")
        return {"notes": rules.strip_source_log(text), "coverage": cov, "followups": followups,
                "truncated": stop == "max_tokens"}

    def call_tool(self, system: str, prompt: str, tool: dict, *, research=False, searches=6, fetches=4,
                  max_tokens=16000) -> dict:
        """도구를 반드시 한 번 호출하게 해서 정해진 형식의 결과를 받습니다."""
        import anthropic

        for attempt in range(2):
            tools = [tool]
            kwargs = {}
            if research:
                tools = toolsets(searches, fetches)[self.tool_level] + [tool]
                kwargs["tool_choice"] = {"type": "auto"}
            else:
                kwargs["tool_choice"] = {"type": "tool", "name": tool["name"]}
            messages = [{"role": "user", "content": prompt}]
            try:
                blocks, stop = self._turn_with(system, messages, tools, max_tokens, kwargs)
            except anthropic.BadRequestError as e:
                if research and self.tool_level + 1 < len(toolsets(1, 1)):
                    self.tool_level += 1
                    print(f"  도구 요청이 거절돼서 한 단계 낮은 도구로 바꿉니다: {str(e)[:160]}", flush=True)
                    continue
                raise
            for b in blocks:
                if _g(b, "type") == "tool_use" and _g(b, "name") == tool["name"]:
                    return {"input": _g(b, "input") or {}, "urls": seen_urls(blocks), "truncated": stop == "max_tokens"}
            print(f"  {tool['name']} 호출이 없어 다시 시도합니다 (stop_reason={stop})", flush=True)
        raise RuntimeError(f"{tool['name']} 결과를 받지 못했습니다")

    def _turn_with(self, system, messages, tools, max_tokens, extra):
        blocks, stop = [], None
        for _ in range(12):
            msgs = messages + ([{"role": "assistant", "content": blocks}] if blocks else [])
            resp = self._create(system=system, messages=msgs, tools=tools, max_tokens=max_tokens, **extra)
            blocks.extend(resp.content)
            stop = resp.stop_reason
            if stop != "pause_turn":
                break
        return blocks, stop

    def text(self, system: str, prompt: str, max_tokens=6000) -> str:
        resp = self._create(system=system, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens)
        out = final_text(resp.content)
        if resp.stop_reason == "max_tokens":
            resp = self._create(system=system, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens * 2)
            out = final_text(resp.content)
        return out
