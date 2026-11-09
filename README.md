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

## 로컬 실행

```bash
python3 collect.py --out wipo_out
```
