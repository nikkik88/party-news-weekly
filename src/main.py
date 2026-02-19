import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Tuple, Optional
import os
from urllib.parse import parse_qs, urljoin, urlparse, urlencode

import requests
from bs4 import BeautifulSoup

# Selenium imports (optional, only used for JS-rendered sites)
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# ----------------------------
# Core models
# ----------------------------

@dataclass(frozen=True)
class Target:
    id: str
    party: str
    site: str
    category: str
    list_url: str


@dataclass(frozen=True)
class ListItem:
    party: str
    category: str
    title: str
    url: str
    date: Optional[str] = None
    content: Optional[str] = None  # 조국혁신당 등 API에서 본문을 가져오는 경우


# ----------------------------
# URL normalization (중복 방지용)
# ----------------------------

# 중복 체크 시 무시할 쿼리 파라미터 (페이지네이션, 세션 등)
IGNORABLE_QUERY_PARAMS = {
    "pageid", "page", "nPage", "nPageSize", "pageSize",
    "q", "keyword", "search", "keyword_type",
    "t", "ref", "utm_source", "utm_medium", "utm_campaign",
}


def normalize_url(url: str) -> str:
    """URL을 정규화하여 중복 비교에 사용.

    - scheme을 https로 통일
    - www 제거
    - trailing slash 제거
    - 쿼리 파라미터 정렬
    - 불필요한 파라미터 제거
    - fragment 제거
    """
    if not url:
        return ""

    parsed = urlparse(url)

    # scheme 통일 (http -> https)
    scheme = "https"

    # www 제거
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    # path 정규화 (trailing slash 제거, 단 root는 유지)
    path = parsed.path.rstrip("/") or "/"

    # 쿼리 파라미터 정규화
    query_params = parse_qs(parsed.query, keep_blank_values=False)
    # 불필요한 파라미터 제거 및 정렬
    filtered_params = {
        k: v for k, v in query_params.items()
        if k.lower() not in IGNORABLE_QUERY_PARAMS
    }
    # 정렬된 쿼리 문자열 생성
    sorted_query = urlencode(
        {k: v[0] if len(v) == 1 else v for k, v in sorted(filtered_params.items())},
        doseq=True
    )

    # fragment 제거하고 재조합
    normalized = f"{scheme}://{netloc}{path}"
    if sorted_query:
        normalized += f"?{sorted_query}"

    return normalized


# ----------------------------
# HTTP helper (polite + stable)
# ----------------------------

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

DEBUG_SITES: set[str] = set()


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def fetch_html(
    session: requests.Session,
    url: str,
    timeout: int = 30,
    headers: Optional[dict[str, str]] = None,
    encoding: Optional[str] = None,
    retries: int = 3,
) -> str:
    merged_headers = dict(session.headers)
    if headers:
        merged_headers.update(headers)

    last_error = None
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout, headers=merged_headers)
            r.raise_for_status()
            if encoding == "auto":
                return decode_bytes(r.content)
            if encoding:
                r.encoding = encoding
            return r.text
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))  # 2초, 4초, 6초 대기
                continue
            raise
    raise last_error


def fetch_json(
    session: requests.Session,
    url: str,
    payload: dict,
    timeout: int = 20,
    headers: Optional[dict[str, str]] = None,
) -> dict:
    merged_headers = dict(session.headers)
    if headers:
        merged_headers.update(headers)
    r = session.post(url, json=payload, timeout=timeout, headers=merged_headers)
    r.raise_for_status()
    return r.json()


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def notion_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def notion_find_by_url(token: str, database_id: str, url: str) -> bool:
    payload = {
        "filter": {"property": "링크", "url": {"equals": url}},
        "page_size": 1,
    }
    r = requests.post(
        f"{NOTION_API_BASE}/databases/{database_id}/query",
        headers=notion_headers(token),
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return bool(data.get("results"))


def notion_create_page(
    token: str,
    database_id: str,
    item: ListItem,
    title_prop: str,
    db_props: dict,
) -> str:
    """
    Create a Notion page with properties matching the database schema.
    Automatically detects property types (rich_text, select, etc.)
    """
    properties = {
        title_prop: {"title": [{"text": {"content": item.title}}]},
        "링크": {"url": item.url},
    }

    # Handle "정당" property - can be rich_text or select
    if "정당" in db_props:
        prop_type = db_props["정당"].get("type")
        if prop_type == "select":
            properties["정당"] = {"select": {"name": item.party}}
        elif prop_type == "rich_text":
            properties["정당"] = {"rich_text": [{"text": {"content": item.party}}]}
        # Fall back to rich_text if type is unexpected
        else:
            properties["정당"] = {"rich_text": [{"text": {"content": item.party}}]}

    # Handle "카테고리" property - can be rich_text or select
    if "카테고리" in db_props:
        prop_type = db_props["카테고리"].get("type")
        if prop_type == "select":
            properties["카테고리"] = {"select": {"name": item.category}}
        elif prop_type == "rich_text":
            properties["카테고리"] = {"rich_text": [{"text": {"content": item.category}}]}
        else:
            properties["카테고리"] = {"rich_text": [{"text": {"content": item.category}}]}

    # Handle "날짜" property
    if item.date:
        properties["날짜"] = {"date": {"start": item.date}}

    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }
    r = requests.post(
        f"{NOTION_API_BASE}/pages",
        headers=notion_headers(token),
        json=payload,
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Notion create failed: {r.status_code} {r.text}")
    return r.json().get("id", "")


def notion_append_children(token: str, page_id: str, blocks: List[dict]) -> None:
    if not page_id or not blocks:
        return
    chunk_size = 100
    for i in range(0, len(blocks), chunk_size):
        payload = {"children": blocks[i : i + chunk_size]}
        r = requests.patch(
            f"{NOTION_API_BASE}/blocks/{page_id}/children",
            headers=notion_headers(token),
            json=payload,
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"Notion append failed: {r.status_code} {r.text}")
        time.sleep(0.2)


def notion_get_db_props(token: str, database_id: str) -> dict:
    r = requests.get(
        f"{NOTION_API_BASE}/databases/{database_id}",
        headers=notion_headers(token),
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("properties", {})


DETAIL_DOMAIN_ALLOWLIST = {
    "www.basicincomeparty.kr",
    "basicincomeparty.kr",
    "www.samindang.kr",
    "samindang.kr",
    "blog.naver.com",  # 사회민주당 블로그
    "rebuildingkoreaparty.kr",
    "www.rebuildingkoreaparty.kr",
    "jinboparty.com",
    "www.jinboparty.com",
    "www.laborparty.kr",
    "laborparty.kr",
    "www.kgreens.org",
    "kgreens.org",
    "www.justice21.org",
    "justice21.org",
}

# 크롤링에서 제외할 URL 목록 (이용약관, 개인정보처리방침 등)
EXCLUDED_URLS = {
    "https://www.justice21.org/newhome/board/board_view.html?num=109587",  # 정의당 이용약관
}


def build_paragraph_blocks(paragraphs: List[str]) -> List[dict]:
    """
    Build Notion paragraph blocks from text paragraphs.
    Notion has a 2000 character limit per text block, so we split long paragraphs.
    """
    blocks: List[dict] = []
    MAX_LENGTH = 2000

    for p in paragraphs:
        text = p.strip()
        if not text:
            continue

        # Split long paragraphs into chunks of max 2000 characters
        if len(text) <= MAX_LENGTH:
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": text}}]}})
        else:
            # Split at sentence boundaries if possible
            chunks = []
            current_chunk = ""

            # Try to split at sentences (. ! ?)
            sentences = re.split(r'([.!?]\s+)', text)

            for i in range(0, len(sentences), 2):
                sentence = sentences[i]
                separator = sentences[i + 1] if i + 1 < len(sentences) else ""
                full_sentence = sentence + separator

                if len(current_chunk) + len(full_sentence) <= MAX_LENGTH:
                    current_chunk += full_sentence
                else:
                    if current_chunk:
                        chunks.append(current_chunk.strip())

                    # If single sentence is too long, force split
                    if len(full_sentence) > MAX_LENGTH:
                        for j in range(0, len(full_sentence), MAX_LENGTH):
                            chunks.append(full_sentence[j:j + MAX_LENGTH].strip())
                        current_chunk = ""
                    else:
                        current_chunk = full_sentence

            if current_chunk:
                chunks.append(current_chunk.strip())

            # Add all chunks as separate blocks
            for chunk in chunks:
                if chunk:
                    blocks.append({"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": chunk}}]}})

    return blocks


def extract_paragraphs_from_element(el: Optional[BeautifulSoup]) -> List[str]:
    if not el:
        return []

    # Make a copy to avoid modifying the original
    el_copy = BeautifulSoup(str(el), 'html.parser')

    # Remove KBoard meta elements (for 노동당)
    for unwanted in el_copy.select('.kboard-title, .kboard-detail, .kboard-document-action, .kboard-document-navi, .kboard-control, .kboard-document-info, .kboard-attr'):
        unwanted.decompose()

    paras = []
    for p in el_copy.select("p"):
        txt = p.get_text(" ", strip=True)
        if txt:
            paras.append(txt)
    if paras:
        return paras
    text = el_copy.get_text("\n", strip=True)
    return [t.strip() for t in text.splitlines() if t.strip()]


def extract_date_from_soup(soup: BeautifulSoup) -> Optional[str]:
    for sel in [".date", ".view_date", ".write_date", ".info_date", ".kboard-list-date", ".kboard-date"]:
        el = soup.select_one(sel)
        if el:
            d = extract_date_from_text(el.get_text(" ", strip=True))
            if d:
                return d
    return extract_date_from_text(soup.get_text(" ", strip=True))


def fetch_with_selenium(url: str, wait_selector: Optional[str] = None, wait_timeout: int = 10) -> str:
    """
    Fetch a page using Selenium (for JS-rendered sites).

    Args:
        url: The URL to fetch
        wait_selector: CSS selector to wait for before extracting HTML (optional)
        wait_timeout: Maximum time to wait for the selector (seconds)

    Returns:
        The fully-rendered HTML as a string
    """
    if not SELENIUM_AVAILABLE:
        raise RuntimeError("Selenium is not installed. Install it with: pip install selenium")

    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(f"user-agent={UA}")

    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        driver.get(url)

        # Wait for specific selector if provided
        if wait_selector:
            try:
                WebDriverWait(driver, wait_timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector))
                )
            except TimeoutException:
                # Continue anyway, maybe the content loaded differently
                pass
        else:
            # Generic wait for page load
            time.sleep(3)

        html = driver.page_source
        return html
    finally:
        if driver:
            driver.quit()


def fetch_detail_for_notion(session: requests.Session, url: str) -> Tuple[Optional[str], List[str]]:
    parsed = urlparse(url)
    if parsed.netloc not in DETAIL_DOMAIN_ALLOWLIST:
        return None, []

    # Determine if we need Selenium for this site
    use_selenium = False
    wait_selector = None

    if "rebuildingkoreaparty.kr" in parsed.netloc:
        # 조국혁신당: JavaScript-rendered (Next.js)
        use_selenium = True
        wait_selector = None  # Use generic 3-second wait
    elif "jinboparty.com" in parsed.netloc:
        # 진보당: JS-rendered content
        use_selenium = True
        wait_selector = ".content_box"

    if use_selenium and SELENIUM_AVAILABLE:
        try:
            html = fetch_with_selenium(url, wait_selector=wait_selector, wait_timeout=10)
        except Exception as e:
            print(f"[WARN] Selenium failed for {url}, falling back to requests: {e}")
            html = fetch_html(session, url, headers={"Referer": url}, encoding="auto")
    else:
        html = fetch_html(session, url, headers={"Referer": url}, encoding="auto")

    soup = BeautifulSoup(html, "html.parser")

    date = extract_date_from_soup(soup)
    content_el = None

    # Site-specific selectors (in priority order)
    selectors = [
        ".ck-content",  # 조국혁신당 (CKEditor)
        "article.newsArticle",  # 조국혁신당 (old structure)
        ".fr-view",  # 녹색당 (Froala editor)
        "div.content",  # 정의당 (board view content)
        ".content_box",  # 진보당
        ".view_content",  # 사회민주당
        ".kboard-document .kboard-content",  # 노동당 (KBoard)
        ".kboard-document-content",
        ".entry-content",  # 기본소득당
        ".view-content",
        ".board_view .content",
        ".board_view_content",
        ".article_content",
        "#contents",
        ".contents",
        "article",
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el and el.get_text(" ", strip=True):
            content_el = el
            break

    paragraphs = extract_paragraphs_from_element(content_el)
    return date, paragraphs


# 카테고리 이름 통일 매핑: 원본 카테고리 → 노션 카테고리
CATEGORY_MAP = {
    "브리핑": "논평",
    "논평브리핑": "논평",
    "기자회견": "논평",
    "모두발언": "논평",
    "정책논평": "논평",
    "활동보고": "논평",
    "발언": "논평",
    "브리핑룸": "논평",
    # "논평"은 이미 "논평"이므로 별도 매핑 불필요
    # "보도자료", "언론보도"는 그대로 유지
}


def upload_to_notion(items: List[ListItem]) -> None:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    database_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not token or not database_id:
        print("[ERR] NOTION_TOKEN or NOTION_DATABASE_ID is not set")
        return

    try:
        props = notion_get_db_props(token, database_id)
    except Exception as e:
        print(f"[ERR] Notion DB fetch failed: {e}")
        return

    title_prop = None
    for name, meta in props.items():
        if meta.get("type") == "title":
            title_prop = name
            break
    required = ["정당", "카테고리", "날짜", "링크"]
    missing = [name for name in required if name not in props]
    if not title_prop:
        print("[ERR] Notion DB has no title property")
        return
    if missing:
        print(f"[ERR] Notion DB missing properties: {missing}")
        return

    session = get_session()
    seen_normalized = set()  # 정규화된 URL로 중복 체크
    created = 0
    skipped = 0

    for it in items:
        if not it.title or not it.url:
            continue

        # 카테고리 이름 통일
        mapped_cat = CATEGORY_MAP.get(it.category, it.category)
        if mapped_cat != it.category:
            it = ListItem(
                party=it.party,
                category=mapped_cat,
                title=it.title,
                url=it.url,
                date=it.date,
                content=it.content,
            )

        # URL 정규화하여 중복 체크
        normalized = normalize_url(it.url)
        if normalized in seen_normalized:
            continue
        if it.url in EXCLUDED_URLS or normalized in EXCLUDED_URLS:
            continue
        seen_normalized.add(normalized)

        try:
            # Notion에서도 원본 URL과 정규화된 URL 모두 체크
            if notion_find_by_url(token, database_id, it.url):
                skipped += 1
                continue

            # ListItem에 content가 이미 있으면 (조국혁신당 API 등) 사용, 없으면 fetch
            if it.content:
                # API에서 가져온 본문이 있는 경우
                paragraphs = [p.strip() for p in it.content.split('\n') if p.strip()]
                detail_date = it.date
            else:
                detail_date, paragraphs = fetch_detail_for_notion(session, it.url)

            if detail_date and not it.date:
                it = ListItem(
                    party=it.party,
                    category=it.category,
                    title=it.title,
                    url=it.url,
                    date=detail_date,
                    content=it.content,
                )
            page_id = notion_create_page(token, database_id, it, title_prop, props)
            blocks = build_paragraph_blocks(paragraphs)
            notion_append_children(token, page_id, blocks)
            created += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"[ERR] Notion upload failed for {it.url}: {e}")

    print(f"[OK] Notion upload: created={created}, skipped={skipped}")


HANGUL_RE = re.compile(r"[\uAC00-\uD7A3]")


def decode_bytes(data: bytes) -> str:
    candidates = []
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            text = data.decode(enc)
            replacements = 0
        except UnicodeDecodeError:
            text = data.decode(enc, errors="replace")
            replacements = text.count("\ufffd")
        hangul = len(HANGUL_RE.findall(text))
        score = (hangul * 10) - (replacements * 20)
        candidates.append((score, replacements, enc, text))
    candidates.sort(reverse=True)
    return candidates[0][3]


def fix_mojibake(text: str) -> str:
    if not text:
        return text
    if HANGUL_RE.search(text):
        return text
    if not any(ch in text for ch in ("ì", "ë", "í", "ï", "â", "ã", "à")):
        return text
    try:
        recovered = text.encode("latin1").decode("utf-8")
    except UnicodeError:
        return text
    if len(HANGUL_RE.findall(recovered)) > len(HANGUL_RE.findall(text)):
        return recovered
    return text


def recover_text(text: str) -> str:
    if not text:
        return text
    candidates = [text]
    for src in ("latin1", "cp1252"):
        for dst in ("utf-8", "cp949", "euc-kr"):
            try:
                cand = text.encode(src).decode(dst)
            except UnicodeError:
                cand = text.encode(src, errors="ignore").decode(dst, errors="ignore")
            candidates.append(cand)

    def score(s: str) -> Tuple[int, int, int]:
        hangul = len(HANGUL_RE.findall(s))
        non_ascii = sum(1 for ch in s if ord(ch) > 127)
        return (hangul, non_ascii, -len(s))

    candidates.sort(key=score, reverse=True)
    return candidates[0]


DATE_RE = re.compile(r"(?:등록일\s*)?(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})")
TIME_ONLY_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")  # "18:34" 같은 시간만 있는 경우


def extract_date_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = DATE_RE.search(text)
    if m:
        y, mth, d = m.groups()
        return f"{y}-{int(mth):02d}-{int(d):02d}"

    # 시간만 있는 경우 (예: "18:34") 오늘 날짜로 처리
    if TIME_ONLY_RE.match(text):
        from datetime import date
        today = date.today()
        return today.strftime("%Y-%m-%d")

    return None


# [M/D] 또는 [MM/DD] 형태의 날짜 패턴 (연도 없음)
SHORT_DATE_RE = re.compile(r"\[(\d{1,2})/(\d{1,2})\]")


def extract_date_from_title(title: str, reference_year: int = 2026) -> Optional[str]:
    """제목에서 [M/D] 형태의 날짜를 추출하고 연도를 추정.

    Args:
        title: 제목 문자열 (예: "[1/27] 기자회견", "[12/29] 참사 1주기")
        reference_year: 기준 연도 (기본값 2026)

    Returns:
        YYYY-MM-DD 형식의 날짜 문자열 또는 None
    """
    if not title:
        return None

    m = SHORT_DATE_RE.search(title)
    if not m:
        return None

    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None

    # 현재 월 기준으로 연도 추정: 현재 1월인데 12월 날짜면 작년
    from datetime import date
    today = date.today()
    current_month = today.month

    if month > current_month + 1:  # 현재 월보다 2개월 이상 뒤면 작년으로 추정
        year = reference_year - 1
    else:
        year = reference_year

    return f"{year}-{month:02d}-{day:02d}"


def clean_title_text(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    text = re.sub(r"등록일\s*\d{4}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2}", "", text).strip()
    return text


def debug_enabled(t: Target) -> bool:
    return t.site in DEBUG_SITES


def debug_log(t: Target, msg: str) -> None:
    if debug_enabled(t):
        print(f"[DIAG] {t.site} {msg}")


# ----------------------------
# Site adapters (list-page parsers)
# Each adapter returns List[ListItem]
# ----------------------------

# basicincomeparty.kr uses KBoard links like:
# /news/briefing?mod=document&pageid=1&uid=8876
# /news/press?mod=document&pageid=1&uid=...
BASICINCOME_POST_PATHS = {"/news/briefing", "/news/press"}
UID_RE = re.compile(r"(?:^|&)uid=(\d+)(?:&|$)")

# Some sites navigate via onclick handlers rather than <a href>
ONCLICK_URL_QUOTED_RE = re.compile(r"(['\"])((?:https?://|/)[^'\"]+)\1")
ONCLICK_URL_BARE_RE = re.compile(r"(https?://[^\s'\"]+|/[^\s'\"]+)")


def extract_url_from_onclick(onclick: str) -> Optional[str]:
    """Best-effort extraction of a URL from inline JS like:
    - location.href='/news/briefing/123'
    - window.location="/news/briefing/123"
    """
    if not onclick:
        return None

    m = ONCLICK_URL_QUOTED_RE.search(onclick)
    if m:
        return m.group(2)

    m = ONCLICK_URL_BARE_RE.search(onclick)
    if m:
        return m.group(1)

    return None


def extract_href_from_attrs(attrs: dict) -> Optional[str]:
    for key in ("href", "data-href", "data-url", "data-link"):
        val = attrs.get(key)
        if val:
            return str(val)

    for key in ("data-no", "data-idx", "data-id", "data-seq"):
        val = attrs.get(key)
        if val and str(val).isdigit():
            return f"/news/briefing/{val}"

    return None


def list_basicincomeparty(session: requests.Session, t: Target) -> List[ListItem]:
    """기본소득당 논평/보도자료 목록 크롤링 (2026-02 개편 후 새 사이트 구조).

    새 구조: /bikr/press 페이지, table tbody tr 행 기반
    - 카테고리: a.bo_cate_link (논평/보도자료)
    - 제목/링크: div.bo_tit > a[href]
    - 날짜: 마지막 td (YYYY.MM.DD. 형식)
    - 페이지네이션: ?page=N
    """
    out: List[ListItem] = []
    seen = set()

    BASICINCOME_DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")

    max_pages = 3
    for page_num in range(1, max_pages + 1):
        if page_num == 1:
            page_url = t.list_url
        else:
            page_url = f"{t.list_url}?page={page_num}"

        html = fetch_html(session, page_url)
        soup = BeautifulSoup(html, "html.parser")

        page_items = 0

        for row in soup.select("table tbody tr"):
            # 제목과 링크: div.bo_tit > a
            tit_a = row.select_one("div.bo_tit a[href]")
            if not tit_a:
                continue
            href = (tit_a.get("href") or "").strip()
            if not href:
                continue
            abs_url = urljoin(t.list_url, href)

            title = tit_a.get_text(" ", strip=True)
            title = re.sub(r'\bNew\b', '', title, flags=re.IGNORECASE).strip()
            title = re.sub(r'\b새글\b', '', title).strip()
            if not title:
                continue

            # 카테고리 (논평/보도자료)
            cate_a = row.select_one("a.bo_cate_link")
            category = cate_a.get_text(strip=True) if cate_a else t.category

            # 날짜: 마지막 td (YYYY.MM.DD. 형식)
            cols = row.find_all("td")
            date = None
            if cols:
                date_text = cols[-1].get_text(strip=True)
                m = BASICINCOME_DATE_RE.search(date_text)
                if m:
                    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

            if abs_url in seen:
                continue
            seen.add(abs_url)

            out.append(ListItem(party=t.party, category=category, title=title, url=abs_url, date=date))
            page_items += 1

        if page_items == 0:
            break

        time.sleep(0.5)

    return out


def list_samindang(session: requests.Session, t: Target) -> List[ListItem]:
    """사회민주당 브리핑 목록 페이지에서 글 링크를 수집.

    사민당 페이지는 카드/영역 클릭(=onclick)으로 이동하는 경우가 있어
    1) a[href]
    2) [onclick]에서 URL 추출
    두 경로를 모두 수집한다.
    """
    html = fetch_html(session, t.list_url, headers={"Referer": t.list_url}, encoding="auto")
    soup = BeautifulSoup(html, "html.parser")

    out: List[ListItem] = []
    seen = set()

    NAV_WORDS = {"브리핑", "공지", "보도자료", "정책", "소식", "검색", "전체", "자료실", "당원가입", "로그인", "소개", "소통", "후원하기"}

    date_re = re.compile(r"(?:등록일\s*)?(\d{4}-\d{2}-\d{2})")

    def normalize_title_and_date(title: str) -> Tuple[str, Optional[str]]:
        if not title:
            return "", None
        m = date_re.search(title)
        date = m.group(1) if m else None
        if date:
            title = date_re.sub("", title).strip()
        return title, date

    def clean_title(text: str) -> str:
        text = re.sub(r"\s+", " ", (text or "")).strip()
        text = re.sub(r"등록일\s*\d{4}-\d{2}-\d{2}", "", text).strip()
        if "[" in text and "]" in text:
            prefix = text.split("[", 1)[0].strip()
            m = re.search(r"\[[^\]]+\]", text)
            if m:
                candidate = (prefix + " " + m.group(0)).strip()
                if len(candidate) >= 6:
                    return candidate
        return text

    def extract_title_from_node(node: BeautifulSoup) -> str:
        # Prefer explicit title elements to avoid excerpt text.
        title_el = node.select_one(
            ".contentBox .title, p.title, .title, .subject, .tit, h1, h2, h3, h4, h5"
        )
        if title_el:
            return clean_title(title_el.get_text(" ", strip=True))

        a = node.select_one("a")
        if a:
            # If the anchor wraps multiple blocks, try to pick the first title-like child.
            child_title = a.select_one(".title, .subject, .tit, h1, h2, h3, h4, h5")
            if child_title:
                return clean_title(child_title.get_text(" ", strip=True))
            return clean_title(a.get_text(" ", strip=True))

        return clean_title(node.get_text(" ", strip=True))

    def extract_date_from_node(node: BeautifulSoup) -> Optional[str]:
        date_el = node.select_one(".info .date, .date")
        if date_el:
            _, date = normalize_title_and_date(date_el.get_text(" ", strip=True))
            return date
        return None


    def add_candidate(title: str, href: str, date: Optional[str] = None) -> None:
        href = (href or "").strip()
        if not href:
            return
        if href.startswith("javascript"):
            return

        # Candidate URL heuristic
        if ("/news/" not in href) and ("briefing" not in href):
            return

        abs_url = urljoin(t.list_url, href)
        parsed = urlparse(abs_url)

        # Only keep internal links
        if parsed.netloc and "samindang.kr" not in parsed.netloc:
            return

        # Only keep actual news-area URLs
        if not parsed.path.startswith("/news/"):
            return

        # Avoid the list page itself
        if abs_url.rstrip("/") == t.list_url.rstrip("/"):
            return

        clean_title, title_date = normalize_title_and_date(title)
        if not clean_title:
            return
        if clean_title in NAV_WORDS:
            return
        if len(clean_title) < 6:
            return

        if abs_url in seen:
            return
        seen.add(abs_url)

        out.append(
            ListItem(
                party=t.party,
                category=t.category,
                title=clean_title,
                url=abs_url,
                date=date or title_date,
            )
        )

    def extract_id_from_text(text: str) -> Optional[str]:
        if not text:
            return None
        m = re.search(r"/news/briefing/(\d+)", text)
        if m:
            return m.group(1)
        m = re.search(r"\b(\d{3,6})\b", text)
        if m:
            return m.group(1)
        return None

    # 0) Prefer explicit briefing list items to avoid title+excerpt contamination.
    list_nodes = soup.select("li[data-url*='/news/briefing/'], li[id^='id_']")
    if list_nodes:
        for li in list_nodes:
            title = extract_title_from_node(li)
            extracted_date = extract_date_from_node(li)

            href = extract_href_from_attrs(li.attrs)
            if not href:
                onclick = (li.get("onclick") or "").strip()
                href = extract_url_from_onclick(onclick)
            if not href:
                inferred_id = extract_id_from_text(str(li))
                if inferred_id:
                    href = f"/news/briefing/{inferred_id}"

            if href:
                add_candidate(title, href, extracted_date)
    else:
        # Fallback: try common list containers.
        for li in soup.select(
            ".admin_list li, .board_list li, .board_list tr, .list li, .notice_list li, .news_list li"
        ):
            title = extract_title_from_node(li)
            extracted_date = extract_date_from_node(li)

            href = None
            a = li.select_one("a[href]")
            if a:
                href = a.get("href")

            if not href:
                onclick = (li.get("onclick") or "").strip()
                href = extract_url_from_onclick(onclick)

            if not href:
                href = extract_href_from_attrs(li.attrs)

            if not href:
                inferred_id = extract_id_from_text(str(li))
                if inferred_id:
                    href = f"/news/briefing/{inferred_id}"

            if href:
                add_candidate(title, href, extracted_date)

    if not list_nodes:
        # 1) Normal anchor links
        for a in soup.select("a[href]"):
            title = a.get_text(" ", strip=True)
            href = a.get("href") or ""
            add_candidate(title, href)

        # 2) Clickable blocks (onclick navigation)
        for el in soup.select("[onclick]"):
            onclick = (el.get("onclick") or "").strip()
            u = extract_url_from_onclick(onclick)
            if not u:
                continue
            title = el.get_text(" ", strip=True)
            add_candidate(title, u)

        # 3) Any tag with data-* URL-ish attrs
        for el in soup.find_all(True):
            href = extract_href_from_attrs(el.attrs)
            if not href:
                continue
            title = el.get_text(" ", strip=True)
            add_candidate(title, href)

    return out


def list_rebuildingkoreaparty(session: requests.Session, t: Target) -> List[ListItem]:
    """조국혁신당 보도자료 목록 페이지에서 글 링크를 수집."""
    # This site renders lists via API (JS), so use JSON endpoint.
    parsed_list = urlparse(t.list_url)
    path = parsed_list.path.rstrip("/")

    # URL 경로별 카테고리 ID 매핑 (API에서 카테고리별로 다른 ID 사용)
    category_id_map = {
        "/news/commentary-briefing": 7,   # 논평 브리핑
        "/news/press-conference": 6,       # 기자회견문
        "/news/press-release": 9,          # 보도자료
    }

    api_url = "https://api.rebuildingkoreaparty.kr/api/board/list"
    category_id = category_id_map.get(path, 7)  # 기본값 7 (논평 브리핑)

    payload = {
        "page": 1,
        "categoryId": category_id,
        "recordSize": 10,
        "pageSize": 5,
        "order": "recent",
    }

    data = fetch_json(
        session,
        api_url,
        payload=payload,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Referer": "https://rebuildingkoreaparty.kr/",
        },
    )

    out: List[ListItem] = []
    seen = set()

    post_path_re = re.compile(r"^/news/[^/]+/\d+$")

    def add_candidate(title: str, href: str, date: Optional[str] = None, content: Optional[str] = None) -> None:
        href = (href or "").strip()
        if not href:
            return
        if href.startswith("javascript"):
            return

        abs_url = urljoin(t.list_url, href)
        parsed = urlparse(abs_url)
        if parsed.netloc and "rebuildingkoreaparty.kr" not in parsed.netloc:
            return

        if not parsed.path.startswith("/news/"):
            return
        if not post_path_re.match(parsed.path):
            return
        if abs_url.rstrip("/") == t.list_url.rstrip("/"):
            return

        clean_title = clean_title_text(title)
        if not clean_title or abs_url in seen:
            return

        seen.add(abs_url)
        out.append(
            ListItem(
                party=t.party,
                category=t.category,
                title=clean_title,
                url=abs_url,
                date=date,
                content=content,
            )
        )

    def extract_items(obj: dict) -> list:
        for key in ("list", "items", "contents", "result"):
            val = obj.get(key)
            if isinstance(val, list):
                return val
        return []

    items: list = []
    if isinstance(data, dict):
        items = extract_items(data)
        if not items and isinstance(data.get("data"), dict):
            items = extract_items(data.get("data"))

    for row in items:
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("subject") or ""
        date = extract_date_from_text(str(row.get("createdAt") or row.get("date") or row.get("regDate") or ""))
        # API에서 본문 텍스트 추출 (descriptionText 사용)
        content = row.get("descriptionText") or row.get("description") or ""

        # API가 categoryId로 필터링하므로 추가 카테고리 검증 불필요
        href = row.get("url") or row.get("path") or ""
        if href:
            href = urljoin(t.list_url, href)
        else:
            post_id = row.get("id") or row.get("boardId") or row.get("idx")
            if not post_id:
                continue
            href = f"{parsed_list.scheme}://{parsed_list.netloc}{path}/{post_id}"

        add_candidate(title, href, date, content)

    return out


def list_jinboparty(session: requests.Session, t: Target) -> List[ListItem]:
    """진보당 논평 목록 페이지에서 글 링크를 수집."""
    html = fetch_html(session, t.list_url, headers={"Referer": t.list_url}, encoding="auto")
    soup = BeautifulSoup(html, "html.parser")

    out: List[ListItem] = []
    seen = set()

    base_qs = parse_qs(urlparse(t.list_url).query)
    expected_board = (base_qs.get("b") or [None])[0]

    def build_read_url(bn: str) -> str:
        parsed = urlparse(t.list_url)
        qs = parse_qs(parsed.query)
        qs["bn"] = [bn]
        qs["m"] = ["read"]
        if "nPage" not in qs:
            qs["nPage"] = ["1"]
        if "nPageSize" not in qs:
            qs["nPageSize"] = ["20"]
        if "f" not in qs:
            qs["f"] = ["ALL2"]
        # Keep p and b from list URL, drop fragment.
        query = urlencode({k: v[0] for k, v in qs.items()})
        return parsed._replace(query=query, fragment="").geturl()

    def fetch_detail_title_date(url: str) -> Tuple[Optional[str], Optional[str]]:
        detail_html = fetch_html(session, url, headers={"Referer": t.list_url}, encoding="auto")
        detail_soup = BeautifulSoup(detail_html, "html.parser")

        title = None
        og_title = detail_soup.select_one("meta[property='og:title']")
        if og_title and og_title.get("content"):
            title = og_title.get("content")
        if not title:
            title_el = detail_soup.select_one(".view_title, .title, .subject, h1, h2")
            if title_el:
                title = title_el.get_text(" ", strip=True)
        if not title:
            title = detail_soup.title.get_text(" ", strip=True) if detail_soup.title else None

        if title:
            title = clean_title_text(recover_text(title))

        date = None
        date_el = detail_soup.select_one(".date, .view_date, .write_date, .info_date")
        if date_el:
            date = extract_date_from_text(date_el.get_text(" ", strip=True))
        if not date:
            date = extract_date_from_text(detail_soup.get_text(" ", strip=True))

        return title, date

    def add_candidate(title: str, href: str, date: Optional[str] = None) -> None:
        href = (href or "").strip()
        if not href or href.startswith("javascript"):
            return

        abs_url = urljoin(t.list_url, href)
        parsed = urlparse(abs_url)
        if parsed.netloc and "jinboparty.com" not in parsed.netloc:
            return

        qs = parse_qs(parsed.query)
        if expected_board and (qs.get("b") or [None])[0] != expected_board:
            return

        if expected_board:
            has_id = any(k in qs for k in ["bn", "sno", "idx", "no", "article", "view"])
            if not has_id:
                return

        clean_title = clean_title_text(title)
        if not clean_title or abs_url in seen:
            return

        seen.add(abs_url)
        out.append(
            ListItem(
                party=t.party,
                category=t.category,
                title=clean_title,
                url=abs_url,
                date=date,
            )
        )

    list_nodes = soup.select(
        "section.table, .board_list tr, .board_list li, .list li, .news_list li, .img_list_item"
    )
    if debug_enabled(t):
        debug_log(t, f"HTML len: {len(html)}")
        debug_log(t, f"a[href]: {len(soup.select('a[href]'))}")

    for node in list_nodes:
        title_el = node.select_one(".tb_title_area .title, .title, .subject, .tit, ._tit, h4, a")
        title = title_el.get_text(" ", strip=True) if title_el else node.get_text(" ", strip=True)
        date = extract_date_from_text(node.get_text(" ", strip=True))
        date_el = node.select_one(".col.wid_140")
        if date_el:
            date = extract_date_from_text(date_el.get_text(" ", strip=True)) or date
        if not date:
            date_el = node.select_one(".item_bottom span")
            if date_el:
                date = extract_date_from_text(date_el.get_text(" ", strip=True)) or date

        href = None
        a = node.select_one("a[href]")
        if a:
            href = a.get("href")
        if not href:
            href = extract_href_from_attrs(node.attrs)
        if not href:
            onclick = (node.get("onclick") or "").strip()
            href = extract_url_from_onclick(onclick)

        if not href and a:
            onclick = (a.get("onclick") or "").strip()
            href = extract_url_from_onclick(onclick)

        if href and "js_board_view" in href:
            m = re.search(r"js_board_view\(['\"](\d+)['\"]\)", href)
            if m:
                href = build_read_url(m.group(1))

        if href:
            detail_title = None
            detail_date = None
            if "bn=" in href:
                detail_title, detail_date = fetch_detail_title_date(href)
            add_candidate(detail_title or title, href, detail_date or date)

    if not out:
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            title = a.get_text(" ", strip=True)
            date = extract_date_from_text(a.get_text(" ", strip=True))
            add_candidate(title, href, date)

    if debug_enabled(t):
        sample = [a.get("href") for a in soup.select("a[href]") if expected_board and expected_board in (a.get("href") or "")]
        debug_log(t, f"board href sample: {sample[:8]}")

    return out


def list_laborparty(session: requests.Session, t: Target) -> List[ListItem]:
    """노동당 공지 목록 페이지에서 글 링크를 수집."""
    html = fetch_html(session, t.list_url, headers={"Referer": t.list_url})
    soup = BeautifulSoup(html, "html.parser")

    out: List[ListItem] = []
    seen = set()

    def add_candidate(title: str, href: str, date: Optional[str] = None) -> None:
        href = (href or "").strip()
        if not href or href.startswith("javascript"):
            return

        abs_url = urljoin(t.list_url, href)
        parsed = urlparse(abs_url)
        if parsed.netloc and "laborparty.kr" not in parsed.netloc:
            return

        clean_title = clean_title_text(title)
        if not clean_title or abs_url in seen:
            return

        seen.add(abs_url)
        out.append(
            ListItem(
                party=t.party,
                category=t.category,
                title=clean_title,
                url=abs_url,
                date=date,
            )
        )

    list_nodes = soup.select(".kboard-list tbody tr")
    if debug_enabled(t):
        debug_log(t, f"HTML len: {len(html)}")
        debug_log(t, f"a[href]: {len(soup.select('a[href]'))}")

    for node in list_nodes:
        title_el = node.select_one(".kboard-thumbnail-cut-strings")
        if title_el:
            title = title_el.get_text(" ", strip=True)
        else:
            title = node.get_text(" ", strip=True)
        title = clean_title_text(title).replace("New", "").strip()

        date = None
        date_el = node.select_one(".kboard-mobile-contents .kboard-date")
        if date_el:
            date = extract_date_from_text(date_el.get_text(" ", strip=True))
        if not date:
            date_el = node.select_one("p.date span")
            if date_el:
                date = extract_date_from_text(date_el.get_text(" ", strip=True))
        if not date:
            date = extract_date_from_text(node.get_text(" ", strip=True))

        href = None
        a = node.select_one("a[href*='uid='][href*='mod=document']")
        if a:
            href = a.get("href")

        if href:
            add_candidate(title, href, date)

    if not out:
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            title = a.get_text(" ", strip=True)
            date = extract_date_from_text(a.get_text(" ", strip=True))
            add_candidate(title, href, date)

    if debug_enabled(t):
        sample = [a.get("href") for a in soup.select("a[href]") if "laborparty" in (a.get("href") or "")]
        debug_log(t, f"laborparty href sample: {sample[:8]}")

    return out


def list_kgreens(session: requests.Session, t: Target) -> List[ListItem]:
    """녹색당 목록 페이지에서 글 링크를 수집.

    녹색당 사이트는 페이지에 따라 다른 구조를 사용:
    1. ul.li_body 구조 (press, statement 등)
    2. div.card 구조 (event, address 등)
    """
    html = fetch_html(session, t.list_url)
    soup = BeautifulSoup(html, "html.parser")

    out: List[ListItem] = []
    seen = set()

    def add_item(href: str, title: str, date: Optional[str] = None) -> None:
        href = (href or "").strip()
        if not href or 'bmode=view' not in href:
            return
        abs_url = urljoin(t.list_url, href)
        if abs_url in seen:
            return
        title = (title or "").strip()
        if not title or len(title) < 6:
            return
        # 날짜가 없으면 제목에서 [M/D] 형태 추출 시도
        if not date:
            date = extract_date_from_title(title)
        seen.add(abs_url)
        out.append(ListItem(party=t.party, category=t.category, title=title, url=abs_url, date=date))

    # 방법 1: ul.li_body 구조 (press, statement 등)
    li_bodies = soup.find_all('ul', class_='li_body')
    for li_body in li_bodies:
        a = li_body.find('a', class_='list_text_title')
        if not a:
            a = li_body.find('a', href=lambda x: x and 'bmode=view' in x)
        if not a:
            continue

        href = a.get("href") or ""
        title = a.get_text(" ", strip=True)

        date = None
        date_li = li_body.find('li', class_='time')
        if date_li:
            date_text = date_li.get('title', '') or date_li.get_text(strip=True)
            if date_text:
                date = extract_date_from_text(date_text)

        add_item(href, title, date)

    # 방법 2: div.card 구조 (event, address 등)
    if not out:
        cards = soup.select('div.card a.post_link_wrap[href*="bmode=view"]')
        for a in cards:
            href = a.get("href") or ""
            # 카드 내 제목은 .title 클래스 또는 card-body 내 텍스트
            title_el = a.select_one('.title')
            if title_el:
                title = title_el.get_text(" ", strip=True)
            else:
                title = a.get_text(" ", strip=True)

            # 날짜는 카드 내 small 태그에 있을 수 있음
            date = None
            card = a.find_parent('div', class_='card')
            if card:
                date_el = card.select_one('small, .date, .time')
                if date_el:
                    date = extract_date_from_text(date_el.get_text(strip=True))

            add_item(href, title, date)

    # 방법 3: 위 방법 실패 시 모든 bmode=view 링크 수집
    if not out:
        for a in soup.find_all('a', href=lambda x: x and 'bmode=view' in x):
            href = a.get("href") or ""
            title = a.get_text(" ", strip=True)
            add_item(href, title)

    return out


# 정의당(Justice21) 게시판 목록 페이지에서 글 링크를 수집
def list_justice21(session: requests.Session, t: Target) -> List[ListItem]:
    """정의당(Justice21) 게시판 목록 페이지에서 글 링크를 수집.

    목록 페이지 URL 예:
    - https://www.justice21.org/newhome/board/board.html?bbs_code=JS21

    글 링크는 보통 다음 형태:
    - board_view.html?bbs_code=JS21&bbs_no=18761
    """
    html = fetch_html(session, t.list_url)
    soup = BeautifulSoup(html, "html.parser")

    base_qs = parse_qs(urlparse(t.list_url).query)
    expected_code = (base_qs.get("bbs_code") or [None])[0]

    out: List[ListItem] = []
    seen = set()

    for a in soup.select("a"):
        href = (a.get("href") or "").strip()

        # Some rows may navigate via onclick
        if not href:
            onclick = (a.get("onclick") or "").strip()
            extracted = extract_url_from_onclick(onclick)
            if extracted:
                href = extracted

        if not href:
            continue

        # Only post-view links
        if "board_view" not in href:
            continue

        abs_url = urljoin(t.list_url, href)
        qs = parse_qs(urlparse(abs_url).query)

        # Board code: allow missing in the link (we'll still accept it), but reject mismatches
        link_code = (qs.get("bbs_code") or [None])[0]
        if expected_code and link_code and link_code != expected_code:
            continue

        # Post id key differs by board/page
        post_no = (qs.get("bbs_no") or qs.get("num") or qs.get("no") or [None])[0]
        if not post_no:
            continue

        title = a.get_text(" ", strip=True)
        if not title:
            continue

        if abs_url in seen:
            continue
        seen.add(abs_url)

        # Try to extract date from the parent row/list item
        date = None
        parent = a.find_parent(['tr', 'li', 'div'])
        if parent:
            date = extract_date_from_text(parent.get_text(" ", strip=True))

        out.append(ListItem(party=t.party, category=t.category, title=title, url=abs_url, date=date))

    return out


def list_placeholder(session: requests.Session, t: Target) -> List[ListItem]:
    # We haven't implemented this site's parser yet.
    # Keeping a placeholder lets the pipeline run without breaking.
    print(f"[SKIP] 아직 파서 미구현: {t.site} ({t.party} / {t.category}) → {t.list_url}")
    return []


ADAPTERS: dict[str, Callable[[requests.Session, Target], List[ListItem]]] = {
    "basicincomeparty": list_basicincomeparty,
    "samindang": list_samindang,
    "rebuildingkoreaparty": list_rebuildingkoreaparty,
    "jinboparty": list_jinboparty,
    "laborparty": list_laborparty,
    "kgreens": list_kgreens,
    "justice21": list_justice21,
}


# ----------------------------
# Runner
# ----------------------------


def load_targets(path: str = "config/sources.json") -> List[Target]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [Target(**x) for x in raw]


def run_list_only(targets: Iterable[Target], per_site_delay_sec: float = 1.2) -> List[ListItem]:
    session = get_session()
    all_items: List[ListItem] = []

    for t in targets:
        adapter = ADAPTERS.get(t.site)
        if adapter is None:
            print(f"[SKIP] 아직 파서 미구현: {t.site} ({t.party} / {t.category}) → {t.list_url}")
            time.sleep(per_site_delay_sec)
            continue

        try:
            items = adapter(session, t)
            print(f"[OK] {t.party} / {t.category} → {len(items)}개")
            all_items.extend(items)
        except Exception as e:
            print(f"[ERR] {t.party} / {t.category} 실패: {e}")

        time.sleep(per_site_delay_sec)

    return all_items




def main() -> int:
    import argparse
    from datetime import datetime

    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/sources.json", help="targets config JSON path")
    p.add_argument("--only", default="", help="run only one site id (e.g., justice21)")
    p.add_argument("--exclude", default="", help="comma-separated site ids to exclude (e.g., jinboparty)")
    p.add_argument("--only-category", default="", help="run only one category name (exact match)")
    p.add_argument("--only-id", default="", help="run only one target id (exact match)")
    p.add_argument("--sample", type=int, default=15, help="how many sample items to print")
    p.add_argument("--debug", default="", help="comma-separated site ids for diagnostics")
    p.add_argument("--notion", action="store_true", help="upload results to Notion database")
    p.add_argument("--date-from", default="2026-01-01", help="filter items from this date (YYYY-MM-DD), default: 2026-01-01")
    args = p.parse_args()

    global DEBUG_SITES
    if args.debug:
        DEBUG_SITES = {x.strip() for x in args.debug.split(",") if x.strip()}

    targets = load_targets(args.config)
    if args.only:
        targets = [t for t in targets if t.site == args.only]
    if args.exclude:
        exclude_sites = {x.strip() for x in args.exclude.split(",") if x.strip()}
        targets = [t for t in targets if t.site not in exclude_sites]
    if args.only_category:
        targets = [t for t in targets if t.category == args.only_category]
    if args.only_id:
        targets = [t for t in targets if t.id == args.only_id]

    items = run_list_only(targets)

    # Filter excluded URLs first
    items = [it for it in items if it.url not in EXCLUDED_URLS and normalize_url(it.url) not in EXCLUDED_URLS]

    # Filter by date if specified
    if args.date_from:
        try:
            cutoff_date = datetime.strptime(args.date_from, "%Y-%m-%d").date()
            filtered_items = []
            skipped_no_date = 0

            for it in items:
                if it.date:
                    try:
                        item_date = datetime.strptime(it.date, "%Y-%m-%d").date()
                        if item_date >= cutoff_date:
                            filtered_items.append(it)
                    except ValueError:
                        # If date parsing fails, skip the item
                        skipped_no_date += 1
                else:
                    # If no date, skip the item (can't verify it's recent)
                    skipped_no_date += 1

            original_count = len(items)
            items = filtered_items
            print(f"[INFO] Filtered by date >= {args.date_from}: {len(items)}/{original_count} items ({skipped_no_date} items kept without dates)")
        except ValueError:
            print(f"[WARN] Invalid date format: {args.date_from}. Expected YYYY-MM-DD. Skipping filter.")

    print(f"\n==== 샘플 출력 (최대 {args.sample}개) ====")
    for it in items[: args.sample]:
        date_suffix = f" ({it.date})" if it.date else ""
        print(f"- [{it.party}/{it.category}] {it.title}{date_suffix}")
        print(f"  {it.url}")

    if args.notion:
        upload_to_notion(items)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
