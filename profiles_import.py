#!/usr/bin/env python3
"""연사 프로필 PDF (WIPO 'Speakers Bio') -> docs/profiles/profiles.json + 사진 썸네일.

PDF 의 각 프로필은 [사진(왼쪽) + 이름(굵게) + 직함/소속 + 약력 문단] 구조다. pdfplumber 로 줄 위치와 이미지 위치를
읽어 사진 오른쪽 위의 줄을 이름으로 삼고, 다음 프로필 전까지를 직함(첫 문단)과 약력으로 모은다.
세션 제목(예: 'Chair Remarks')은 앞뒤 간격이 큰 한 줄짜리 제목으로 감지해 프로필에 붙인다.

사용: python3 profiles_import.py 프로필.pdf --out docs/profiles   (pdfplumber, Pillow 필요 — 가져올 때 한 번만 실행)
"""
import argparse
import io
import json
import os
import re
import sys
import unicodedata

import pdfplumber
from PIL import Image


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "person"


def thumb(im, size=192):
    img = Image.open(io.BytesIO(im["stream"].get_data())).convert("RGB")
    w, h = img.size
    m = min(w, h)
    img = img.crop(((w - m) // 2, (h - m) // 2, (w - m) // 2 + m, (h - m) // 2 + m)).resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80, optimize=True)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--out", default="docs/profiles")
    a = ap.parse_args()
    img_dir = os.path.join(a.out, "img")
    os.makedirs(img_dir, exist_ok=True)
    for f in os.listdir(img_dir):
        os.remove(os.path.join(img_dir, f))

    pdf = pdfplumber.open(a.pdf)
    # 1) 페이지별 줄/사진 수집 (머리글 'Speakers', 쪽번호 제외)
    items = []                                              # (page, top, kind, obj)
    for pn, pg in enumerate(pdf.pages, 1):
        if pn == 1:
            continue                                        # 표지
        for ln in pg.extract_text_lines(return_chars=True):
            t = ln["text"].strip()
            if not t or (ln["top"] < 50 and ln["x0"] > 450) or ln["top"] > 755 or re.fullmatch(r"\d+", t):
                continue
            items.append((pn, ln["top"], "line", {"x0": ln["x0"], "top": ln["top"], "text": t, "page": pn}))
        for im in pg.images:
            w, h = im["x1"] - im["x0"], im["bottom"] - im["top"]
            if 60 <= w <= 140 and 60 <= h <= 140:
                items.append((pn, im["top"], "img", {"x0": im["x0"], "x1": im["x1"], "top": im["top"], "page": pn, "im": im}))
    items.sort(key=lambda x: (x[0], x[1], 0 if x[2] == "img" else 1))

    # 2) 이미지 + 오른쪽 위 이름 줄 = 프로필 시작. 간격 큰 한 줄 제목 = 세션 제목
    lines_by_page = {}
    for pn, _, k, o in items:
        if k == "line":
            lines_by_page.setdefault(pn, []).append(o)
    starts = {}                                             # id(name line) -> image
    for pn, _, k, o in items:
        if k != "img":
            continue
        cand = [l for l in lines_by_page.get(pn, []) if l["x0"] >= o["x1"] - 8 and -12 <= l["top"] - o["top"] <= 30]
        if cand:
            starts[id(min(cand, key=lambda l: abs(l["top"] - o["top"])))] = o
    seq = [o for _, _, k, o in items if k == "line"]
    honor = re.compile(r"^(H\.? ?E\.|Mr\.|Ms\.|Mrs\.|Dr\.?|Prof\.?|Fr\.|Hon\.|Amb\.)\s")
    for i, l in enumerate(seq):                            # 사진이 없는 프로필: 경칭으로 시작하는 짧은 줄 + 앞 공백 + 뒤 직함 줄
        if id(l) in starts or l["x0"] < 150 or len(l["text"]) > 55 or not honor.match(l["text"]):
            continue
        prev, nxt = (seq[i - 1] if i else None), (seq[i + 1] if i + 1 < len(seq) else None)
        gap_b = (l["top"] - prev["top"]) if prev and prev["page"] == l["page"] else 99
        gap_a = (nxt["top"] - l["top"]) if nxt and nxt["page"] == l["page"] else 99
        if gap_b >= 25 and (20 <= gap_a <= 40 or gap_a == 99):        # 99: 다음 줄이 다음 쪽(이름이 쪽 맨 아래)
            starts[id(l)] = None
    session, people, cur = "", [], None
    for i, l in enumerate(seq):
        if id(l) in starts:
            cur = {"name": l["text"], "session": session, "page": l["page"], "im": starts[id(l)]["im"] if starts[id(l)] else None, "lines": []}
            people.append(cur)
            continue
        prev, nxt = (seq[i - 1] if i else None), (seq[i + 1] if i + 1 < len(seq) else None)
        gap_b = (l["top"] - prev["top"]) if prev and prev["page"] == l["page"] else 99
        gap_a = (nxt["top"] - l["top"]) if nxt and nxt["page"] == l["page"] else 99
        if l["x0"] < 80 and len(l["text"]) < 90 and gap_b >= 35 and gap_a >= 35 and not l["text"].endswith((".", ",")):
            session, cur = l["text"], None                  # 세션 제목: 이후 줄은 다음 프로필의 이름 전까지 버린다
            continue
        if cur is not None:
            cur["lines"].append(l)

    # 3) 문단 구성: 첫 문단=직함, 나머지=약력 (줄 간격 17pt 초과면 새 문단, 쪽을 넘어 이어지는 문단은 합침)
    out = []
    for p in people:
        paras, last = [], None
        for l in p["lines"]:
            new = last is None or l["page"] != last["page"] or l["top"] - last["top"] > 17 or re.match(r"^[-•]\s", l["text"])
            if new:
                paras.append([l["text"]])
            else:
                paras[-1].append(l["text"])
            last = l
        texts = [" ".join(x) for x in paras]
        merged = []
        for t in texts:                                     # 쪽 경계에서 끊긴 문장 이어붙이기
            if merged and not re.search(r"[.!?:”\")]$", merged[-1]) and t[:1].islower():
                merged[-1] += " " + t
            else:
                merged.append(t)
        title = merged[0] if merged else ""
        bio = [re.sub(r"^-\s+", "• ", t) for t in merged[1:]]
        pid = slug(p["name"])
        fn = pid + ".jpg"
        try:
            if p["im"] is None:
                raise LookupError("PDF 에 사진 없음")
            open(os.path.join(img_dir, fn), "wb").write(thumb(p["im"]))
        except Exception as e:                              # 사진이 없거나 못 읽어도 프로필 텍스트는 남긴다
            if not isinstance(e, LookupError):
                print("사진 실패:", p["name"], e, file=sys.stderr)
            fn = None
        out.append({"id": pid, "name": re.sub(r"\bH\.\s*E\.", "H.E.", p["name"]), "title": title, "session": p["session"], "page": p["page"],
                    "bio": bio, "photo": f"img/{fn}" if fn else None})
    json.dump({"source": os.path.basename(a.pdf), "people": out}, open(os.path.join(a.out, "profiles.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"{len(out)}명 · 사진 {sum(1 for o in out if o['photo'])}장")
    for o in out:
        print(f'  p{o["page"]:<3} {o["name"][:34]:34} | {o["title"][:60]:60} | bio {len(o["bio"])}문단 | {o["session"][:30]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
