#!/usr/bin/env python3
"""수집된 영문 스크립트를 행사 아젠더(docs/agenda.json) 기준으로 발표(세션)별로 나눠 저장한다. (표준 라이브러리만 사용)

입력  : collect.py 출력 폴더의 updates.jsonl (확정 문장 + 시각)
출력  : <out>/<id>_<slug>.txt   세션별 스크립트 (맨 위 '# ' 줄은 머리말)
        <out>/index.json         세션 목록/구간/문장 수 (웹 뷰어가 사용)
        <out>/all_by_session.md  전체를 세션 제목으로 구분해 한 파일로

경계 결정 (세션마다, 날짜별로 독립 처리 -> 2~3일 행사 지원)
  1) 아젠더 예정 시각 + 앞 세션까지 누적된 지연(drift) 으로 '예상 시작 시각' 계산
  2) 예상 시각 -10분 ~ +12분 안에서 그 세션의 발표자 이름/제목 키워드가 처음 언급되는 문장을 시작점으로 삼음
     (ASR 오인식을 감안해 유사도 매칭, 휴식 세션은 '쉬겠다'는 안내 문장 다음부터)
  3) 단서가 없으면 예상 시각 이후 첫 문장
  4) --override d1-04=10:46:30 (Geneva 시각) 으로 수동 지정 가능. 어떤 방식으로 정했는지는 index.json 의 method 에 기록.
발표자별 저장 (by_speaker/, 각 발표자의 발언만 모아 저장 -> 발표자별 AI 요약의 입력)
  - 웹캐스트 발언자 마커(markers.jsonl: 이름/국가 + 시각)가 있으면 그 시각으로 발언자 교대를 나눈다 (가장 정확, 플로어 발언의 국가별 구분 포함).
    마커 시각은 문장 시각(end_time_iso)과 같은 시계(CVC5 현지 시계)라 별도 보정 없이 비교한다.
  - 마커가 없으면: 발표자가 1명이면 아젠더 기준, 패널이면 사회자의 호명 문장("…님 모시겠습니다")에서 이름을 잡아 나눈다(경계가 덜 정확).
  - 산출물: by_speaker/<세션id>__<발표자>.txt, by_speaker/speakers.jsonl (세션 맥락 + 소속/역할 + 단어 수 + 본문)
시계: 문장 시각(end_time_iso)이 UTC 인지 Geneva 현지시간인지는 수신 시각(ts)과 비교해 자동 판별 (--clock 으로 강제 가능).
"""
import argparse
import difflib
import json
import math
import os
import re
import statistics
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
GVA = ZoneInfo("Europe/Zurich")
HERE = os.path.dirname(os.path.abspath(__file__))

TITLES = {"mr", "ms", "mrs", "dr", "prof", "professor", "he", "hon", "sir"}
PARTICLES = {"de", "van", "von", "der", "den", "la", "le", "da", "di", "dos", "del", "el", "bin"}
STOP = {"about", "after", "their", "there", "which", "would", "could", "other"}
WIN_BEFORE = timedelta(minutes=10)
WIN_AFTER = timedelta(minutes=12)
DRIFT_MIN, DRIFT_MAX = timedelta(minutes=-15), timedelta(minutes=45)
THRESHOLD = 3.0


# ---------------------------------------------------------------- 텍스트 매칭
def norm(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def toks(s):
    return norm(s).split()


def fuzzy_in(word, tokens, thr=0.84):
    if len(word) < 4:
        return word in tokens
    return any(abs(len(t) - len(word)) <= 2 and difflib.SequenceMatcher(None, word, t).ratio() >= thr
               for t in tokens)


def person_tokens(name):
    return [t for t in toks(name) if t not in TITLES and t not in PARTICLES and len(t) > 1]


def person_hit(ptoks, ltoks):
    if not ptoks:
        return False
    m = [fuzzy_in(w, ltoks) for w in ptoks]
    return sum(m) >= 2 or (m[-1] and len(ptoks[-1]) >= 5)   # 이름 2개 이상 또는 뚜렷한 성(5자+)


def build_cues(item):
    people = []
    if item.get("moderator"):
        people.append(item["moderator"])
    people += item.get("speakers", [])
    people = [p for p in people if p.get("name", "").strip().upper() != "TBC"]
    title = item["title"]
    prefix = [t for t in toks(title.split(":")[0]) if len(t) >= 3 and not t.isdigit()] if ":" in title else []
    main = [t for t in dict.fromkeys(toks(title)) if len(t) >= 5 and t not in STOP and t not in prefix]
    return {"people": [person_tokens(p["name"]) for p in people], "prefix": prefix, "main": main}


def score(c, ltoks):
    s = 0.0
    if any(person_hit(pt, ltoks) for pt in c["people"]):
        s += 3
    if c["prefix"]:
        hit = sum(fuzzy_in(w, ltoks) for w in c["prefix"])
        if hit == len(c["prefix"]):
            s += 3 if len(c["prefix"]) >= 2 else 1.5
    if c["main"]:
        n = len(c["main"])
        m = sum(fuzzy_in(w, ltoks) for w in c["main"])
        need = n if n <= 3 else max(3, math.ceil(n * 0.6))   # 짧은 제목은 전부, 긴 제목은 60%+ (일반 단어 오탐 방지)
        s += 3 if (n >= 2 and m >= need) else min(m, 2) * 0.5   # 키워드 1개만으로는 확정하지 않음
    return s


# ---------------------------------------------------------------- 입력 / 시계
FILLER = {"thank", "you", "thanks", "yeah", "yes", "okay", "ok", "bye", "uh", "um", "hmm", "mm", "mhm", "music", "applause"}


def is_filler(text):
    """필러(감사/맞장구/음)만으로 된 줄. 한두 글자짜리 비영어 토큰('嗯' 등)도 필러로 본다."""
    toks = re.findall(r"\w+", (text or "").lower())
    return bool(toks) and all(t in FILLER or (not t.isascii() and len(t) <= 2) for t in toks)


def filler_repeat(text):
    """한 줄 안에서 필러 구가 3번 이상 반복 ('Yeah. Yeah. Yeah.')"""
    return is_filler(text) and (len([p for p in re.split(r"[.!?。！？]+", text) if p.strip()]) >= 3
                                or len(re.findall(r"\w+", text)) >= 6)       # 'Yeah. Yeah. Yeah.' / 'Thank you 嗯 嗯 嗯 嗯 ...'


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


def read_rows(dirs):
    rows = {}
    for d in dirs:
        p = os.path.join(d, "updates.jsonl")
        if not os.path.exists(p):
            continue
        for ln in open(p, encoding="utf-8"):
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("type") != "final" or not (r.get("text") or "").strip():
                continue
            if r.get("noise") or not re.search(r"\w", r["text"]):      # 점만 있는 줄 등 잡음은 분할/저장에서 제외
                continue
            try:
                ts = datetime.fromisoformat(r["ts"]).astimezone(UTC)
            except (KeyError, ValueError):
                continue
            end = None
            if r.get("end_time_iso"):
                try:
                    end = datetime.fromisoformat(r["end_time_iso"]).replace(tzinfo=None)
                except ValueError:
                    pass
            rows[(d, r.get("line_number"))] = {"src": d, "n": r.get("line_number"), "ts": ts, "end": end,
                                               "catchup": bool(r.get("catchup")), "text": r["text"].strip()}
    out = sorted(rows.values(), key=lambda r: (r["src"], r["n"] or 0))
    kept = []
    for src in sorted({r["src"] for r in out}):         # 스트림(폴더)별로 필러 반복 구간 제거
        kept += drop_filler_runs([r for r in out if r["src"] == src], key=lambda r: r["text"])
    return kept


def _gva_times(rows, clock_fn):
    return [clock_fn(r) for r in rows if r["end"]]


def calibrate(rows, clock, agenda):
    """문장 시각(end_time_iso)이 어느 시계인지 판별. 반환: (clock, 중앙값(초) | None, 메모)
    clock: 'utc' | 'geneva' | 'receipt' | 'offset:<초>' (문장 시각 + <초> = UTC)"""
    if clock != "auto":
        return clock, None, ""
    live = [r for r in rows if r["end"] and not r["catchup"]]     # 라이브로 받은 줄만 (따라잡기 배치 제외)
    if len(live) >= 5:
        diffs = [(r["ts"] - r["end"].replace(tzinfo=UTC)).total_seconds() for r in live]
        med = statistics.median(diffs)
        off = int(round(med / 900.0)) * 900                         # 수신 지연(수 분)은 15분 단위 반올림으로 흡수
        if off == 0:
            return "utc", med, ""
        if abs(off + 3600) <= 900 and abs(med + 3600) < 900:
            return "geneva", med, ""
        return f"offset:{off}", med, f"문장 시각 시차 {off / 3600:+.2f}h 로 판별(라이브 수신 줄 기준)"
    pool = [r for r in rows if r["end"]]
    if not pool:
        return "receipt", None, "문장 시각 없음 -> 수신 시각 사용"
    # 라이브로 받은 줄이 거의 없음(전부 지난 문장 따라잡기): 아젠더 첫 시각과 가장 잘 맞는 시계를 고른다
    best = None
    for hyp in ("utc", "geneva"):
        err = 0.0
        for day in agenda["days"]:
            ts_ = [to_geneva(r, hyp, None) for r in pool]
            ts_ = [t for t in ts_ if t.strftime("%Y-%m-%d") == day["date"]]
            if ts_:
                err += abs((min(ts_) - at(datetime.strptime(day["date"], "%Y-%m-%d"), day["items"][0]["start"])).total_seconds())
        if best is None or err < best[1]:
            best = (hyp, err)
    if best and best[1] < 3600:
        return best[0], None, f"라이브 수신 줄이 없어 아젠더 첫 시각과 비교해 '{best[0]}' 로 판별"
    return "geneva", None, "시계 판별 불가(전부 지난 문장 따라잡기) -> Geneva 현지시간으로 가정, 경계가 이상하면 --clock 지정"


def to_geneva(r, clock, prev):
    if clock == "receipt" or r["end"] is None and prev is None:
        return r["ts"].astimezone(GVA)
    if r["end"] is None:
        return prev
    if clock.startswith("offset:"):
        return (r["end"].replace(tzinfo=UTC) + timedelta(seconds=int(clock[7:]))).astimezone(GVA)
    if clock == "utc":
        return r["end"].replace(tzinfo=UTC).astimezone(GVA)
    return r["end"].replace(tzinfo=GVA)


def hhmmss(s):
    p = [int(x) for x in s.split(":")] + [0, 0]
    return p[0], p[1], p[2]


def at(day_date, hms):
    h, m, s = hhmmss(hms)
    return datetime(day_date.year, day_date.month, day_date.day, h, m, s, tzinfo=GVA)


# ---------------------------------------------------------------- 날짜별 분할
def title_match(desc, title):
    """웹캐스트 안건 마커 설명이 아젠더 세션 제목과 같은 것인가 (번호/구두점 차이, 콜론 앞 접두어만 쓴 경우 허용)"""
    d = norm(re.sub(r"^\s*\d+[.)]\s*", "", desc))
    t = norm(title)
    pre = norm(title.split(":")[0]) if ":" in title else t
    def same(a, b):                      # 'Showcase 1' 과 'Showcase 2' 는 글자 유사도가 높아도 다른 세션 (숫자가 다르면 불일치)
        return re.findall(r"\d+", a) == re.findall(r"\d+", b) and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85
    return same(d, t) or same(d, pre)


def subject_marker_pos(item, lines, markers, prev, lead):
    """안건 마커로 세션 시작 위치를 찾는다. 앞 세션 시작 이후의 가장 이른 일치 마커. 없으면 None"""
    floor = lines[prev].get("raw") if prev < len(lines) else None
    for m in markers:
        if not m.get("subject") or not title_match(m["desc"], item["title"]):
            continue
        if floor is not None and m["t"] < floor - timedelta(seconds=lead):
            continue
        for k in range(prev, len(lines)):
            if lines[k].get("raw") and lines[k]["raw"] >= m["t"] - timedelta(seconds=lead):
                return k
    return None


def split_day(day, lines, overrides, markers=(), lead=5.0, cue_only=False):
    """lines: [{'t': aware Geneva dt, 'ltoks': [...], ...}] 시간순. 반환: 세션별 (시작 위치, 방식) 목록."""
    date = datetime.strptime(day["date"], "%Y-%m-%d").date()
    items = day["items"]
    out, prev, drift = [], 0, timedelta(0)
    for i, it in enumerate(items):
        S = at(date, it["start"])
        pos, method, anchor = None, "not-reached", None
        if it["id"] in overrides:
            T = at(date, overrides[it["id"]])
            pos = next((k for k in range(prev, len(lines)) if lines[k]["t"] >= T), None)
            method = "manual"
            anchor = lines[pos]["t"] if pos is not None else None
        elif i == 0:
            pos, method = (0, "first-line") if lines else (None, "not-reached")
            sp = subject_marker_pos(it, lines, markers, 0, lead) if lines else None
            if sp is not None:
                pos, method, anchor = sp, "marker", lines[sp]["t"]
        elif subject_marker_pos(it, lines, markers, prev, lead) is not None:
            pos = subject_marker_pos(it, lines, markers, prev, lead)
            method, anchor = "marker", lines[pos]["t"]
        else:
            E = S + drift
            c = build_cues(it)
            for k in range(prev, len(lines)):
                t = lines[k]["t"]
                if not cue_only and t > E + WIN_AFTER:
                    break
                if (cue_only or t >= E - WIN_BEFORE) and score(c, lines[k]["ltoks"]) >= THRESHOLD:
                    pos = k + 1 if it["type"] == "break" else k
                    method, anchor = "cue", t
                    break
            if pos is None and not cue_only:
                pos = next((k for k in range(prev, len(lines)) if lines[k]["t"] >= E), None)
                method = "schedule" if pos is not None else "not-reached"
        if pos is not None:
            pos = max(pos, prev)
            if anchor is not None:
                drift = min(DRIFT_MAX, max(DRIFT_MIN, anchor - S))
            prev = pos
        out.append((pos, method))
    return out


def _slices(lines, res):
    starts = [p for p, _ in res]
    out = []
    for i, (pos, method) in enumerate(res):
        nxt = next((q for q in starts[i + 1:] if q is not None), len(lines))
        out.append((lines[pos:max(pos, nxt)] if pos is not None else [], method))
    return out


def subject_events(day, markers):
    """웹캐스트 마커 타임라인 -> [(시각, 아젠더 항목 index)]. 안건 마커는 제목 매칭, 'Session Break' 같은 발언자 마커는 다음 휴식 항목으로.
    같은 안건이 연속으로 찍히면 처음 것만 쓴다. 같은 안건이 나중에 다시 나와도(장관 발언 재개 등) 그대로 둔다."""
    items = day["items"]
    ev, top = [], -1
    for m in markers:
        i = None
        if m.get("subject"):
            i = next((k for k, it in enumerate(items) if title_match(m["desc"], it["title"])), None)
        elif re.search(r"\b(break|lunch)\b", norm(m["desc"])):          # 'Session Break' 는 발언자/안건 마커가 아닌 별도 종류
            i = next((k for k in range(top + 1, len(items)) if items[k]["type"] == "break"), None)
        if i is None:
            continue
        if ev and ev[-1][1] == i:
            continue
        ev.append((m["t"], i)); top = max(top, i)
    return ev


def assign_day(day, lines, overrides, markers, lead):
    """하루치 문장을 아젠더 항목별로 나눈다. 반환: [(문장 목록, 경계 방식)] (항목 순서).
    안건 마커가 있으면 마커 타임라인을 따른다(진행이 예정과 달라도, 같은 안건이 여러 구간에 나뉘어도 맞게).
    마커 구간 안에서 마커 없는 항목(예정상 그 뒤에 오는 것)은 단서(발표자 이름/제목 언급)가 있을 때만 가져간다.
    안건 마커가 없거나 수동 지정이 있으면 기존 방식(단서+예정 시각)을 쓴다."""
    items = day["items"]
    ev = subject_events(day, markers) if not overrides else []
    if not ev or not lines:
        return _slices(lines, split_day(day, lines, overrides, markers, lead))

    def pos_at(t):
        for k, l in enumerate(lines):
            if l.get("raw") and l["raw"] >= t - timedelta(seconds=lead):
                return k
        return len(lines)

    bounds = [pos_at(t) for t, _ in ev]
    segs = []
    if bounds[0] > 0:
        segs.append((ev[0][1] if ev[0][1] == 0 else 0, 0, bounds[0]))      # 첫 마커 이전 문장은 그날 첫 항목에
    for k, (_, i) in enumerate(ev):
        segs.append((i, bounds[k], bounds[k + 1] if k + 1 < len(ev) else len(lines)))
    marked = {i for _, i in ev}
    out = [([], "not-reached") for _ in items]

    def put(i, ls, method):
        if ls:
            out[i] = (out[i][0] + ls, method if not out[i][0] else out[i][1])

    for k, (i, a, b) in enumerate(segs):
        sub = lines[a:b]
        if not sub:
            continue
        nxt_item = segs[k + 1][0] if k + 1 < len(segs) else n_items(items)
        followers = [f for f in range(i + 1, nxt_item) if f not in marked] if nxt_item > i else []
        if not followers:
            put(i, sub, "marker")
            continue
        pseudo = {"date": day["date"], "items": [items[i]] + [items[f] for f in followers]}
        for (ls, method), idx in zip(_slices(sub, split_day(pseudo, sub, {}, (), lead, cue_only=True)), [i] + followers):
            put(idx, ls, "marker" if idx == i else method)
    return out


def n_items(items):
    return len(items)


def slug(title, default="session"):
    s = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower())
    return s.strip("-")[:48] or default


# ---------------------------------------------------------------- 발표자별 분할
INTRO = {"welcome", "invite", "invited", "turn", "over", "floor", "hear", "next", "please", "start", "begin",
         "pass", "introduce", "joining", "give", "handing", "word", "now"}
CHAIR_WORDS = {"chair", "chairperson", "chairman", "chairwoman", "vice", "moderator"}


def read_markers(dirs):
    """webcast 발언자 마커 (collect.py 의 markers.jsonl 의 new_markers) -> 시간순 [{t(naive), desc, person}]"""
    seen, out = set(), []
    for d in dirs:
        p = os.path.join(d, "markers.jsonl")
        if not os.path.exists(p):
            continue
        for ln in open(p, encoding="utf-8"):
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            for m in row.get("new_markers") or []:
                k = json.dumps(m, sort_keys=True, ensure_ascii=False)
                if k in seen:
                    continue
                seen.add(k)
                try:
                    t = datetime.fromisoformat(m["timestampCVC5"]).replace(tzinfo=None)
                except (KeyError, ValueError):
                    continue
                out.append({"t": t, "desc": (m.get("description") or "").strip(), "person": "personId" in m,
                            "subject": "subjectId" in m})
    out.sort(key=lambda m: m["t"])
    return out


def _real(p):
    return bool(p) and p.get("name", "").strip().upper() != "TBC"


def _label(p, role):
    return (p["name"], p.get("affiliation", ""), role)


def _item_people(item):
    mod = item.get("moderator") if _real(item.get("moderator")) else None
    spk = [p for p in item.get("speakers", []) if _real(p)]
    return mod, spk


def resolve_label(desc, item):
    """마커 설명 -> (이름, 소속, 역할). 아젠더 인물과 맞으면 그 인물, 'CHAIR' 는 사회자, 그 외는 설명 그대로(국가/기관 등)."""
    dt = toks(desc)
    mod, spk = _item_people(item)
    role_spk = "panelist" if item["type"] == "panel" else "speaker"
    cands = ([(mod, "moderator")] if mod else []) + [(x, role_spk) for x in spk]
    if dt and set(dt) <= CHAIR_WORDS:
        if mod:
            return _label(mod, "moderator")
        # 'Chair Remarks' 처럼 의장 본인이 발표자로 적힌 세션이면 그 사람
        if len(spk) == 1 and "chair" in toks(item["title"]):
            return _label(spk[0], "chair")
        return ("Chair", "", "chair")
    for p, role in cands:
        if person_hit(person_tokens(p["name"]), dt):
            return _label(p, role)
    # 마커가 직함으로 찍힌 경우 (예: 'DIRECTOR GENERAL' -> 소속 '직함, 기관' 의 첫 구절): 정확히 같은 직함을 우선
    if dt:
        for exact in (True, False):
            for p, role in cands:
                title = toks((p.get("affiliation") or "").split(",")[0])
                if title and ((set(dt) == set(title)) if exact else (len(dt) >= 2 and set(dt) <= set(title))):
                    return _label(p, role)
    return (desc or "(미상)", "", "floor")


def _match_pos(ptoks, ltoks):
    pos = -1
    for w in ptoks:
        for i, t in enumerate(ltoks):
            if (len(w) < 4 and w == t) or (len(w) >= 4 and abs(len(t) - len(w)) <= 2 and
                                           difflib.SequenceMatcher(None, w, t).ratio() >= 0.84):
                pos = max(pos, i)
    return pos


def _add(turns, label, line):
    if turns and turns[-1]["label"][0] == label[0]:
        turns[-1]["lines"].append(line)
    else:
        turns.append({"label": label, "lines": [line]})


def build_turns(seg, item, markers, lead):
    """세션 문장(seg)을 발언자 교대 단위로 나눈다. 반환: (turns, 방식)  turns=[{'label':(이름,소속,역할),'lines':[...]}]"""
    mod, spk = _item_people(item)
    role_spk = "panelist" if item["type"] == "panel" else "speaker"
    raws = [l["raw"] for l in seg if l.get("raw")]
    turns = []
    # 1) 마커
    if raws:
        lo, hi = min(raws) - timedelta(seconds=lead), max(raws)
        bounds = [(m["t"] - timedelta(seconds=lead), resolve_label(m["desc"], item))
                  for m in markers if m["person"] and lo <= m["t"] <= hi]
        if bounds:
            cur = _label(mod, "moderator") if mod else ("(진행/도입)", "", "unknown")
            bi = 0
            for l in seg:
                # 문장은 끝나는 시각으로 기록되고 한 문장이 30~60초에 걸치므로, 발언자 교대는 문장의 '시작'(앞 문장이 끝난 시각)으로 판단한다
                while bi < len(bounds) and l.get("raw") and (l.get("rstart") or l["raw"]) >= bounds[bi][0]:
                    cur = bounds[bi][1]; bi += 1
                _add(turns, cur, l)
            return turns, "marker"
    # 2) 마커 없음: 발표자가 1명뿐이면 아젠더 기준
    if len(spk) == 1 and not mod:
        for l in seg:
            _add(turns, _label(spk[0], role_spk), l)
        return turns, "agenda"
    # 3) 마커 없음: 패널 - 사회자의 호명 문장에서 이름을 잡아 교대
    if spk and (mod or len(spk) > 1):
        cur = _label(mod, "moderator") if mod else ("(진행/도입)", "", "unknown")
        ptoks = {p["name"]: person_tokens(p["name"]) for p in spk}
        switched = 0
        for l in seg:
            lt = l["ltoks"]; ls = set(lt)
            hits = [(p, _match_pos(ptoks[p["name"]], lt)) for p in spk if person_hit(ptoks[p["name"]], lt)]
            if hits and (INTRO & ls):
                p = max(hits, key=lambda h: h[1])[0]
                if cur[0] != p["name"]:
                    switched += 1
                _add(turns, _label(mod, "moderator") if mod else cur, l)   # 호명 문장은 사회자 발언, 교대는 다음 줄부터
                cur = _label(p, role_spk)
                continue
            elif hits and ({"thank", "thanks"} & ls) and cur[2] == role_spk and any(h[0]["name"] == cur[0] for h in hits):
                cur = _label(mod, "moderator") if mod else ("(진행/도입)", "", "unknown")
            _add(turns, cur, l)
        if switched:
            return turns, "cue"
    # 4) 구분 불가
    turns = []
    for l in seg:
        _add(turns, ("(발표자 구분 불가)", "", "unknown"), l)
    return turns, "none"


def write_speakers(sp_dir, item, day, ent, turns, method, records):
    """발표자(이름)별로 발언을 모아 파일/레코드로 저장하고 ent['speakers'] 를 채운다."""
    by = {}
    for t in turns:
        by.setdefault(t["label"], []).append(t)
    ent["speakers"], used = [], set()
    for label, ts in by.items():
        name, affil, role = label
        key = f'{item["id"]}__{slug(name, "unknown")}'
        base, k = key, 2
        while key in used:
            key = f"{base}-{k}"; k += 1
        used.add(key)
        lines = [l for t in ts for l in t["lines"]]
        words = sum(len(l["text"].split()) for l in lines)
        turn_info = [{"start": t["lines"][0]["t"].strftime("%H:%M:%S"), "end": t["lines"][-1]["t"].strftime("%H:%M:%S"),
                      "line_from": t["lines"][0]["n"], "line_to": t["lines"][-1]["n"], "n_lines": len(t["lines"])} for t in ts]
        text = "\n\n".join("\n".join(l["text"] for l in t["lines"]) for t in ts)
        fn = key + ".txt"
        head = [f'# [{item["id"]}] {item["title"]}',
                f'# 발표자: {name}' + (f' — {affil}' if affil else "") + f'  ({role})',
                f'# Day {day["day"]} · {day["date"]} · 예정 {item["start"]}-{item["end"]} (Geneva) · 발언 {len(ts)}회 · '
                f'{len(lines)}문장 · {words:,}단어 · 구분 방식: {method}']
        with open(os.path.join(sp_dir, fn), "w", encoding="utf-8") as f:
            f.write("\n".join(head) + "\n\n" + text + "\n")
        records.append({"id": key, "session_id": item["id"], "session_title": item["title"], "session_type": item["type"],
                        "day": day["day"], "date": day["date"], "scheduled": f'{item["start"]}-{item["end"]}',
                        "speaker": name, "affiliation": affil, "role": role, "method": method,
                        "n_turns": len(ts), "n_lines": len(lines), "n_words": words, "turns": turn_info, "text": text})
        ent["speakers"].append({"key": key, "name": name, "affiliation": affil, "role": role, "n_lines": len(lines),
                                "n_words": words, "n_turns": len(ts), "file": fn, "method": method})


def people_str(it):
    parts = []
    if it.get("moderator") and it["moderator"]["name"] != "TBC":
        parts.append("Moderator: " + it["moderator"]["name"])
    sp = [p["name"] for p in it.get("speakers", []) if p["name"] != "TBC"]
    if sp:
        parts.append(("Speakers: " if len(sp) > 1 else "Speaker: ") + "; ".join(sp))
    return " | ".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inputs", action="append", required=True, help="collect.py 출력 폴더 (여러 번 지정 가능)")
    ap.add_argument("--agenda", default=os.path.join(HERE, "docs", "agenda.json"))
    ap.add_argument("--out", default=None, help="기본: 첫 --in 폴더/by_session")
    ap.add_argument("--clock", default="auto", help="auto | utc | geneva | receipt | offset:<초> (문장 시각+초=UTC)")
    ap.add_argument("--override", action="append", default=[], help="ID=HH:MM[:SS] (Geneva 시각) 시작 시각 수동 지정")
    ap.add_argument("--overrides-file", default=None, help='{"d1-04": "10:46:30"} 형태 JSON')
    ap.add_argument("--speakers-out", default=None, help="기본: <out>의 형제 폴더 by_speaker")
    ap.add_argument("--turn-lead", type=float, default=0.0, help="발언자 교대: 마커 시각보다 이만큼(초) 앞에서 시작한 문장부터 새 발언자로 본다")
    ap.add_argument("--marker-lead", type=float, default=5.0, help="마커 시각보다 이만큼(초) 앞 문장부터 새 발언자로 본다")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    out = a.out or os.path.join(a.inputs[0], "by_session")
    log = (lambda *x: None) if a.quiet else print

    agenda = json.load(open(a.agenda, encoding="utf-8"))
    overrides = {}
    if a.overrides_file and os.path.exists(a.overrides_file):
        overrides.update(json.load(open(a.overrides_file, encoding="utf-8")))
    for o in a.override:
        k, v = o.split("=", 1)
        overrides[k.strip()] = v.strip()

    rows = read_rows(a.inputs)
    if not rows:
        log("확정 문장(updates.jsonl)이 아직 없어 건너뜀")
        return 0
    clock, med, note = calibrate(rows, a.clock, agenda)
    prev, lines, last_raw = None, [], None
    rows.sort(key=lambda r: (r["src"], r["n"] or 0))
    for r in rows:
        t = to_geneva(r, clock, prev)
        if prev is not None and t < prev:
            t = prev                      # 시간 역행 방지
        prev = t
        rstart = last_raw                  # 문장 시작 시각 = 앞 문장이 끝난 시각 (첫 문장은 자기 끝 시각)
        last_raw = r["end"] or last_raw    # 마커와 비교할 '원래 시계' 시각 (없으면 앞 문장 값)
        lines.append({**r, "t": t, "raw": last_raw, "rstart": rstart or last_raw, "ltoks": toks(r["text"])})
    lines.sort(key=lambda l: l["t"])
    log(f"문장 {len(lines)}개 | 시계: {clock}" + (f" (수신-문장 중앙값 {med:.0f}s)" if med is not None else "") +
        (f" | {note}" if note else ""))

    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):             # 이전 결과 정리
        if re.match(r"^d\d+-\d+_.*\.txt$", f) or f in ("index.json", "all_by_session.md"):
            os.remove(os.path.join(out, f))
    sp_dir = a.speakers_out or os.path.join(os.path.dirname(os.path.abspath(out)), "by_speaker")
    os.makedirs(sp_dir, exist_ok=True)
    for f in os.listdir(sp_dir):
        if re.match(r"^d\d+-\d+__.*\.txt$", f) or f == "speakers.jsonl":
            os.remove(os.path.join(sp_dir, f))
    markers = read_markers(a.inputs)
    sp_records = []

    index, md = [], []
    used_dates = set()
    for day in agenda["days"]:
        dl = [l for l in lines if l["t"].strftime("%Y-%m-%d") == day["date"]]
        if not dl:
            continue
        used_dates.add(day["date"])
        assigned = assign_day(day, dl, overrides, markers, a.marker_lead)
        for i, it in enumerate(day["items"]):
            seg, method = assigned[i]
            ent = {"id": it["id"], "day": day["day"], "date": day["date"], "title": it["title"], "type": it["type"],
                   "start": it["start"], "end": it["end"], "people": people_str(it), "n_lines": len(seg),
                   "method": method, "file": None}
            if seg:
                fn = f'{it["id"]}_{slug(it["title"])}.txt'
                ent.update(file=fn, actual_start=seg[0]["t"].isoformat(timespec="seconds"),
                           actual_end=seg[-1]["t"].isoformat(timespec="seconds"),
                           line_from=seg[0]["n"], line_to=seg[-1]["n"])
                head = [f'# [{it["id"]}] {it["title"]}',
                        f'# Day {day["day"]} · {day["date"]} · 예정 {it["start"]}-{it["end"]} (Geneva)']
                if ent["people"]:
                    head.append("# " + ent["people"])
                head.append(f'# 실제 구간 {seg[0]["t"].strftime("%H:%M:%S")}-{seg[-1]["t"].strftime("%H:%M:%S")} · '
                            f'{len(seg)}문장 · 경계 방식: {method}')
                with open(os.path.join(out, fn), "w", encoding="utf-8") as f:
                    f.write("\n".join(head) + "\n\n" + "\n".join(l["text"] for l in seg) + "\n")
                if it["type"] not in ("break", "social"):
                    turns, smeth = build_turns(seg, it, markers, a.turn_lead)
                    write_speakers(sp_dir, it, day, ent, turns, smeth, sp_records)
                md.append(f'## [{it["id"]}] {it["title"]}\n\n_Day {day["day"]} {it["start"]}-{it["end"]}'
                          + (f' · {ent["people"]}' if ent["people"] else "") + "_\n\n"
                          + "\n".join(l["text"] for l in seg) + "\n")
            index.append(ent)
    skipped = sum(1 for l in lines if l["t"].strftime("%Y-%m-%d") not in used_dates)
    if skipped:
        log(f"아젠더 날짜 밖의 문장 {skipped}개는 제외")
    json.dump({"generated": datetime.now(UTC).isoformat(timespec="seconds"), "clock": clock,
               "clock_note": note, "items": index}, open(os.path.join(out, "index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    open(os.path.join(out, "all_by_session.md"), "w", encoding="utf-8").write("\n".join(md))
    with open(os.path.join(sp_dir, "speakers.jsonl"), "w", encoding="utf-8") as f:
        for rec in sp_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    for e in index:
        if e["n_lines"]:
            log(f'  {e["id"]:6} {e["start"]}-{e["end"]}  {e["n_lines"]:5}문장  {e["method"]:9} {e["title"][:60]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
