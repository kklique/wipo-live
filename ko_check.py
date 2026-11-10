#!/usr/bin/env python3
"""한국어 번역·요약 검사기 (slop-rewrite 의 'AI 티 원칙' 중 번역문에 해당하는 것만 채택).

https://github.com/byungjunjang/slop-rewrite 의 원칙(principles/ai-tells-ko.md)에서 번역·요약 글에 맞는 것을 골랐다.
- 검사기는 문장을 고치지 않고 위반 위치만 센다. 고치는 일은 번역 작업(LLM)이 하고, 뜻이 바뀌지 않았는지는 사람이 확인한다.
- 연설 전사본의 번역이므로 서두·결말 규칙(요약 마무리, 덕담 등)은 원문 발언 자체라 채택하지 않았다.
- 채택한 것: 이중 피동(AT-31), 줄표 남용(AT-18), 콜론 극적 공개(AT-19), 판단 회피 종결(AT-29), '필요' 남발(AT-30),
  군더더기 구문(AT-28), 중요성 과장(AT-26), 정형 결말(AT-12), 번역투 조사·피동(AT-41), 직역 어휘(AT-47~57 중 일반 항목),
  정도부사 중독(AT-32), 무생물 주어+인간 동사(AT-22).

사용: python3 ko_check.py docs/official/day1-am/ko.json [...]   (ko.json: {번호: 번역}, summaries.json, profiles.json)
"""
import json
import re
import sys

# (ID, 이름, 정규식, 단락당 허용 횟수) — 허용 횟수를 넘으면 위반. 0 이면 한 번만 나와도 위반.
RULES = [
    ("AT-31", "이중 피동", r"(되어지|되어진|시켜지|시켜진|불리어지|보여지|쓰여지|만들어지게 되)", 0),
    ("AT-18", "줄표", r"(?<!\d)[—–]|[—–](?!\d)", 0),            # 연도·수치 범위(2023–2024)는 예외
    ("AT-19", "콜론 극적 공개", r"(입니다|습니다|이다|했다|한다)\s*:", 0),
    ("AT-29", "판단 회피 종결", r"(라고 할 수 있|라고 볼 수 있|라 할 수 있|할 수 있다고 (생각|봅))", 1),
    ("AT-30", "'필요' 남발", r"(할 필요가 있|할 필요가 있습니다|될 필요가 있)", 1),
    ("AT-28", "군더더기 구문", r"(하기 위한 목적으로|하는 데 있어서?|에 있어서|현재 시점에서|하는 것이 가능합니다)", 1),
    ("AT-26", "중요성 과장", r"(획기적|패러다임 전환|분수령|혁명적|게임 체인저)", 0),
    ("AT-12", "정형 결말", r"(새로운 지평|시대가 도래|할 때입니다|새 장을 열)", 0),
    ("AT-41", "번역투 조사·피동", r"(에 의해서?|에 의하여|로서의|으로서의|에 대하여|에 대해서?|에 대한|[을를] 통해서?|에 관한 것입니다)", 2),
    ("AT-41b", "대명사 직역", r"(그것(은|을|이|의|도)|그들(은|을|이|의)?|그녀(는|를|가|의)?)", 1),
    ("AT-41c", "'~것입니다' 연속", r"것(입니다|이다|이었습니다)", 2),
    ("AT-29b", "'~할 수 있' 과다", r"할 수 있", 2),
    ("AT-47", "직역 공식", r"(바로 여기에서 시작|갈래로 갈립|또 하나의 축)", 0),
    ("AT-56", "'~에 가깝습니다'", r"에 가깝(습니다|다)", 1),
    ("AT-32", "정도부사 중독", r"(정말|매우|아주|굉장히|대단히)", 3),
    ("AT-22", "무생물 주어+인간 동사", r"(데이터|결과|연구|보고서|통계|기술)(는|가|은|이) [^.]{0,30}(말해 준다|말해 줍니다|증명한다|증명합니다|보여 줍니다|웅변)", 0),
    ("AT-44", "'단순한 X가 아니라 Y'", r"단순한? [^.]{0,20}(가|이) 아니라", 0),
]


def check(text):
    out = []
    for rid, name, pat, allow in RULES:
        n = len(re.findall(pat, text))
        if n > allow:
            out.append((rid, name, n))
    return out


def iter_texts(path):
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict) and "people" in d:                       # profiles.json
        for p in d["people"]:
            yield p["id"] + ":title", p.get("title_ko") or ""
            for i, t in enumerate(p.get("bio_ko") or []):
                yield f'{p["id"]}:bio{i}', t
    elif isinstance(d, dict) and ("sessions" in d or "speakers" in d):   # summaries.json
        for grp in ("sessions", "speakers"):
            for k, v in (d.get(grp) or {}).items():
                yield f"{grp}:{k}:summary", v.get("summary", "")
                for i, t in enumerate(v.get("points") or []):
                    yield f"{grp}:{k}:p{i}", t
    else:                                                           # ko.json {번호: 번역}
        for k, t in d.items():
            yield k, t


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    tot, flagged = 0, 0
    by_rule = {}
    for path in [a for a in sys.argv[1:] if not a.startswith("-")]:
        for key, t in iter_texts(path):
            tot += 1
            hits = check(t)
            if hits:
                flagged += 1
                for rid, name, n in hits:
                    by_rule[(rid, name)] = by_rule.get((rid, name), 0) + n
                if "-v" in sys.argv:
                    print(path.split("/")[-1], key, [(h[0], h[2]) for h in hits])
    print(f"검사 {tot}건 · 위반 {flagged}건")
    for (rid, name), n in sorted(by_rule.items(), key=lambda x: -x[1]):
        print(f"  {rid} {name}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
