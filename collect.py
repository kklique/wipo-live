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
import re
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("WIPO_API", "https://www.wipo.int/s2t/live_s2t/getLiveTranscript")   # WIPO_API: 시험용 대체 주소
DEFAULT_MEETING = "db9f085696a115b4e7b251498aecf4a1_no_delay"
SESSION_URLS = [
    "https://webcast.wipo.int/liveSessions.json",
    "https://webcast.wipo.int/history_liveSessions.json",
]
# 변화 감지 대상 필드. markers 는 새로 생긴 것만, 나머지는 값이 바뀔 때만 기록한다.
META_FIELDS = ("name", "event", "startDate", "endDate", "live", "live_status", "cvc5status", "people", "subjects")


def is_noise(text):
    """글자·숫자가 하나도 없는 줄 ('.', '. .', '...' 등). 휴식·무음 구간에서 음성인식이 계속 내보내는 비정상 문장."""
    return not re.search(r"\w", text or "")


FILLER = {"thank", "you", "thanks", "yeah", "yes", "okay", "ok", "bye", "uh", "um", "hmm", "mm", "mhm", "music", "applause"}


def is_filler(text):
    """필러(감사/맞장구/음)만으로 된 줄. 한두 글자짜리 비영어 토큰('嗯' 등)도 필러로 본다."""
    toks = re.findall(r"\w+", (text or "").lower())
    return bool(toks) and all(t in FILLER or (not t.isascii() and len(t) <= 2) for t in toks)


def filler_repeat(text):
    """한 줄 안에서 필러 구가 3번 이상 반복 ('Yeah. Yeah. Yeah.')"""
    return is_filler(text) and len([p for p in re.split(r"[.!?。！？]+", text) if p.strip()]) >= 3


def drop_filler_runs(items, key=lambda x: x):
    """비정상 스크립트 제거: 필러만 있는 줄이 3줄 이상 연속이면 그 구간 전체, 한 줄 안 3회 이상 반복이면 그 줄을 삭제한다.
    1~2줄짜리 단독 'Thank you.' 는 실제 발언일 수 있어 유지한다. key 로 항목에서 텍스트를 꺼낸다 (항목 순서/정체성 유지)."""
    out, run = [], []
    for x in items:
        t = key(x)
        if is_filler(t):
            if not filler_repeat(t):
                run.append(x)
            continue
        if len(run) < 3:
            out.extend(run)
        run = []
        out.append(x)
    if len(run) < 3:
        out.extend(run)
    return out


def clean_file(path):
    """저장된 transcript.txt 에서 잡음 줄을 제거한다 (수집기 시작 시 1회, 수집기만 쓰는 시점이라 경합 없음). 제거한 줄 수를 반환."""
    try:
        old = open(path, encoding="utf-8").read().split("\n")
    except OSError:
        return 0
    keep = drop_filler_runs([l for l in old if l.strip() and not is_noise(l)])
    removed = len([l for l in old if l.strip()]) - len(keep)
    if removed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + ("\n" if keep else ""))
        os.replace(tmp, path)
    return removed


def append(path, text):
    """매번 열고 닫으며 덧붙인다. 파일 핸들을 계속 들고 있으면, 워크플로가 `git pull --rebase` 로 작업 폴더의 파일을 교체했을 때
    이후 기록이 지워진 옛 파일로 가서 유실된다 (실제로 일어난 사고). 열고 닫는 방식은 파일이 교체되어도 항상 현재 파일에 쓴다."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


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

    removed = clean_file(p_txt)
    if removed:
        print(f"[{now()}] 기존 저장본에서 비정상 줄(점만 있는 줄, 필러 반복 구간) {removed}개 제거", flush=True)
    try:                # state.json 에 기록할 '저장된 문장 수' (웹이 파일 줄 수와 대조해 누락을 알아챔)
        saved_lines = sum(1 for _ in open(p_txt, encoding="utf-8"))
    except OSError:
        saved_lines = 0

    def emit(rec):
        append(p_jsonl, json.dumps(rec, ensure_ascii=False) + "\n")

    p_markers, p_session = (os.path.join(a.out, f) for f in ("markers.jsonl", "session_latest.json"))
    seen_markers = set()
    try:  # 재실행(예: 오후 세션) 시 이미 기록한 마커는 다시 쓰지 않는다
        for line in open(p_markers, encoding="utf-8"):
            for m in json.loads(line).get("new_markers") or []:
                seen_markers.add(json.dumps(m, sort_keys=True, ensure_ascii=False))
    except (OSError, json.JSONDecodeError):
        pass
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
            append(p_markers, json.dumps({"ts": now(), **changed_meta, "new_markers": new_markers}, ensure_ascii=False) + "\n")
            with open(p_session, "w", encoding="utf-8") as f:
                json.dump(sess, f, ensure_ascii=False, indent=2)
            print(f"[{now()}] 마커 갱신 (신규 {len(new_markers)}건, 누적 {len(seen_markers)}건, "
                  f"status={sess.get('live_status')})", flush=True)

    pend, in_run, run_from = [], False, None   # 필러 줄 보류 버퍼: 3줄 이상 연속이면 버리고, 그 전에 끊기면 저장
    last_partial = None
    waiting_notice = False
    last_change = None  # 마지막으로 새 내용이 들어온 시각 (monotonic); 첫 데이터 수신 전에는 None
    try:                # 재시작해도 이어지도록 이전 state.json 의 last_data 를 이어받음
        last_data = json.load(open(p_state)).get("last_data")
    except Exception:
        last_data = None
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
                      "batch": len(lines), "catchup": len(lines) > 3,   # 한 번에 많이 받았다 = 지난 문장 따라잡기
                      **({"noise": True} if is_noise(text) else {})})   # 점만 있는 줄 등: 원본 기록에는 남기되 저장본에서는 제외
                if text and not is_noise(text):
                    if is_filler(text):
                        if filler_repeat(text):
                            pass                                       # 한 줄 안 반복: 바로 제외
                        elif in_run:
                            pass                                       # 이미 비정상 구간으로 판정된 중
                        else:
                            pend.append(text)
                            if len(pend) >= 3:                         # 3줄 연속 -> 비정상 구간, 보류분 포함 전부 버림
                                in_run, run_from = True, line_no + i - 2
                                emit({"ts": now(), "type": "filler_run_start", "line_from": run_from})
                                pend = []
                    else:
                        if in_run:
                            emit({"ts": now(), "type": "filler_run_end", "line_to": line_no + i - 1})
                        in_run = False
                        for t in pend:                                 # 1~2줄짜리 필러는 실제 발언으로 보고 저장
                            append(p_txt, t + "\n")
                            saved_lines += 1
                        pend = []
                        append(p_txt, text + "\n")
                        saved_lines += 1
                print(f"[F {line_no + i}] {text}", flush=True)
            if lines:
                last_change = time.monotonic()
                last_data = now()

            nxt = d.get("next_line_number")
            expected = line_no + len(lines)
            if isinstance(nxt, int) and nxt != expected:     # 줄 번호가 이어지지 않음 = 누락 가능성
                emit({"ts": now(), "type": "gap", "from": line_no, "expected_next": expected, "next_line_number": nxt})
                print(f"[{now()}] 경고: 줄 번호 불연속 (예상 {expected}, 응답 {nxt})", flush=True)
            if isinstance(nxt, int) and nxt >= line_no:
                line_no = nxt
            else:
                line_no += len(lines)
            # updated: 수집기가 살아 있다는 신호 / last_data: WIPO 에서 마지막으로 새 문장(확정·진행 중)이 들어온 시각
            # (스트림이 멈추면 updated 는 계속 갱신되지만 last_data 는 멈춘다)
            json.dump({"next_line_number": line_no, "updated": now(), "last_data": last_data, "saved_lines": saved_lines},
                      open(p_state, "w"))

            # 2) 진행 중(미확정) 문장 -- 바뀔 때마다 기록
            partial = ((d.get("last_line") or {}).get("text") or "").strip()
            if partial and is_noise(partial):
                partial = ""
            if partial and partial != last_partial:
                last_change = time.monotonic()
                last_data = now()
                emit({"ts": now(), "type": "partial", "line_number": line_no, "text": partial})
                print(f"[P {line_no}] {partial}", flush=True)
            if partial:
                last_partial = partial

            time.sleep(a.interval)
    except KeyboardInterrupt:
        print(f"\n[{now()}] 중단. 다음 시작 line_number={line_no}", flush=True)
    for t in pend:                       # 종료 시 보류 중인 1~2줄 필러는 저장 (3줄 미만이면 실제 발언일 수 있음)
        append(p_txt, t + "\n")


if __name__ == "__main__":
    sys.exit(main())
