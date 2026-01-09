# party-news-weekly handoff

## 목적

- 여러 정당 사이트의 목록을 자동 수집해 Notion DB에 적재
- Notion DB 형태: `정당 / 카테고리 / 제목 / 날짜 / 링크`
- 상세 페이지 본문을 Notion 페이지에 문단 블록으로 저장
- 실행 주기: 매일 10시 / 18시 / 02시 (3회)

## 현재 상태 요약

- 크롤러 엔트리: `src/main.py`
- 타깃 목록: `config/sources.json`
- 실행 예:
  - 전체: `python3 src/main.py --sample 20`
  - Notion 업로드: `python3 src/main.py --sample 5 --notion`
  - 특정 사이트: `python3 src/main.py --only jinboparty --sample 10`
  - 특정 카테고리: `python3 src/main.py --only-category 논평 --sample 10`
  - 특정 id: `python3 src/main.py --only-id jinbo_category_286 --sample 10`

## 구현된 사이트별 파서

- 기본소득당: `/news/briefing`, `/news/press`(언론보도 외부 링크)
- 사회민주당: `/news/briefing` (카드형/onclick 대응)
- 조국혁신당: JS 렌더링 → API(`api.rebuildingkoreaparty.kr/api/board/list`)로 수집
  - 현재 categoryId=7 고정 + URL slug 필터링
- 진보당: 목록 텍스트 깨짐 → 상세 페이지에서 제목/날짜 재수집
  - `js_board_view('id')` → read URL 생성
  - `img_list_item` 카드형(보도자료) 대응
- 노동당: KBoard 목록 파싱(브리핑/논평 page_id 별도)
- 녹색당: press/event/statement/statement2/address
  - statement/statement2 모두 category=논평
- 정의당: board_view 링크 수집 (bbs_code=JS21, 브리핑룸)

## Notion 업로드

- 환경변수 필요:
  - `NOTION_TOKEN`
  - `NOTION_DATABASE_ID` (예: `2e1c9c3e833b803b8accf5fb620224bb`)
- DB 속성명(정확히):
  - 제목(Title), 정당(Text), 카테고리(Text), 날짜(Date), 링크(URL)
- 업로드 동작:
  - `--notion` 옵션 사용 시 업로드
  - 링크 기준 중복 체크 후 생성
  - 상세 페이지 본문을 문단 블록으로 추가
  - 날짜가 없으면 상세 페이지에서 보정

## 남은 이슈/할 일

- Notion 업로드 시 사이트별 상세 본문/날짜 누락 발생 가능
  - JS 렌더링/HTML 구조 다른 사이트는 추가 파서 필요
- 현재 본문 추출 셀렉터는 공통 후보만 사용
  - 필요 시 사이트별 `fetch_detail_for_notion()` 개선
- 스케줄러(자동 실행)는 아직 미설정
  - macOS `launchd`로 10/18/02시 실행 예정

## 환경 변수 설정 예

```bash
export NOTION_TOKEN="새로 발급한 시크릿"
export NOTION_DATABASE_ID="2e1c9c3e833b803b8accf5fb620224bb"
```
