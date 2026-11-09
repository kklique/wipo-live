#!/usr/bin/env python3
"""회의 시간대에 주기적으로 실행되어 '저장 누락'을 막는 감시기. (표준 라이브러리 + gh CLI)

하는 일 (아젠더 날짜/시간대 안에서만 동작)
  1) webcast liveSessions.json 에서 진행 중인 세션의 meeting_id 를 찾고, 설정된 기본 id 와 합쳐 후보를 만든다.
  2) 후보마다 WIPO 전사 API 를 저장된 위치(state.json)부터 조회해 '아직 저장 안 된 새 문장'이 있는지 본다.
  3) 새 문장이 있는데 수집 작업이 없으면 -> 수집 워크플로를 자동으로 시작한다.
     또한 회의 시간대(시작 20분 전 ~ 종료 30분 후) 동안에는 수집 작업이 항상 하나 떠 있도록 한다:
     전사가 아직 없거나(점심 휴회, 세션 시작 전) 다음 세션을 기다리는 중이어도 대기용 수집 작업을 미리 시작해 두므로,
     오후 세션의 첫 문장부터 저장된다. (수집기는 전사가 나올 때까지 기다리며, 시간대가 끝나면 자동 종료)
     수집 작업이 떠 있는데 12분 넘게 저장 위치가 갱신되지 않으면 -> 취소하고 다시 시작한다.
  4) 이상 징후(러너에서 WIPO 접속 불가, 수집 작업이 반복 실패)는 GitHub 이슈로 알린다 (중복 방지).
두 스트림(<해시>_no_delay, <해시>)은 각각 독립적으로 수집한다: 한쪽이 멈춰도 다른 쪽에 저장된다.
수집기는 저장된 줄 번호부터 이어받고 WIPO 가 지난 문장을 보관하므로, 재시작해도 빠지는 문장이 없다.
"""
import argparse
import json
import os
import random
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

GVA = ZoneInfo("Europe/Zurich")
UTC = timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://www.wipo.int/s2t/live_s2t/getLiveTranscript"
SESSIONS = "https://webcast.wipo.int/liveSessions.json"
DEFAULT_IDS = ["db9f085696a115b4e7b251498aecf4a1"]      # 첫 행사(11/9-10). 필요하면 --ids 로 추가
ID_EVENT = {"db9f085696a115b4e7b251498aecf4a1": "WIPO/IP-AI/GEN/26"}   # 이 id 는 해당 행사 날짜에만 대기용 수집을 유지
WORKFLOW = "collect.yml"
STALE_MIN = 12          # 수집 작업이 떠 있어도 저장 위치가 이 시간 넘게 안 바뀌면 멈춘 것으로 본다
YOUNG_RUN_MIN = 10      # 막 시작한 작업은 기다려 준다
FAIL_LIMIT = 3          # 최근 40분 안에 실패한 수집 작업이 이 이상이면 사람에게 알린다


def now_utc():
    return datetime.now(UTC)


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def probe(mid, line):
    """('ok', data) | ('none', None)=아직 데이터 없음(503/404) | ('error', 설명)=접속/응답 이상"""
    try:
        return "ok", http_json(f"{API}?meeting_id={mid}&line_number={line}&cache={random.random()}")
    except urllib.error.HTTPError as e:
        return ("none", None) if e.code in (503, 404) else ("error", f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        return "error", f"{type(e).__name__}: {e}"


def live_sessions():
    """{hash: 세션 이름} - 진행 중(NOT_STARTED/STOPPED 가 아닌) 세션"""
    try:
        data = http_json(f"{SESSIONS}?cache={random.random()}")
    except Exception:
        return {}
    out = {}
    for s in data if isinstance(data, list) else []:
        st = (s.get("live_status") or "").upper()
        h = s.get("WIPOs2tLiveTranscriptHash")
        if h and st not in ("", "NOT_STARTED", "STOPPED"):
            out[h] = s.get("name", "")
    return out


def saved_state(data_dir, mid):
    """저장된 (next_line_number, updated) 또는 (None, None)"""
    try:
        st = json.load(open(os.path.join(data_dir, "transcripts", mid, "state.json"), encoding="utf-8"))
        return st.get("next_line_number"), datetime.fromisoformat(st["updated"]).astimezone(UTC)
    except Exception:
        return None, None


def gh(args, dry, capture=True):
    if dry and args[0] in ("workflow", "issue", "run") and args[1] in ("run", "create", "cancel"):
        print("  [dry-run] gh " + " ".join(args))
        return ""
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    except FileNotFoundError:
        print("  gh CLI 없음 - 건너뜀")
        return ""
    if r.returncode != 0:
        print(f"  gh {' '.join(args[:2])} 실패: {r.stderr.strip()[:200]}")
    return r.stdout


def list_runs(dry):
    out = gh(["run", "list", "--workflow", WORKFLOW, "--limit", "30", "--json",
              "databaseId,status,conclusion,displayTitle,createdAt"], False)
    try:
        return json.loads(out or "[]")
    except json.JSONDecodeError:
        return []


def run_id(r):
    """수집 작업 제목 'collect <meeting_id>' 의 id (정확히 일치 비교용: '<해시>' 가 '<해시>_no_delay' 에 포함되는 문제 방지)"""
    t = (r.get("displayTitle") or "").split()
    return t[-1] if t else ""


def decide(mid, probe_res, saved_next, saved_updated, runs, listed, now, keep_alive=False):
    """반환: (action, reason). action: none | dispatch | restart | alert"""
    mine = [r for r in runs if run_id(r) == mid]
    active = [r for r in mine if r.get("status") in ("queued", "in_progress", "waiting", "pending", "requested")]
    fails = [r for r in mine if r.get("conclusion") == "failure" and
             now - datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) < timedelta(minutes=40)]
    kind, data = probe_res
    if kind == "error":
        return ("alert", f"WIPO 전사 API 접속 이상: {data}") if listed else ("none", f"접속 이상({data}) - 세션 미진행")
    if kind == "none":
        if keep_alive and not active:
            return "dispatch", "회의 시간대 - 전사 시작을 기다리는 수집 작업이 없어 대기용으로 시작"
        return "none", "아직 전사 데이터 없음"
    new_lines = len((data or {}).get("full_lines") or [])
    if new_lines == 0:
        if keep_alive and not active:
            return "dispatch", "회의 시간대 - 다음 발언을 기다리는 수집 작업이 없어 대기용으로 시작"
        return "none", "저장 위치까지 모두 저장됨"
    if len(fails) >= FAIL_LIMIT:
        return "alert", f"수집 작업이 최근 40분 동안 {len(fails)}회 실패 (새 문장 {new_lines}개 미저장)"
    if not active:
        return "dispatch", f"새 문장 {new_lines}개가 저장되지 않았고 실행 중인 수집 작업이 없음"
    youngest = max(datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) for r in active)
    if now - youngest < timedelta(minutes=YOUNG_RUN_MIN):
        return "none", "수집 작업이 막 시작됨 - 대기"
    if saved_updated is None or now - saved_updated > timedelta(minutes=STALE_MIN):
        age = "저장 기록 없음" if saved_updated is None else f"{(now - saved_updated).total_seconds() / 60:.0f}분 전"
        return "restart", f"수집 작업은 떠 있으나 저장 위치가 갱신되지 않음 (마지막 {age}), 새 문장 {new_lines}개"
    return "none", "수집 작업 정상 진행 중"


def window_bounds(agenda, now_g, margin_before=20, margin_after=30):
    """오늘 회의 시간대 (시작 20분 전, 종료 30분 후) 또는 None"""
    for day in agenda["days"]:
        if day["date"] != now_g.strftime("%Y-%m-%d"):
            continue
        h, m = map(int, day["items"][0]["start"].split(":")[:2])
        start = now_g.replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(minutes=margin_before)
        h, m = map(int, day["items"][-1]["end"].split(":")[:2])
        end = now_g.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(minutes=margin_after)
        return start, end
    return None


def in_window(agenda, now_g, margin_before=20, margin_after=30):
    b = window_bounds(agenda, now_g, margin_before, margin_after)
    return bool(b) and b[0] <= now_g <= b[1]


def today_event_code(agenda, now_g):
    for day in agenda["days"]:
        if day["date"] == now_g.strftime("%Y-%m-%d"):
            return (day.get("event") or {}).get("code")
    return None


def run_minutes(agenda, now_g):
    """대기용 수집 작업의 최대 실행 시간(분): 시간대 종료까지 + 15분, 20~300분"""
    b = window_bounds(agenda, now_g)
    left = int((b[1] - now_g).total_seconds() // 60) + 15 if b else 60
    return max(20, min(300, left))


def open_issue(title, body, dry):
    found = gh(["issue", "list", "--state", "open", "--search", f'"{title}" in:title', "--json", "number"], False)
    try:
        if json.loads(found or "[]"):
            print(f"  이미 열린 이슈 있음: {title}")
            return
    except json.JSONDecodeError:
        pass
    gh(["issue", "create", "--title", title, "--body", body + "\n\n_자동 생성: watchdog_"], dry)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(HERE), "data"), help="data 브랜치 체크아웃 경로")
    ap.add_argument("--agenda", default=os.path.join(HERE, "docs", "agenda.json"))
    ap.add_argument("--ids", default=os.environ.get("MEETING_IDS", ""), help="쉼표로 구분한 추가 meeting_id (해시 또는 해시_no_delay)")
    ap.add_argument("--dry-run", action="store_true", help="워크플로 실행/이슈 생성 없이 판단만 출력")
    ap.add_argument("--ignore-window", action="store_true", help="행사 시간대 검사 건너뜀(테스트용)")
    a = ap.parse_args()

    agenda = json.load(open(a.agenda, encoding="utf-8"))
    now = now_utc()
    now_g = now.astimezone(GVA)
    print(f"[{now_g:%Y-%m-%d %H:%M} Geneva] watchdog 시작" + (" (dry-run)" if a.dry_run else ""))
    if not a.ignore_window and not in_window(agenda, now_g):
        print("행사 시간대가 아님 - 종료")
        return 0

    live = live_sessions()
    ids = {h: n for h, n in live.items()}
    for x in DEFAULT_IDS + [i.strip() for i in a.ids.split(",") if i.strip()]:
        ids.setdefault(x.replace("_no_delay", ""), "")
    td = os.path.join(a.data_dir, "transcripts")
    if os.path.isdir(td):
        for d in os.listdir(td):
            ids.setdefault(d.replace("_no_delay", ""), "")
    runs = list_runs(a.dry_run)
    keep = in_window(agenda, now_g)          # 회의 시간대 안이면 수집 작업을 항상 하나 유지
    rc = 0
    for h, name in ids.items():
        # WIPO 는 같은 세션에 '<해시>_no_delay'(저지연)와 '<해시>'(일반) 스트림을 따로 제공하고, 한쪽만 멈추는 일이 있었다
        # (10:17 UTC 에 _no_delay 가 멈추고 일반 스트림은 계속 진행). 그래서 두 스트림을 각각 독립적으로 수집한다 (이중화).
        keep_h = keep and (h in live or ID_EVENT.get(h, today_event_code(agenda, now_g)) == today_event_code(agenda, now_g))
        for mid in (f"{h}_no_delay", h):
            nxt, upd = saved_state(a.data_dir, mid)
            res = probe(mid, nxt or 1)
            action, reason = decide(mid, res, nxt, upd, runs, h in live, now, keep_alive=keep_h)
            print(f"- {mid[:12]}…{'_nd' if mid.endswith('_no_delay') else '   '} {('(' + name + ') ') if name else ''}저장위치={nxt} -> {action}: {reason}")
            if action in ("dispatch", "restart"):
                if action == "restart":
                    for r in runs:
                        if run_id(r) == mid and r.get("status") in ("queued", "in_progress", "waiting", "pending", "requested"):
                            gh(["run", "cancel", str(r["databaseId"])], a.dry_run)
                gh(["workflow", "run", WORKFLOW, "-f", f"meeting_id={mid}", "-f", f"max_minutes={run_minutes(agenda, now_g)}",
                    "-f", "idle_exit_minutes=45"], a.dry_run)
                if nxt is None and res[0] == "ok":      # 실제 전사가 처음 확인된 경우에만 시작 알림
                    open_issue(f"[watchdog] 수집 시작: {mid[:8]}{'_nd' if mid.endswith('_no_delay') else ''} {now_g:%Y-%m-%d}",
                               f"회의 전사가 시작된 것을 감지해 수집을 자동 시작했습니다.\n\n- meeting_id: `{mid}`\n- {name or '세션 이름 미확인'}\n- {now_g:%Y-%m-%d %H:%M} (Geneva)", a.dry_run)
            elif action == "alert":
                rc = 1
                open_issue(f"[watchdog] 수집 이상: {mid[:8]}{'_nd' if mid.endswith('_no_delay') else ''} {now_g:%Y-%m-%d}",
                           f"- meeting_id: `{mid}`\n- 내용: {reason}\n- 시각: {now_g:%Y-%m-%d %H:%M} (Geneva)\n\n"
                           "Actions 탭에서 `Collect WIPO live transcript` 실행 로그를 확인하세요. "
                           "러너에서 WIPO 접속이 막힌 경우 다른 환경(PC 등)에서 `python3 collect.py` 를 실행해야 합니다.", a.dry_run)
    return 0   # 이상 징후는 이슈로 알리며, 워크플로 자체는 실패시키지 않는다 (다음 점검이 계속 돌도록)


if __name__ == "__main__":
    sys.exit(main())
