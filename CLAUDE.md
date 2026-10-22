# corporate_analysis_for_jay

메자닌(CB·BW·EB) 투자 전 기업분석 대시보드.

- 대시보드: `mezzanine-dashboard/index.html` → claude.ai Artifact https://claude.ai/artifact/Xg657YT7XEYf2j71uCrH6C 로 게시
  (같은 URL에 다시 게시할 때는 Artifact `url`을 넘기고, capabilities는 `sample`, `db`(root read view / write admin, `requests`·`qual`은 write interact)를 유지)
- 평가 기준: `mezzanine-dashboard/CRITERIA.md`
- DART 수집: `scripts/dart_fetch.py` (환경 변수 `DART_API_KEY` 필요)

## "대기 중인 DART 요청 처리해"

대시보드의 "DART 수집 요청" 버튼은 대시보드 저장소의 `requests/<id>`에 `{query, at}`만 기록한다. 예약 작업이 매시 정각(KST 07~23시)에 새 세션을 띄워 이 절차를 자동으로 실행하고, 사람이 같은 말을 해도 같은 절차를 실행한다. 이 요청을 받으면 아래만 실행한다. 데이터 내용은 읽지 말고(토큰 절약) 스크립트 출력 한 줄 요약만 확인한다.

1. ArtifactData `list`로 `requests`와 `results` 컬렉션을 `out_dir=.cache/queue`에 저장한다 (url은 위 대시보드). `requests`에 있는 id가 모두 `results`에도 있으면 "대기 요청 없음"으로 끝낸다.
2. `python3 scripts/dart_fetch.py --queue .cache/queue` (포그라운드로 실행하고 timeout은 10분. 백그라운드로 돌린 뒤 `pgrep -f dart_fetch.py`로 기다리면 자기 자신을 잡아 끝나지 않는다)
   - results가 없는 요청만 최대 10건 처리하고 `.cache/upload/writes.json`을 만든다.
3. `.cache/upload/writes.json`의 배열을 ArtifactData `batch`의 `writes`로 그대로 보낸다.
   - 이미 있는 문서(같은 기업을 다시 받은 경우)라서 거부되면 그 문서를 `get`해 `version`을 `if_version`으로 붙여 다시 보낸다.
4. 저장소를 못 쓰는 화면(claude.ai 밖, 다른 조직 계정)에서도 보이도록 같은 데이터를 대시보드 파일로도 올린다.
   - `python3 scripts/static_files.py .cache/upload/writes.json` → `.cache/static/files.json`(게시 경로 → 로컬 파일 매핑)이 만들어진다.
   - Artifact `read`(url은 위 대시보드)로 현재 페이지를 받는다. 결과에 적힌 저장 파일이 현재 페이지 전체다.
   - Artifact publish: `url`=대시보드, `file_path`=read가 저장한 페이지 파일, `files`=`files.json`의 매핑 그대로. 페이지는 고치지 않고, 넘기지 않은 기존 파일은 그대로 남는다. `capabilities`는 넘기지 않는다(기존 유지).
5. 사용자에게 처리한 기업과 실패한 요청을 한 줄씩 보고한다.

저장소 구조(대시보드 파일 `data/c/<corp_code>.json`·`data/l/<검색키>.json`에도 같은 내용): `companies/<corp_code>`(기업 데이터), `qual/<corp_code>`(정성평가 결과), `lookup/n_<정규화한 이름의 UTF-8 hex>`·`lookup/t_<종목코드>`(검색어 → corp_code), `results/<요청 id>`(처리 결과), `requests/<요청 id>`(대기열).
