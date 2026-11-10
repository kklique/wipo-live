#!/usr/bin/env python3
"""WIPO 공식 verbatim 스크립트(.docx)를 가져와 안건별/발언자별 파일로 나눈다 (표준 라이브러리만 사용).

WIPO 가 행사 후 배포하는 'WIPO S2T ... verbatim' 문서는 안건 표지('<< 제목 >>')와 발언자 표지('이름 [hh:mm:ss]')가
들어 있어, 실시간 수집분(STT)보다 경계가 정확하다. 이 스크립트는 문서를 읽어

  docs/official/<이름>/
    transcript.txt            전체 (■ 안건 / ▌ 발언자 표지 줄 + 단락)
    by_session/<id>_<slug>.txt, index.json
    by_speaker/<id>__<slug>.txt, speakers.jsonl
    info.json                 출처, 시계 보정값, 실시간 수집분과의 대조 결과
    <원본>.docx               원본 사본
  docs/official/index.json    공식본 목록 (뷰어가 읽음)

을 만든다. 실시간 수집분(data 브랜치의 updates.jsonl, transcript.txt, by_session/ ...)은 건드리지 않는다.
뷰어의 프로그램 탭은 공식본이 있는 안건은 이 공식본을 우선 보여주고, 전체 저장본 탭에서는 선택해 볼 수 있다.

사용: python3 official_import.py 문서.docx --name day1-am --label "Day 1 오전" --out docs/official
        [--agenda docs/agenda.json] [--markers markers.jsonl] [--transcript transcript.txt]
"""
import argparse
import json
import os
import re
import shutil
import statistics
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import split_by_agenda as sba

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
UTC = timezone.utc


def read_paragraphs(path):
    """docx -> [(스타일, 텍스트)] (문서 순서)"""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(W + "p"):
        st = p.find(f"{W}pPr/{W}pStyle")
        style = st.get(W + "val") if st is not None else ""
        txt = []
        for el in p.iter():
            if el.tag == W + "t" and el.text:
                txt.append(el.text)
            elif el.tag in (W + "br", W + "tab"):
                txt.append(" ")
        out.append((style, re.sub(r"\s+", " ", "".join(txt)).strip()))
    return out


SPK = re.compile(r"^(.*?)\s*\[(\d+):(\d\d):(\d\d)\]$")
SEC = re.compile(r"^<<\s*(.*?)\s*>>$")


def parse(paras):
    """-> (title, events). events: ('section', 제목) | ('turn', 이름, 시작초) | ('text', 문단)"""
    title, events = "", []
    for style, t in paras:
        if not t:
            continue
        if style == "Title":
            title = t
        elif style == "S2TSpeaker":
            m = SEC.match(t)
            if m:
                events.append(("section", m.group(1)))
                continue
            m = SPK.match(t)
            if m:
                events.append(("turn", m.group(1).strip(), int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))))
        elif style == "S2TText":
            events.append(("text", t))
    return title, events


def words(s):
    return re.findall(r"[a-z0-9']+", s.lower())


def shingles(ws, n=4):
    return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("docx")
    ap.add_argument("--agenda", default="docs/agenda.json")
    ap.add_argument("--out", default="docs/official", help="공식본 루트 폴더 (그 아래 --name 폴더와 index.json 을 만든다)")
    ap.add_argument("--name", required=True, help="이 문서의 폴더 이름 (예: day1-am)")
    ap.add_argument("--label", help="뷰어에 표시할 이름 (예: Day 1 오전)")
    ap.add_argument("--markers", help="실시간 수집 markers.jsonl (시계 보정용)")
    ap.add_argument("--transcript", help="실시간 수집 transcript.txt (대조용)")
    a = ap.parse_args()

    title, events = parse(read_paragraphs(a.docx))
    m = re.search(r"(\d{4}-\d\d-\d\d)", title)
    if not m:
        print("문서 제목에서 날짜를 찾지 못했습니다:", title, file=sys.stderr)
        return 1
    date = m.group(1)
    agenda = json.load(open(a.agenda, encoding="utf-8"))
    day = next((d for d in agenda["days"] if d["date"] == date), None)
    if not day:
        print("아젠더에 없는 날짜:", date, file=sys.stderr)
        return 1
    items = day["items"]

    # 1) 턴(발언) 목록: 안건 표지가 바뀔 때마다 그 안건으로, 'Session Break' 는 다음 휴식 항목으로
    turns, cur_sec, cur_item = [], None, None

    def item_for(sec):
        return next((i for i, it in enumerate(items) if sba.title_match(sec, it["title"])), None)

    for ev in events:
        if ev[0] == "section":
            cur_sec = ev[1]
            cur_item = item_for(cur_sec)
            if cur_item is None:
                print("아젠더와 맞지 않는 안건 표지:", cur_sec, file=sys.stderr)
        elif ev[0] == "turn":
            name, off = ev[1], ev[2]
            if re.fullmatch(r"(?i)session break", name):
                nb = next((i for i in range((cur_item or 0) + 1, len(items)) if items[i]["type"] == "break"), None)
                if nb is not None:
                    cur_item = nb
            turns.append({"raw": name, "off": off, "item": cur_item if cur_item is not None else 0, "sec": cur_sec, "paras": []})
        elif ev[0] == "text" and turns:
            turns[-1]["paras"].append(ev[1])
    for t in turns:
        t["paras"] = [p for p in t["paras"] if p.strip()]

    # 2) 표지 없는 후속 항목(예: 의장 발언): 구간 안에서 그 항목 발표자 이름과 같은 발언은 그 항목으로
    order = []                                     # 구간 순서 [(item, turn 인덱스 범위)]
    for k, t in enumerate(turns):
        if not order or order[-1][0] != t["item"]:
            order.append([t["item"], k, k])
        order[-1][2] = k
    marked = {o[0] for o in order}
    for n, (i, a0, a1) in enumerate(order):
        nxt = order[n + 1][0] if n + 1 < len(order) else len(items)
        followers = [f for f in range(i + 1, nxt) if f not in marked] if nxt > i else []
        for f in followers:
            people = [p for p in ([items[f].get("moderator")] if items[f].get("moderator") else []) + list(items[f].get("speakers", []))
                      if p and p["name"] != "TBC"]
            for k in range(a0, a1 + 1):
                if any(sba.person_hit(sba.person_tokens(p["name"]), sba.toks(turns[k]["raw"])) for p in people):
                    turns[k]["item"] = f

    # 3) 시계 보정: 실시간 수집 마커(절대 UTC)와 문서 오프셋을 이름으로 맞춰 중앙값
    base, cands, note = None, [], "markers 없음: 시계 보정 불가 (실제 시각 표시 생략)"
    if a.markers and os.path.exists(a.markers):
        seen = set()
        for ln in open(a.markers, encoding="utf-8"):
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            for mk in row.get("new_markers") or []:
                if not mk.get("personId") or not mk.get("timestampCVC5"):
                    continue
                k = (mk["description"], mk["timestampCVC5"])
                if k in seen:
                    continue
                seen.add(k)
                t = datetime.fromisoformat(mk["timestampCVC5"]).replace(tzinfo=UTC)
                dt = set(sba.toks(mk["description"]))
                for tn in turns:                               # 같은 이름의 모든 발언이 후보 (이름이 반복되므로 다수결로 고른다)
                    if dt and set(sba.toks(tn["raw"])) == dt:
                        cands.append(t - timedelta(seconds=tn["off"]))
        if cands:
            best = max(cands, key=lambda c: sum(1 for o in cands if abs((o - c).total_seconds()) <= 15))
            near = [o for o in cands if abs((o - best).total_seconds()) <= 15]
            near.sort()
            base = near[len(near) // 2]
            note = f"웹캐스트 마커 {len(near)}/{len(cands)}개가 일치 (편차 ±{max(abs((o - base).total_seconds()) for o in near):.0f}s)"
    gva = timezone(timedelta(hours=1))                 # 11월 Geneva = UTC+1 (행사 기간 동일)
    for t in turns:
        t["t"] = (base + timedelta(seconds=t["off"])).astimezone(gva) if base else None

    # 4) 안건별로 모아 파일 작성
    out = os.path.join(a.out, a.name)
    sess_dir, sp_dir = os.path.join(out, "by_session"), os.path.join(out, "by_speaker")
    for d in (sess_dir, sp_dir):
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
    pn = 0
    for t in turns:                                      # 전체 단락 번호
        t["nums"] = []
        for _ in t["paras"]:
            pn += 1
            t["nums"].append(pn)
    index, sp_records, md = [], [], []
    for i, it in enumerate(items):
        ts = [t for t in turns if t["item"] == i]
        ent = {"id": it["id"], "day": day["day"], "date": day["date"], "title": it["title"], "type": it["type"],
               "start": it["start"], "end": it["end"], "people": sba.people_str(it), "n_lines": 0, "method": "official",
               "file": None, "official": True}
        if ts and any(t["paras"] for t in ts):
            lines_n = sum(len(t["paras"]) for t in ts)
            nwords = sum(len(p.split()) for t in ts for p in t["paras"])
            ent["n_lines"] = lines_n
            ent["paras"] = [n for t in ts for n in t["nums"]]            # 전체 단락 번호 (번역 정렬용)
            fn = f'{it["id"]}_{sba.slug(it["title"])}.txt'
            ent["file"] = fn
            if ts[0]["t"]:
                ent["actual_start"] = ts[0]["t"].isoformat(timespec="seconds")
                nxt_off = next((t2["off"] for t2 in turns if t2["off"] > ts[-1]["off"]), None)
                if nxt_off is not None:
                    ent["actual_end"] = (base + timedelta(seconds=nxt_off)).astimezone(gva).isoformat(timespec="seconds")
            head = [f'# [{it["id"]}] {it["title"]}', f'# Day {day["day"]} · {day["date"]} · 예정 {it["start"]}-{it["end"]} (Geneva)']
            if ent["people"]:
                head.append("# " + ent["people"])
            head.append(f'# 공식 스크립트(WIPO verbatim) · 문서 시각 {ts[0]["off"] // 3600:02d}:{ts[0]["off"] % 3600 // 60:02d}:{ts[0]["off"] % 60:02d}~ · '
                        f'{lines_n}단락 · {nwords:,}단어')
            body = []
            for t in ts:
                body.extend(t["paras"])
            with open(os.path.join(sess_dir, fn), "w", encoding="utf-8") as f:
                f.write("\n".join(head) + "\n\n" + "\n".join(body) + "\n")
            if it["type"] not in ("break", "social"):
                tl = []
                for t in ts:
                    label = ("(휴식/진행)", "", "unknown") if re.fullmatch(r"(?i)session break", t["raw"]) else sba.resolve_label(t["raw"], it)
                    tt = t["t"] or datetime(2000, 1, 1, tzinfo=gva)
                    lines = [{"t": tt, "n": n, "text": p} for n, p in zip(t["nums"], t["paras"])]
                    if not lines:
                        continue
                    if tl and tl[-1]["label"][0] == label[0]:
                        tl[-1]["lines"].extend(lines)
                    else:
                        tl.append({"label": label, "lines": lines})
                sba.write_speakers(sp_dir, it, day, ent, tl, "official", sp_records)
                nums_by = {}
                for t in tl:
                    nums_by.setdefault(t["label"][0], []).extend(l["n"] for l in t["lines"])
                for sp in ent.get("speakers", []):
                    sp["paras"] = nums_by.get(sp["name"], [])
                for sp in ent.get("speakers", []):            # 단락 단위이므로 머리글의 '문장' 표기를 바꾼다
                    fp = os.path.join(sp_dir, sp["file"])
                    txt = open(fp, encoding="utf-8").read().replace("문장 ·", "단락 ·", 1)
                    open(fp, "w", encoding="utf-8").write(txt)
            md.append(f'## [{it["id"]}] {it["title"]}\n\n' + "\n".join(body) + "\n")
        index.append(ent)

    # 5) 전체 (표지 줄 포함) + 단락 목록(번역 입력/정렬용)
    with open(os.path.join(out, "paragraphs.json"), "w", encoding="utf-8") as f:
        json.dump([{"n": n, "item": items[t["item"]]["id"], "speaker": t["raw"], "off": t["off"], "text": p}
                   for t in turns for n, p in zip(t["nums"], t["paras"])], f, ensure_ascii=False)
    with open(os.path.join(out, "transcript.txt"), "w", encoding="utf-8") as f:
        last_sec = None
        for t in turns:
            it = items[t["item"]]
            if t["sec"] != last_sec:
                f.write(f'■ {t["sec"]}\n')
                last_sec = t["sec"]
            f.write(f'▌ [{t["off"] // 3600:02d}:{t["off"] % 3600 // 60:02d}:{t["off"] % 60:02d}] {t["raw"]}\n')
            for p in t["paras"]:
                f.write(p + "\n")
    json.dump({"generated": datetime.now(UTC).isoformat(timespec="seconds"), "source": os.path.basename(a.docx),
               "official": True, "unit": "단락", "items": index},
              open(os.path.join(sess_dir, "index.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(os.path.join(sess_dir, "all_by_session.md"), "w", encoding="utf-8").write("\n".join(md))
    with open(os.path.join(sp_dir, "speakers.jsonl"), "w", encoding="utf-8") as f:
        for r in sp_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    shutil.copyfile(a.docx, os.path.join(out, os.path.basename(a.docx)))

    # 6) 실시간 수집분과 대조 (4단어 연속 구간 일치율)
    off_words = [w for t in turns for p in t["paras"] for w in words(p)]
    info = {"source": os.path.basename(a.docx), "title": title, "date": date, "generated": datetime.now(UTC).isoformat(timespec="seconds"),
            "turns": len(turns), "paragraphs": pn, "words": len(off_words),
            "base_utc": base.isoformat(timespec="seconds") if base else None, "clock_note": note,
            "sessions": {e["id"]: e["n_lines"] for e in index if e["n_lines"]}}
    if a.transcript and os.path.exists(a.transcript):
        stt = [ln.strip() for ln in open(a.transcript, encoding="utf-8") if ln.strip()]
        tail = " ".join(words(turns[-1]["paras"][-2] if len(turns[-1]["paras"]) > 1 else turns[-1]["paras"][-1])[:6])
        end = next((k for k, ln in enumerate(stt) if "it s lunch break" in " ".join(words(ln.replace("'", " ")))
                    or "lunch break and we resume" in ln.lower()), None)
        if end is not None:
            stt = stt[:end + 2]                              # 점심 안내 문장까지 = 공식본이 끝나는 곳
        osh = shingles(off_words)
        cov, low = [], []
        for ln in stt:
            sh = shingles(words(ln))
            if len(sh) >= 3:
                c = len(sh & osh) / len(sh)
                cov.append(c)
                if c < 0.3:
                    low.append(ln[:100])
        ssh = shingles([w for ln in stt for w in words(ln)])
        ocov = [len(shingles(words(p)) & ssh) / max(1, len(shingles(words(p)))) for t in turns for p in t["paras"] if len(shingles(words(p))) >= 3]
        info["compare"] = {"stt_lines": len(stt), "stt_words": sum(len(words(l)) for l in stt),
                           "stt_lines_found_in_official_pct": round(100 * sum(1 for c in cov if c >= 0.5) / max(1, len(cov)), 1),
                           "official_paras_found_in_stt_pct": round(100 * sum(1 for c in ocov if c >= 0.5) / max(1, len(ocov)), 1),
                           "stt_lines_not_in_official_sample": low[:15]}
    json.dump(info, open(os.path.join(out, "info.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    man_p = os.path.join(a.out, "index.json")
    man = json.load(open(man_p, encoding="utf-8")) if os.path.exists(man_p) else {"items": []}
    man["items"] = [m for m in man["items"] if m["key"] != a.name] + [
        {"key": a.name, "label": a.label or a.name, "date": date, "source": os.path.basename(a.docx),
         "paragraphs": pn, "words": len(off_words), "sessions": info["sessions"]}]
    man["items"].sort(key=lambda m: (m["date"], m["key"]))
    json.dump(man, open(man_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
