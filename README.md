# wipo-live

WIPO Live Speech-to-Text(`webcast.wipo.int/live-speech-to-text`)의 영문 실시간 스크립트를 수집해 저장한다.

페이지가 내부적으로 폴링하는 `https://www.wipo.int/s2t/live_s2t/getLiveTranscript?meeting_id=…&line_number=N` 를
`collect.py` 가 직접 호출한다. (`full_lines` = 확정 문장, `last_line` = 진행 중 문장, `next_line_number` = 이어받기 위치)

## GitHub Actions 로 실행

1. 이 워크플로가 **기본 브랜치**에 있어야 Actions 탭에서 실행 버튼이 보인다 (PR 머지 또는 기본 브랜치 지정).
2. Actions 탭 → **Collect WIPO live transcript** → **Run workflow**
   - `meeting_id`: URL의 `meeting_id=` 값 (기본값은 현재 대상 회의)
   - 회의 시작 전에 눌러도 된다. 503 이면 30초마다 재시도하며 기다린다.
   - 데이터를 받은 뒤 `idle_exit_minutes`(기본 180분) 동안 변화가 없으면 자동 종료.
3. 결과는 2분마다 **`data` 브랜치**의 `transcripts/<meeting_id>/` 에 커밋되고, 종료 시 Artifact 로도 올라간다.

| 파일 | 내용 |
|---|---|
| `transcript.txt` | 확정 문장만, 한 줄에 한 문장 |
| `updates.jsonl` | 모든 갱신 기록 (확정 문장 + 진행 중 문장의 변경 스냅샷, 수신 시각 포함) |
| `state.json` | 이어받기 위치 (재실행하면 이어서 저장) |
| `markers.jsonl` | 발언자 마커 변경 기록. webcast `liveSessions.json`(종료 후엔 `history_liveSessions.json`)에서 이 회의의 세션을 찾아 30초마다 확인하고, 새 마커(`new_markers`: 발언자/안건 + `timestampCVC5` 시각)나 상태 변화가 있을 때만 한 줄 추가 |
| `session_latest.json` | 매칭된 webcast 세션 정보 전체의 최신본 |

### 발언자별 분리에 대해
라이브 전사 API 자체에는 화자 정보가 없다. 화자는 WIPO 회의 시스템이 찍는 **발언자 마커**(`markers.jsonl`)의 시각을
`updates.jsonl` 문장의 `end_time_iso` 와 대조해서 사후에 나눌 수 있다. 마커는 사람이 발언권을 줄 때 찍히므로
패널 토론 등에서는 경계가 부정확할 수 있고, 라이브 중 마커가 실제로 채워지는지는 회의가 시작되어야 확인된다.

### 제한 사항
- GitHub 호스티드 러너의 job 은 최대 6시간. 더 길면 종료 후 같은 meeting_id 로 다시 실행하면 `data` 브랜치의 `state.json` 부터 이어받는다.
- **저장소가 public 이면 수집 결과(`data` 브랜치)와 Actions 로그가 모두 공개된다.** 공개를 원치 않으면 private 로 둘 것.
  (private 저장소는 무료 플랜 기준 월 Actions 사용 시간 한도가 있다.)

## 저장 누락 방지 (안전장치)

| 겹 | 장치 | 하는 일 |
|---|---|---|
| 1 | **수집기** (`collect.py`) | 저장된 줄 번호(`state.json`)부터 이어받음. 일시 오류/503 은 계속 재시도. 늦게 시작해도 WIPO 가 보관한 지난 문장을 1번부터 받아옴(`catchup` 표시). 줄 번호가 이어지지 않으면 `gap` 기록 + 경고 |
| 2 | **수집 워크플로** | 수집기가 비정상 종료하면 작업 안에서 저장 위치부터 자동 재시작(20회 넘게 죽으면 작업 실패 처리). `start_line` 입력으로 특정 줄부터 다시 받을 수도 있음 |
| 3 | **Watchdog** (`watchdog.yml`) | **두 스트림(`<해시>_no_delay`, `<해시>`)을 각각 독립적으로 수집**(한쪽이 멈춰도 다른 쪽에 저장; 실제로 `_no_delay` 가 10:17 UTC 에 멈춘 적 있음).  행사 날짜(11/9–11) UTC 08–17시에 5분마다(오후 세션 직전 14:00 UTC 전후에는 추가 점검): **회의 시간대(시작 20분 전~종료 30분 후)에는 수집 작업을 항상 하나 유지**(점심 휴회·세션 시작 전에도 대기용으로 시작 → 오후 첫 문장부터 저장). 새 전사가 있는데 수집 작업이 없으면 **자동 시작**, 떠 있는데 12분 넘게 저장 위치가 안 바뀌면 **취소 후 재시작**. 러너에서 WIPO 접속 불가/반복 실패는 **GitHub 이슈**로 알림. 첫 수집 시작도 이슈로 알림 |

Watchdog 은 기본 meeting_id(`db9f…`)와 webcast 에서 '진행 중'인 세션의 id, `data` 브랜치에 이미 있는 id 를 감시한다.
다른 id(예: 11/11 AIII)는 진행 중 세션으로 자동 감지되며, 미리 지정하려면 Actions → Watchdog → Run workflow 의 `ids` 에 넣는다.
`dry_run=true`, `ignore_window=true` 로 수동 점검(판단만 출력)할 수 있다.
수집·분할 시계 판별은 라이브로 받은 줄 기준이며, 지난 문장을 한꺼번에 받은 경우에도 아젠더 첫 시각과 비교해 판별한다.

### 시각 기준 (중요)
- WIPO 가 주는 문장 시각(`end_time_iso`)과 마커 시각(`timestampCVC5`)은 **시간대 표시가 없는 UTC** 값이다 (개회 10:04 Geneva = 09:04 UTC 로 확인).
  `updates.jsonl`/`markers.jsonl`/`state.json` 에는 이 원본 값이 그대로 저장된다.
- 웹 화면(실시간 탭의 문장 시각, 마커 패널)과 `by_session/`, `by_speaker/` 의 시각은 **Geneva 현지 시간**으로 변환해서 보여준다.
  변환 기준은 분할 결과의 `clock` 값(`utc`/`geneva`/`offset:<초>`)이며 수신 시각과 비교해 자동 판별한다.

### 스트림 멈춤 감지·자동 전환 (웹)
- 수집기는 `state.json` 에 `updated`(수집기 생존 신호)와 `last_data`(WIPO 에서 마지막으로 새 문장이 들어온 시각)를 따로 기록한다. 스트림이 멈추면 `updated` 는 계속 갱신되지만 `last_data` 는 멈춘다.
- 웹은 기본으로 일반 스트림(`<해시>`)을 보고, 선택한 스트림이 4분 넘게 새 문장이 없는데 다른 스트림은 진행 중이면 **자동으로 전환**한다(`?id=` 로 직접 지정하거나 메뉴로 고른 경우는 전환하지 않고 "저장 상태" 탭에 경고). 쉬는 시간에는 전환/경고하지 않는다.
- "저장 상태" 탭의 `두 스트림 비교` 카드에서 각 스트림이 진행 중인지 멈췄는지 볼 수 있다.

## 발표별 분할 (아젠더 기준) · 3일 일정

`docs/agenda.json` 에 3일 일정을 넣어 두었다 (Geneva 시간). 출처는 공식 Provisional Program 두 개이며 TBC/시간은 바뀔 수 있으니 이 파일을 고치면 된다.

| 날짜 | 행사 | 비고 |
|---|---|---|
| 11/9, 11/10 | First Global Forum on IP and AI (`WIPO/IP-AI/GEN/26`) | 10:00–13:00 / 15:00–18:00 |
| 11/11 | AI Infrastructure Interchange (AIII) Annual Meeting (`WIPO/AIII/GE/26`) | **다른 행사라 meeting_id 가 다를 수 있음** |

`split_by_agenda.py` 는 수집된 `updates.jsonl` 을 날짜별 아젠더로 나눠 `transcripts/<meeting_id>/by_session/` 에 저장한다
(워크플로가 2분마다 자동 실행). 하루 단위로 독립 처리하므로 meeting_id 가 날마다 달라도, 같은 날 여러 번 나눠 실행해도 된다.

- `<id>_<제목>.txt` : 발표별 스크립트 (맨 위 `# ` 줄은 머리말: 시간, 발표자, 실제 구간, 경계 방식)
- `index.json` : 세션 목록과 실제 구간/문장 수 (웹 프로그램 탭이 사용)
- `all_by_session.md` : 전체를 발표 제목으로 구분한 한 파일

경계는 (1) 예정 시각 + 앞 세션까지의 누적 지연으로 예상 시작 시각을 잡고 (2) 그 ±10분 안에서 발표자 이름/제목 키워드가
처음 언급되는 문장을 시작점으로(ASR 오인식 대비 유사도 매칭), (3) 단서가 없으면 예정 시각 이후 첫 문장으로 정한다.
방식은 `index.json` 의 `method`(`cue`/`schedule`/`manual`/`first-line`)에 기록된다. 틀린 경계는 직접 지정해서 다시 나눌 수 있다:

```bash
python3 split_by_agenda.py --in wipo_out --out wipo_out/by_session --override d1-04=10:46:30
```

문장 시각의 시계(UTC/Geneva)는 수신 시각과 비교해 자동 판별하며 `--clock utc|geneva|receipt` 로 강제할 수 있다.
빈 휴식 세션은 파일이 생기지 않는다. 합성 데이터(드리프트·이름 오인식·시각 누락 포함)로 검증했고 실제 회의 ASR 로는 미검증이다.

## 발표자별 저장 (AI 요약 입력용)

세션별 저장(`by_session/`)과 **별도로**, 각 발표자의 발언만 모아 `transcripts/<meeting_id>/by_speaker/` 에 저장한다.
진행자/다른 패널의 발언이 섞이지 않으므로 **발표자별 AI 요약의 입력**으로 바로 쓸 수 있다.

| 파일 | 내용 |
|---|---|
| `<세션id>__<발표자>.txt` | 그 발표자의 발언만 (여러 번 발언하면 빈 줄로 구분). 머리말: 세션 제목, 발표자·소속·역할, 발언 횟수, 문장/단어 수, 구분 방식 |
| `speakers.jsonl` | 발표자(세션) 한 건당 한 줄: `session_id/title/type`, `day/date/scheduled`, `speaker/affiliation/role`, `method`, `n_turns/n_lines/n_words`, `turns`(시각·줄 범위), `text`(본문) |

- 발언자 교대는 (1) 웹캐스트 **발언자 마커**(이름/국가 + 시각, `markers.jsonl`)가 있으면 그 시각으로 나눈다(`method: marker`).
  플로어 발언은 마커의 국가/기관명(`GERMANY (GROUP B COORDINATOR)` 등)으로 구분된다. (2) 마커가 없으면 발표자가 1명인 세션은 아젠더 기준(`agenda`),
  패널은 사회자의 호명 문장("…님 모시겠습니다")에서 이름을 잡아 나눈다(`cue`). (3) 그래도 알 수 없으면 `(발표자 구분 불가)` 로 묶는다(`none`).
- 전사 자체에는 화자 구분이 없으므로 경계는 근사치다. 합성 패널 데이터(사회자+패널 4명)에서 마커/호명 방식 모두 줄 단위 100% 일치,
  마커 없는 플로어 발언은 구분 불가로 표시됨. 실제 회의 ASR 로는 미검증이다. 마커가 라이브 중 비어 있으면 회의 후 마커를 받아 다시 나눌 수 있다.
- 웹 프로그램 탭에서 패널/플로어 세션을 열면 `전체 세션 | 발표자별` 선택 버튼이 나타난다.
- AI 요약 단계(미구현): `speakers.jsonl` 을 읽어 발표자별로 요약을 생성하면 된다. 각 레코드에 세션 제목·발표자 소속/역할이 같이 있어 프롬프트에 그대로 넣을 수 있고,
  `n_words` 로 긴 발언(청크 분할)을 판단할 수 있다. `role` 이 `moderator`/`chair` 인 레코드는 요약 대상에서 빼는 것을 권한다.

## WIPO 공식 스크립트 가져오기 (행사 후 배포본)

WIPO 가 행사 후 배포하는 `…verbatim.docx` 는 안건(`<< 제목 >>`)과 발언자(`이름 [hh:mm:ss]`) 표지가 들어 있어 실시간 수집분(STT)보다 경계가 정확합니다. 공식본을 가져오면 프로그램 탭이 해당 안건을 공식본으로 보여줍니다.

```bash
python3 official_import.py 문서.docx --name day1-am --label "Day 1 오전 (11월 9일)" --out docs/official \
  --markers <data 브랜치>/transcripts/<meeting_id>/markers.jsonl --transcript <같은 폴더>/transcript.txt
```

- 결과는 `docs/official/<이름>/` (전체 `transcript.txt`, `by_session/`, `by_speaker/`, `info.json`, 원본 docx) 와 목록 `docs/official/index.json` 입니다. main 에 머지하면 Pages 에 반영됩니다. 실시간 수집분(data 브랜치)은 건드리지 않고 그대로 보관합니다.
- 프로그램 탭: 공식본이 있는 안건은 "공식 스크립트 N단락" 으로 표시되고 발표자별 보기도 공식본 기준입니다. 공식본에 없는 안건(예: 아직 배포되지 않은 오후 세션)은 실시간 수집분을 그대로 씁니다.
- 전체 저장본 탭: 상단 선택으로 실시간 수집분과 공식본을 오갑니다 (공식본은 ■ 안건 / ▌ 발언자 표지 줄 포함).
- 문서의 시각은 녹화 시작 기준 오프셋이라, 웹캐스트 발언자 마커와 이름으로 맞춰 절대 시각(UTC)을 구합니다 (`info.json` 의 `base_utc`, `clock_note`). `info.json` 의 `compare` 는 실시간 수집분과 4단어 연속 구간 일치율입니다.
- 표지 없는 안건(예: Chair Remarks)은 그 앞 안건 구간 안에서 아젠더 발표자 이름과 같은 발언을 그 안건으로 가져옵니다. 문서에 없는 안건은 비어 있습니다.

## 웹에서 보기 (GitHub Pages)

`docs/index.html` 은 탭 세 개로 구성된 정적 뷰어다.

| 탭 | 데이터 출처 | 내용 |
|---|---|---|
| **실시간** | 브라우저가 WIPO `getLiveTranscript` 를 **직접** 3초마다 호출 | 확정 문장(시각 포함) + **지금 말하는 중인 문장**(커서 표시), 글자 크기 조절, 자동 스크롤/`↓ 최신으로`. 저장·커밋 지연 없음. WIPO 연결이 안 되면(회의 시작 전 등) 저장본 마지막 30문장으로 대체 표시하고 계속 재시도 |
| **프로그램** | `docs/agenda.json` + `data` 브랜치의 `by_session/index.json` (60초마다) | 3일 일정표. 발표 제목을 누르면 그 발표의 저장된 스크립트가 펼쳐지고(`.txt` 저장/복사), 스크립트가 있는 세션에는 문장 수 표시. `#program:d1-04` 처럼 링크로 특정 발표를 바로 열 수 있음 |
| **전체 저장본** | `data` 브랜치의 `transcript.txt` / `markers.jsonl` (30초마다 확인) | 처음부터 전체 문장(번호), 검색, `.txt` 저장/복사, 발언자 마커 패널. 저장은 약 2분 간격이라 최신 몇 분은 실시간 탭에서 확인 |

※ 실시간 탭은 보는 사람의 브라우저가 WIPO 서버를 직접 호출한다(webcast 페이지 자신이 하는 것과 같은 요청).
실시간 탭의 시작 위치는 저장본 문장 수 근처로 잡아 응답을 작게 유지하고, 범위를 벗어나면 1번부터 다시 받는다.

켜는 방법 (저장소 설정, 1회):
1. 저장소를 **public** 으로 (무료 플랜의 Pages 조건)
2. 이 PR 을 `main` 에 머지
3. Settings → Pages → *Build and deployment* → Source: **Deploy from a branch** → Branch: `main`, Folder: `/docs` → Save
4. 잠시 후 `https://kklique.github.io/wipo-live/` 에서 열린다.

URL 파라미터: `?id=<meeting_id>` (회의 선택), `?repo=owner/name`, `?branch=data`. `#full` 을 붙이면 전체 저장본 탭으로 열린다.
데이터는 GitHub API(`contents`, ETag 재검증)로 읽고, 한도 초과 등으로 실패하면 `raw.githubusercontent.com` 으로 폴백한다.

## 로컬 실행

```bash
python3 collect.py --out wipo_out
```
