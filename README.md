# party-news-weekly

한국 정당 브리핑·보도자료를 자동 수집해 노션 DB에 저장하는 뉴스 마이너

## 주요 기능

- 목록 페이지에서 글 제목, URL, 날짜 자동 수집
- 상세 페이지 본문을 Notion 페이지에 문단 블록으로 저장
- 조국혁신당: API에서 직접 본문 추출 (Selenium 불필요)
- 진보당: Selenium으로 JavaScript 렌더링 처리
- URL 정규화를 통한 중복 체크 (http/https, www, trailing slash 등 통일)

## 지원 정당

| 정당 | 카테고리 | 크롤링 방식 |
|------|---------|------------|
| 기본소득당 | 브리핑, 언론보도 | HTML 파싱 |
| 사회민주당 | 브리핑 | HTML 파싱 |
| 조국혁신당 | 기자회견, 논평브리핑, 보도자료 | **API 직접 호출** |
| 진보당 | 모두발언, 논평, 정책논평, 보도자료 | Selenium (로컬 전용) |
| 노동당 | 브리핑, 논평 | HTML 파싱 |
| 녹색당 | 보도자료, 활동보고, 논평, 발언 | HTML 파싱 |
| 정의당 | 브리핑룸 | HTML 파싱 |

> **참고**: 진보당은 GitHub Actions에서 IP 차단되어 로컬에서만 크롤링 가능합니다.

## 설치

### 1. Python 패키지 설치

```bash
pip install -r requirements.txt
```

### 2. Chrome & ChromeDriver 설치

Selenium을 사용하는 사이트(조국혁신당, 진보당)의 본문을 추출하려면 Chrome과 ChromeDriver가 필요합니다.

#### Windows

1. Chrome 브라우저 설치: [https://www.google.com/chrome/](https://www.google.com/chrome/)
2. ChromeDriver 다운로드:
   - [ChromeDriver 다운로드 페이지](https://googlechromelabs.github.io/chrome-for-testing/)
   - Chrome 버전과 일치하는 ChromeDriver 다운로드
   - `chromedriver.exe`를 PATH에 있는 디렉토리에 복사 (예: `C:\Windows\System32\`)

#### macOS

```bash
brew install --cask google-chrome
brew install chromedriver
```

#### Linux

```bash
# Chrome 설치
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install ./google-chrome-stable_current_amd64.deb

# ChromeDriver 설치 (Chrome 버전 확인 후 일치하는 버전 다운로드)
google-chrome --version
```

### 3. 환경 변수 설정

Notion 업로드를 사용하려면 환경 변수를 설정해야 합니다:

```bash
export NOTION_TOKEN="your-notion-integration-token"
export NOTION_DATABASE_ID="your-database-id"
```

## 사용법

### 기본 실행 (전체 사이트)

```bash
python src/main.py --sample 20
```

### 특정 사이트만 실행

```bash
# 조국혁신당만
python src/main.py --only rebuildingkoreaparty --sample 10

# 진보당만
python src/main.py --only jinboparty --sample 10
```

### 특정 카테고리만 실행

```bash
python src/main.py --only-category 논평 --sample 10
```

### Notion 업로드

```bash
python src/main.py --sample 5 --notion
```

### 자동 스케줄링 (하루 3회 실행)

#### 옵션 1: GitHub Actions (권장 - 무료, 24시간 자동 실행)

GitHub Actions를 사용하면 컴퓨터를 켜두지 않아도 자동으로 크롤링됩니다.

**설정 방법:**

1. GitHub 리포지토리의 **Settings** → **Secrets and variables** → **Actions** 이동

2. **New repository secret** 클릭하여 2개 추가:
   - Name: `NOTION_TOKEN`
     Value: (Notion Integration Token)
   - Name: `NOTION_DATABASE_ID`
     Value: (Notion Database ID)

3. 완료! 매일 자동으로 실행됩니다:
   - 오전 10:30 (KST)
   - 오후 15:30 (KST)
   - 저녁 19:30 (KST)

4. 수동 실행: **Actions** 탭 → **Auto Crawl Party News** → **Run workflow**

**실행 로그 확인:**
- GitHub 리포지토리 **Actions** 탭에서 확인 가능

#### 옵션 2: 로컬 스케줄러 (컴퓨터 켜둬야 함)

로컬에서 스케줄러를 실행하려면:

```bash
python scheduler.py
```

스케줄러는 각 정당에서 최신 3개 게시물을 자동으로 수집하여 Notion에 업로드합니다. 프로그램을 종료하려면 `Ctrl+C`를 누르세요.

**백그라운드에서 계속 실행하려면:**

Windows (PowerShell):
```powershell
Start-Process python -ArgumentList "scheduler.py" -WindowStyle Hidden
```

Linux/macOS:
```bash
nohup python scheduler.py > scheduler.log 2>&1 &
```

## Notion 데이터베이스 설정

Notion 데이터베이스는 다음 속성을 가져야 합니다:

- **제목** (Title): 글 제목
- **정당** (Text): 정당 이름
- **카테고리** (Text): 카테고리 (브리핑, 논평, 보도자료 등)
- **날짜** (Date): 작성일
- **링크** (URL): 원문 링크

## 기술적 세부사항

### 조국혁신당 API

조국혁신당은 Next.js 기반이지만, 내부 API를 직접 호출하여 본문을 가져옵니다:

```
POST https://api.rebuildingkoreaparty.kr/api/board/list
{
  "page": 1,
  "categoryId": 7,  // 6=기자회견문, 7=논평브리핑, 9=보도자료
  "recordSize": 10,
  "order": "recent"
}
```

API 응답의 `descriptionText` 필드에서 본문을 추출하므로 Selenium이 필요 없습니다.

### 진보당 Selenium 처리

진보당은 JavaScript로 콘텐츠를 동적으로 렌더링하므로 Selenium이 필요합니다:

- Headless Chrome으로 실행
- `.content_box` 셀렉터가 로드될 때까지 대기
- GitHub Actions에서는 IP 차단으로 인해 로컬 전용 스크립트 `crawl_jinboparty.py` 사용

### URL 정규화

중복 체크 시 URL을 정규화하여 비교합니다:
- `http` → `https` 통일
- `www.` 제거
- trailing slash 제거
- 불필요한 쿼리 파라미터 제거 (page, utm_* 등)

### 사이트별 셀렉터

| 정당 | 본문 셀렉터 |
|------|-----------|
| 조국혁신당 | API `descriptionText` |
| 진보당 | `.content_box` |
| 사회민주당 | `.view_content` |
| 기본소득당 | `.entry-content` |
| 노동당 | `.kboard-document .kboard-content` |
| 녹색당 | `.fr-view` (Froala editor) |
| 정의당 | `div.content` |

## 문제 해결

### ChromeDriver 오류

```
selenium.common.exceptions.WebDriverException: Message: 'chromedriver' executable needs to be in PATH
```

→ ChromeDriver를 PATH에 추가하거나 `chromedriver.exe`를 Python 스크립트와 같은 디렉토리에 복사

### Selenium Alert 오류

진보당 사이트에서 비공개 게시물 접근 시 Alert 창이 표시될 수 있습니다. 이는 정상적인 동작이며, 해당 글은 건너뜁니다.

### 인코딩 오류 (Windows)

Windows 콘솔에서 한글이 깨지는 경우:
```bash
chcp 65001  # UTF-8로 변경
```

## 최근 변경사항

- **2026-01-27**: 날짜 필터링 및 크롤링 개선
  - 기본 날짜 필터 추가 (`--date-from` 기본값 2026-01-01)
  - 녹색당: 제목에서 `[M/D]` 형태 날짜 자동 추출
  - 기본소득당: 시간만 표시된 경우("18:34") 오늘 날짜로 처리
  - 기본소득당: 페이지네이션 지원 (3페이지, 최대 30개 수집)
  - 날짜 없는 항목은 필터링에서 제외 (오래된 기사 유입 방지)

- **2026-01-16**: 조국혁신당 크롤링 개선
  - API categoryId 매핑 수정 (기자회견=6, 논평브리핑=7, 보도자료=9)
  - API `descriptionText`에서 직접 본문 추출 (Selenium 불필요)
  - URL 정규화를 통한 중복 감지 개선
  - 기본소득당 제목에서 'New' 접두사 자동 제거
  - 녹색당 다중 HTML 구조 파싱 지원

- **2026-01-11**: Selenium 기반 JavaScript 렌더링 크롤러 추가
  - 조국혁신당, 진보당 본문 추출 성공
  - 사이트별 셀렉터 개선

자세한 내용은 [HANDOFF.md](HANDOFF.md)를 참고하세요.
