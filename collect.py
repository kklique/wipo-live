#!/usr/bin/env python3
"""WIPO Live Speech-to-Text 수집기 (표준 라이브러리만 사용).

webcast.wipo.int 페이지가 내부적으로 폴링하는 공개 엔드포인트
  https://www.wipo.int/s2t/live_s2t/getLiveTranscript?meeting_id=...&line_number=N
를 주기적으로 호출해서 갱신되는 영문 텍스트를 모두 저장한다.

응답 구조
  full_lines       : 확정된 문장 목록 (line_number 부터 끝까지)  [{text, end_time_iso}]
  last_line.text   : 현재 말하는 중인(미확정) 문장 -- 계속 바뀜
  next_line_number : 다음에 이어서 요청할 번호

출력 (--out 디렉터리)
  transcript.txt   : 확정 문장만, 한 줄에 한 문장 (읽기용)
  updates.jsonl    : 모든 변화 기록 (확정 문장 + 진행 중 문장의 매 변경 스냅샷)
  state.json       : 이어받기용 다음 line_number
  markers.jsonl    : 발언자 마커 변경 기록 (webcast liveSessions.json 의 markers/people 등).
                     새 마커가 생기거나 상태/people 이 바뀔 때만 한 줄 추가 (new_markers = 새로 생긴 마커).
                     화자별 분리는 이 파일 + updates.jsonl 로 사후 처리.
  session_latest.json : 매칭된 webcast 세션 정보의 전체 최신본

회의가 시작 전이면(503 등) 조용히 재시도하며 기다린다. Ctrl+C 로 종료, 재실행하면 이어서 저장.
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://www.wipo.int/s2t/live_s2t/getLiveTranscript"
DEFAULT_MEETING = "db9f085696a115b4e7b251498aecf4a1_no_delay"
SESSION_URLS = [
    "https://webcast.wipo.int/liveSessions.json",
    "https://webcast.wipo.int/history_liveSessions.json",
]
# 변화 감지 대상 필드. markers 는 새로 생긴 것만, 나머지는 값이 바뀔 때만 기록한다.
META_FIELDS = ("name", "event", "startDate", "endDate", "live", "live_status", "cvc5status", "people", "subjects")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(meeting_id, line_number, timeout=30):
    # 페이지와 동일하게 cache 파라미터로 CDN 캐시(max-age=10)를 피한다.
    url = f"{API}?meeting_id={meeting_id}&line_number={line_number}&cache={random.random()}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def find_session(meeting_id):
    """webcast 세션 목록에서 meeting_id(접미사 _no_delay 제외)와 같은 전사 해시를 가진 세션을 찾는다."""
    key = meeting_id.replace("_no_delay", "")
    for url in SESSION_URLS:
        req = urllib.request.Request(f"{url}?cache={random.random()}", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        for sess in data if isinstance(data, list) else []:
            if sess.get("WIPOs2tLiveTranscriptHash") == key:
                return sess
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--meeting-id", default=DEFAULT_MEETING)
    ap.add_argument("--out", default="out")
    ap.add_argument("--interval", type=float, default=2.0, help="회의 진행 중 폴링 간격(초)")
    ap.add_argument("--wait-interval", type=float, default=30.0, help="회의 시작 전 대기 폴링 간격(초)")
    ap.add_argument("--start-line", type=int, default=None, help="시작 line_number (기본: 저장된 state 또는 1)")
    ap.add_argument("--max-seconds", type=float, default=None, help="이 시간(초) 후 종료 (대기 시간 포함)")
    ap.add_argument("--idle-exit-minutes", type=float, default=None,
                    help="데이터를 한 번이라도 받은 뒤, 이 시간(분) 동안 새 변화가 없으면 종료")
    ap.add_argument("--markers-interval", type=float, default=30.0,
                    help="발언자 마커(webcast 세션 정보) 확인 간격(초). 0 이면 끔")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    p_txt, p_jsonl, p_state = (os.path.join(a.out, f) for f in ("transcript.txt", "updates.jsonl", "state.json"))

    line_no = a.start_line
    if line_no is None:
        try:
            line_no = json.load(open(p_state))["next_line_number"]
        except Exception:
            line_no = 1

    txt = open(p_txt, "a", encoding="utf-8")
    log = open(p_jsonl, "a", encoding="utf-8")

    def emit(rec):
        log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log.flush()

    p_markers, p_session = (os.path.join(a.out, f) for f in ("markers.jsonl", "session_latest.json"))
    seen_markers = set()
    try:  # 재실행(예: 오후 세션) 시 이미 기록한 마커는 다시 쓰지 않는다
        for line in open(p_markers, encoding="utf-8"):
            for m in json.loads(line).get("new_markers") or []:
                seen_markers.add(json.dumps(m, sort_keys=True, ensure_ascii=False))
    except (OSError, json.JSONDecodeError):
        pass
    markers_log = open(p_markers, "a", encoding="utf-8")
    last_meta = None
    last_markers_check = 0.0
    markers_notice = None  # 'nomatch' | 'error' : 같은 안내를 반복 출력하지 않기 위함

    def check_markers():
        nonlocal last_meta, last_markers_check, markers_notice
        if not a.markers_interval or time.monotonic() - last_markers_check < a.markers_interval:
            return
        last_markers_check = time.monotonic()
        try:
            sess = find_session(a.meeting_id)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as e:
            if markers_notice != "error":
                print(f"[{now()}] 마커 조회 실패({type(e).__name__}: {e}) - 계속 재시도", flush=True)
                markers_notice = "error"
            return
        if sess is None:
            if markers_notice != "nomatch":
                print(f"[{now()}] webcast 세션 목록에 아직 이 meeting_id 없음 - 계속 확인", flush=True)
                markers_notice = "nomatch"
            return
        if markers_notice is not None:
            print(f"[{now()}] webcast 세션 매칭: {sess.get('name')} ({sess.get('live_status')})", flush=True)
            markers_notice = None

        new_markers = []
        for m in sess.get("markers") or []:
            k = json.dumps(m, sort_keys=True, ensure_ascii=False)
            if k not in seen_markers:
                seen_markers.add(k)
                new_markers.append(m)
        meta = {k: sess.get(k) for k in META_FIELDS}
        changed_meta = {k: v for k, v in meta.items() if last_meta is None or last_meta.get(k) != v}
        if new_markers or changed_meta:
            last_meta = meta
            markers_log.write(json.dumps({"ts": now(), **changed_meta, "new_markers": new_markers},
                                         ensure_ascii=False) + "\n")
            markers_log.flush()
            with open(p_session, "w", encoding="utf-8") as f:
                json.dump(sess, f, ensure_ascii=False, indent=2)
            print(f"[{now()}] 마커 갱신 (신규 {len(new_markers)}건, 누적 {len(seen_markers)}건, "
                  f"status={sess.get('live_status')})", flush=True)

    last_partial = None
    waiting_notice = False
    last_change = None  # 마지막으로 새 내용이 들어온 시각 (monotonic); 첫 데이터 수신 전에는 None
    t0 = time.time()
    print(f"[{now()}] meeting_id={a.meeting_id} 시작 line_number={line_no} -> {a.out}/", flush=True)

    try:
        while True:
            if a.max_seconds and time.time() - t0 > a.max_seconds:
                print(f"[{now()}] --max-seconds 도달, 종료", flush=True)
                break
            if (a.idle_exit_minutes and last_change is not None
                    and time.monotonic() - last_change > a.idle_exit_minutes * 60):
                print(f"[{now()}] {a.idle_exit_minutes:g}분간 변화 없음, 종료", flush=True)
                break
            check_markers()
            try:
                d = fetch(a.meeting_id, line_no)
            except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as e:
                # 회의 시작 전에는 503(점검 안내 HTML)이 오며, 이는 정상적인 '아직 없음' 상태.
                if not waiting_notice:
                    print(f"[{now()}] 아직 전사 데이터 없음/일시 오류 ({type(e).__name__}: {e}). "
                          f"{a.wait_interval:.0f}초마다 재시도...", flush=True)
                    waiting_notice = True
                time.sleep(a.wait_interval)
                continue
            if waiting_notice:
                print(f"[{now()}] 전사 데이터 수신 시작", flush=True)
                waiting_notice = False

            # 1) 확정 문장
            lines = d.get("full_lines") or []
            for i, ln in enumerate(lines):
                text = (ln.get("text") or "").strip()
                emit({"ts": now(), "type": "final", "line_number": line_no + i,
                      "end_time_iso": ln.get("end_time_iso"), "text": text,
                      "batch": len(lines), "catchup": len(lines) > 3})   # 한 번에 많이 받았다 = 지난 문장 따라잡기
                if text:
                    txt.write(text + "\n")
                print(f"[F {line_no + i}] {text}", flush=True)
            if lines:
                txt.flush()
                last_change = time.monotonic()

            nxt = d.get("next_line_number")
            expected = line_no + len(lines)
            if isinstance(nxt, int) and nxt != expected:     # 줄 번호가 이어지지 않음 = 누락 가능성
                emit({"ts": now(), "type": "gap", "from": line_no, "expected_next": expected, "next_line_number": nxt})
                print(f"[{now()}] 경고: 줄 번호 불연속 (예상 {expected}, 응답 {nxt})", flush=True)
            if isinstance(nxt, int) and nxt >= line_no:
                line_no = nxt
            else:
                line_no += len(lines)
            json.dump({"next_line_number": line_no, "updated": now()}, open(p_state, "w"))

            # 2) 진행 중(미확정) 문장 -- 바뀔 때마다 기록
            partial = ((d.get("last_line") or {}).get("text") or "").strip()
            if partial and partial != last_partial:
                last_change = time.monotonic()
                emit({"ts": now(), "type": "partial", "line_number": line_no, "text": partial})
                print(f"[P {line_no}] {partial}", flush=True)
            if partial:
                last_partial = partial

            time.sleep(a.interval)
    except KeyboardInterrupt:
        print(f"\n[{now()}] 중단. 다음 시작 line_number={line_no}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
