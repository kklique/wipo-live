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
    return list(rows.values())


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
def split_day(day, lines, overrides):
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
        else:
            E = S + drift
            c = build_cues(it)
            for k in range(prev, len(lines)):
                t = lines[k]["t"]
                if t > E + WIN_AFTER:
                    break
                if t >= E - WIN_BEFORE and score(c, lines[k]["ltoks"]) >= THRESHOLD:
                    pos = k + 1 if it["type"] == "break" else k
                    method, anchor = "cue", t
                    break
            if pos is None:
                pos = next((k for k in range(prev, len(lines)) if lines[k]["t"] >= E), None)
                method = "schedule" if pos is not None else "not-reached"
        if pos is not None:
            pos = max(pos, prev)
            if anchor is not None:
                drift = min(DRIFT_MAX, max(DRIFT_MIN, anchor - S))
            prev = pos
        out.append((pos, method))
    return out


def slug(title):
    s = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower())
    return s.strip("-")[:48] or "session"


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
    prev, lines = None, []
    rows.sort(key=lambda r: (r["src"], r["n"] or 0))
    for r in rows:
        t = to_geneva(r, clock, prev)
        if prev is not None and t < prev:
            t = prev                      # 시간 역행 방지
        prev = t
        lines.append({**r, "t": t, "ltoks": toks(r["text"])})
    lines.sort(key=lambda l: l["t"])
    log(f"문장 {len(lines)}개 | 시계: {clock}" + (f" (수신-문장 중앙값 {med:.0f}s)" if med is not None else "") +
        (f" | {note}" if note else ""))

    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):             # 이전 결과 정리
        if re.match(r"^d\d+-\d+_.*\.txt$", f) or f in ("index.json", "all_by_session.md"):
            os.remove(os.path.join(out, f))

    index, md = [], []
    used_dates = set()
    for day in agenda["days"]:
        dl = [l for l in lines if l["t"].strftime("%Y-%m-%d") == day["date"]]
        if not dl:
            continue
        used_dates.add(day["date"])
        res = split_day(day, dl, overrides)
        starts = [p for p, _ in res]
        for i, it in enumerate(day["items"]):
            pos, method = res[i]
            nxt = next((p for p in starts[i + 1:] if p is not None), len(dl))
            seg = dl[pos:max(pos, nxt)] if pos is not None else []
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
    for e in index:
        if e["n_lines"]:
            log(f'  {e["id"]:6} {e["start"]}-{e["end"]}  {e["n_lines"]:5}문장  {e["method"]:9} {e["title"][:60]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
