#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_news.py — 3-Wave Dashboard 뉴스 폴백 수집기

원칙:
  * 실제 검색으로 확인된 기사만 저장한다. 제목/매체/날짜/URL을 생성하지 않는다.
  * 결과가 5건 미만이면 있는 만큼만 저장하고 found_count 로 알린다.
  * 0건이면 빈 배열 + status_reason 을 남긴다.
  * importance_score 는 기계적으로 계산하고 산출 근거를 importance_basis 에 남긴다.

경로 우선순위: Google News RSS(키 불필요) → NewsAPI(NEWSAPI_KEY) → Bing News(BING_NEWS_KEY)
출력: news.json
"""

import html
import json
import math
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "scoring_config.json")
MANUAL_PATH = os.path.join(HERE, "manual_inputs.json")
OUT_PATH = os.path.join(HERE, "news.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
TIMEOUT = 25
SSL_CTX = ssl.create_default_context()

NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "").strip()
BING_NEWS_KEY = os.environ.get("BING_NEWS_KEY", "").strip()
RESOLVE_LIMIT = int(os.environ.get("RESOLVE_LIMIT", "6"))  # 지표당 원문 URL 해석 시도 건수
# Google 은 짧은 간격의 연속 요청에 연결을 끊는다([Errno 54]). 간격을 넉넉히 둔다.
REQUEST_GAP_SEC = float(os.environ.get("REQUEST_GAP_SEC", "1.5"))
RETRY_BASE_SEC = float(os.environ.get("RETRY_BASE_SEC", "5.0"))

_url_cache = {}


def http_get(url, headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read()


# ----------------------------------------------------------------------------
# 텍스트 유틸
# ----------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[a-z0-9]+|[가-힣]+")
_STOP = {"the", "a", "an", "of", "for", "to", "in", "on", "and", "is", "as", "at",
         "with", "by", "from", "its", "it", "be", "will", "that", "this", "says", "say"}


def tokens(text):
    out = set()
    for t in _TOKEN_RE.findall((text or "").lower()):
        if len(t) <= 1 or t in _STOP:
            continue
        out.add(t)
    return out


def norm_title(t):
    t = html.unescape(t or "").lower()
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", t)      # Google RSS 의 " - 매체명" 접미사 제거
    t = re.sub(r"[^a-z0-9가-힣]+", " ", t)
    return " ".join(t.split())


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def domain_of(url):
    try:
        h = urllib.parse.urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:  # noqa: BLE001
        return ""


def parse_rfc2822(s):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


# ----------------------------------------------------------------------------
# Google News RSS
# ----------------------------------------------------------------------------
def google_news_rss(query, feed, limit, retries=4):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query, "hl": feed["hl"], "gl": feed["gl"], "ceid": feed["ceid"],
    })
    # 실패 원인은 두 가지다.
    #   * DNS 일시 실패([Errno 8]) — 산발적, 짧게 쉬면 복구
    #   * 연결 강제 종료([Errno 54] Connection reset) — Google 의 레이트리밋.
    #     짧게 재시도하면 오히려 더 막히므로 지수 백오프에 지터를 섞어 물러선다.
    last = None
    for attempt in range(retries + 1):
        try:
            raw = http_get(url)
            break
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                wait = RETRY_BASE_SEC * (2 ** attempt) + random.uniform(0, 2.0)
                time.sleep(wait)
    else:
        raise last
    root = ET.fromstring(raw)
    items = []
    for it in root.findall("./channel/item")[:limit]:
        title_raw = it.findtext("title") or ""
        link = it.findtext("link") or ""
        pub = parse_rfc2822(it.findtext("pubDate") or "")
        src_el = it.find("{http://news.google.com/}source")
        if src_el is None:
            src_el = it.find("source")
        source_name = (src_el.text if src_el is not None else "") or ""
        source_url = (src_el.attrib.get("url") if src_el is not None else "") or ""
        # Google 은 제목 끝에 " - 매체명" 을 붙인다. 매체명이 도메인으로 오는 경우도 있어
        # (예: source="biz.chosun.com" 인데 제목 접미사는 "조선비즈") 일반 규칙으로도 한 번 더 제거한다.
        title = html.unescape(title_raw).strip()
        if source_name:
            title = re.sub(r"\s+-\s+" + re.escape(source_name) + r"\s*$", "", title).strip()
        title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip() or html.unescape(title_raw).strip()
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        desc = " ".join(desc.split())
        # Google News RSS 의 description 은 "<a>제목</a> 매체명" 형태라 제목과 사실상 동일하다.
        # 제목을 그대로 반복하는 snippet 은 화면에서 노이즈이므로 비운다.
        if norm_title(desc).startswith(norm_title(title)[:40]) or \
                jaccard(tokens(desc), tokens(title)) >= 0.8:
            desc = ""
        items.append({
            "title": title,
            "source": source_name,
            "publisher_domain": domain_of(source_url),
            "published_at": pub.isoformat() if pub else None,
            "url": link,
            "url_is_google_redirect": "news.google.com" in link,
            "snippet": desc[:300],
            "provider": "google_news_rss",
            "query": query,
            "feed": feed["id"],
        })
    return items


def resolve_google_url(link):
    """Google News 리다이렉트 URL → 원문 URL. 실패 시 None."""
    if link in _url_cache:
        return _url_cache[link]
    result = None
    try:
        gid = link.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        page = http_get(link).decode("utf-8", "ignore")
        sig = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if sig and ts:
            inner = json.dumps(["garturlreq", [
                ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                 None, None, None, None, None, 0, 1],
                "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], gid,
                int(ts.group(1)), sig.group(1)])
            body = urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])})
            req = urllib.request.Request(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                data=body.encode(),
                headers={"User-Agent": UA,
                         "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
                txt = r.read().decode("utf-8", "ignore")
            m = re.findall(r'https?://(?!news\.google)[^\\"\s]{15,400}', txt)
            if m:
                result = m[0]
    except Exception:  # noqa: BLE001
        result = None
    _url_cache[link] = result
    return result


# ----------------------------------------------------------------------------
# 키 기반 폴백 provider
# ----------------------------------------------------------------------------
def newsapi_search(query, limit):
    q = urllib.parse.urlencode({"q": query, "pageSize": limit, "sortBy": "publishedAt",
                                "language": "en"})
    raw = http_get("https://newsapi.org/v2/everything?" + q,
                   headers={"User-Agent": UA, "X-Api-Key": NEWSAPI_KEY})
    d = json.loads(raw.decode("utf-8"))
    out = []
    for a in d.get("articles", []):
        out.append({
            "title": (a.get("title") or "").strip(),
            "source": ((a.get("source") or {}).get("name") or "").strip(),
            "publisher_domain": domain_of(a.get("url") or ""),
            "published_at": a.get("publishedAt"),
            "url": a.get("url") or "",
            "url_is_google_redirect": False,
            "snippet": (a.get("description") or "")[:300],
            "provider": "newsapi",
            "query": query,
            "feed": "en",
        })
    return out


def bing_search(query, limit):
    q = urllib.parse.urlencode({"q": query, "count": limit, "sortBy": "Date"})
    raw = http_get("https://api.bing.microsoft.com/v7.0/news/search?" + q,
                   headers={"User-Agent": UA, "Ocp-Apim-Subscription-Key": BING_NEWS_KEY})
    d = json.loads(raw.decode("utf-8"))
    out = []
    for a in d.get("value", []):
        out.append({
            "title": (a.get("name") or "").strip(),
            "source": ((a.get("provider") or [{}])[0].get("name") or "").strip(),
            "publisher_domain": domain_of(a.get("url") or ""),
            "published_at": a.get("datePublished"),
            "url": a.get("url") or "",
            "url_is_google_redirect": False,
            "snippet": (a.get("description") or "")[:300],
            "provider": "bing_news",
            "query": query,
            "feed": "en",
        })
    return out


# ----------------------------------------------------------------------------
# 중요도 산식
# ----------------------------------------------------------------------------
def tier_for(domain, tiers):
    if not domain:
        return float(tiers.get("default", 0.25)), "미상 매체"
    for score_str, domains in tiers.items():
        if score_str == "default":
            continue
        for d in domains:
            if domain == d or domain.endswith("." + d):
                return float(score_str), d
    return float(tiers.get("default", 0.25)), "미등록 매체"


def score_articles(articles, keyword_tokens, imp_cfg, now):
    w = imp_cfg["weights"]
    tiers = imp_cfg["source_tiers"]
    kw_cfg = imp_cfg["keyword_hits"]
    rec_cfg = imp_cfg["recency"]
    cl_cfg = imp_cfg["cluster"]

    # 클러스터링: 제목 토큰 유사도로 동일 사안 묶기
    tok = [tokens(a["title"]) for a in articles]
    cluster_id = [-1] * len(articles)
    clusters = []
    for i in range(len(articles)):
        if cluster_id[i] != -1:
            continue
        cid = len(clusters)
        clusters.append([i])
        cluster_id[i] = cid
        for j in range(i + 1, len(articles)):
            if cluster_id[j] == -1 and jaccard(tok[i], tok[j]) >= cl_cfg["similarity_threshold"]:
                cluster_id[j] = cid
                clusters[cid].append(j)

    for i, a in enumerate(articles):
        tier, tier_match = tier_for(a.get("publisher_domain", ""), tiers)

        hits = len(tok[i] & keyword_tokens)
        kw = min(hits * kw_cfg["per_hit"], kw_cfg["cap"])

        days = None
        rec = 0.0
        if a.get("published_at"):
            try:
                dt = datetime.fromisoformat(a["published_at"].replace("Z", "+00:00"))
                days = max(0.0, (now - dt).total_seconds() / 86400.0)
                rec = math.exp(-math.log(2) * days / rec_cfg["half_life_days"])
            except (ValueError, AttributeError):
                rec = 0.0

        csize = len(clusters[cluster_id[i]])
        cl = min((csize - 1) * cl_cfg["per_extra_article"], cl_cfg["cap"])

        raw = w["source_tier"] * tier + w["keyword_hits"] * kw + w["recency"] * rec + w["cluster"] * cl
        a["importance_score"] = round(max(0.0, min(1.0, raw)) * 100.0, 1)
        a["importance_basis"] = (
            "매체티어 %.2f(%s)×%.2f + 키워드적중 %d개→%.2f×%.2f + 최신성 %.2f(%s)×%.2f "
            "+ 동일사안 보도 %d건→%.2f×%.2f = %.1f/100"
            % (tier, tier_match, w["source_tier"],
               hits, kw, w["keyword_hits"],
               rec, ("%.1f일 경과" % days) if days is not None else "발행일 불명",
               w["recency"],
               csize, cl, w["cluster"], a["importance_score"])
        )
        a["_cluster_size"] = csize
    return articles


# ----------------------------------------------------------------------------
# 관련성 필터 (작은 파도 전용)
# ----------------------------------------------------------------------------
# 회사 이름이 제목에 없는 기사는 그 종목의 근거가 될 수 없다.
# 예: "GS price increase" 검색이 "Smiths Group FY2026 slides" 를 물어오고,
#     "S-Oil" 검색이 캐나다 Saturn Oil & Gas 기사를 물어온다.
_GENERIC_TOKENS = {
    "holdings", "holding", "technologies", "technology", "group", "corp", "corporation",
    "inc", "incorporated", "company", "companies", "systems", "international",
    "industries", "solutions", "enterprise", "enterprises", "limited", "ltd",
    "주식회사", "지주", "홀딩스",
}


def relevance_terms(cand):
    """후보 종목을 가리키는 검색 토큰 목록."""
    raw = {
        (cand.get("search_ticker") or "").strip(),
        (cand.get("company_ko") or "").strip(),
        (cand.get("company_en") or "").strip(),
        (cand.get("ticker") or "").split(".")[0].strip(),
    }
    terms = set()
    for t in raw:
        if not t:
            continue
        terms.add(t)
        # 영문 사명은 토큰으로도 쪼갠다: "Merck & Company" → "merck"
        for tok in re.split(r"[^0-9A-Za-z가-힣\-]+", t):
            if len(tok) >= 4 and tok.lower() not in _GENERIC_TOKENS:
                terms.add(tok)
    return sorted(terms, key=len, reverse=True)


def mentions_company(title, terms):
    if not terms:
        return True
    low = (title or "").lower()
    for t in terms:
        if len(t) <= 3:
            # 짧은 티커(GS, MRK)는 앞뒤가 영숫자가 아닐 때만 인정한다.
            # 한글은 영숫자가 아니므로 "GS리테일" 은 매칭된다.
            if re.search(r"(?<![0-9A-Za-z])" + re.escape(t) + r"(?![0-9A-Za-z])",
                         title or "", re.IGNORECASE):
                return True
        elif t.lower() in low:
            return True
    return False


# ----------------------------------------------------------------------------
# 지표 단위 수집
# ----------------------------------------------------------------------------
def dedup(articles, dedup_cfg):
    seen_url, seen_title, out = set(), set(), []
    thr = dedup_cfg["title_similarity_threshold"]
    kept_tok = []
    for a in articles:
        u = a.get("url", "")
        nt = norm_title(a.get("title", ""))
        if not nt:
            continue
        key_st = (a.get("source", "").lower(), nt)
        if u and u in seen_url:
            continue
        if nt in seen_title or key_st in seen_title:
            continue
        t = tokens(nt)
        if any(jaccard(t, kt) >= thr for kt in kept_tok):
            continue
        seen_url.add(u)
        seen_title.add(nt)
        seen_title.add(key_st)
        kept_tok.append(t)
        out.append(a)
    return out


def collect_for(key, keywords, cfg, log, rel_terms=None):
    news_cfg = cfg["news"]
    per_kw = news_cfg["fetch_per_keyword"]
    max_age = news_cfg["max_age_days"]
    now = datetime.now(timezone.utc)

    raw, errors, providers_used, queries_used = [], [], [], []

    for kw in keywords:
        got_any = False
        for feed in news_cfg["language_feeds"]:
            # 한글 키워드는 ko 피드에만, 영문 키워드는 en 피드에만 태운다.
            is_ko_kw = bool(re.search(r"[가-힣]", kw))
            if is_ko_kw != (feed["id"] == "ko"):
                continue
            try:
                items = google_news_rss(kw, feed, per_kw)
                raw.extend(items)
                queries_used.append({"keyword": kw, "feed": feed["id"], "count": len(items)})
                if "google_news_rss" not in providers_used:
                    providers_used.append("google_news_rss")
                got_any = True
            except Exception as e:  # noqa: BLE001
                errors.append("google_news_rss[%s/%s]: %s" % (kw, feed["id"], e))
            time.sleep(REQUEST_GAP_SEC + random.uniform(0, 0.5))

        if not got_any:
            for name, key_present, fn in (("newsapi", NEWSAPI_KEY, newsapi_search),
                                          ("bing_news", BING_NEWS_KEY, bing_search)):
                if not key_present:
                    errors.append("%s: API 키 없음(환경변수 미설정) — 건너뜀" % name)
                    continue
                try:
                    items = fn(kw, per_kw)
                    raw.extend(items)
                    queries_used.append({"keyword": kw, "feed": "en", "count": len(items)})
                    if name not in providers_used:
                        providers_used.append(name)
                    break
                except Exception as e:  # noqa: BLE001
                    errors.append("%s[%s]: %s" % (name, kw, e))

    # 기간 필터
    fresh = []
    for a in raw:
        if not a.get("published_at"):
            continue
        try:
            dt = datetime.fromisoformat(a["published_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - dt).days <= max_age:
            fresh.append(a)

    deduped = dedup(fresh, news_cfg["dedup"])

    # 종목 단위 수집이면 제목에 회사명이 없는 기사를 버린다.
    dropped_irrelevant = 0
    if rel_terms:
        before = len(deduped)
        deduped = [a for a in deduped if mentions_company(a.get("title"), rel_terms)]
        dropped_irrelevant = before - len(deduped)

    kw_tokens = set()
    for kw in keywords:
        kw_tokens |= tokens(kw)

    scored = score_articles(deduped, kw_tokens, news_cfg["importance"], now)
    scored.sort(key=lambda a: a["importance_score"], reverse=True)
    top = scored[:news_cfg["articles_per_indicator"]]

    # 상위 기사에 한해 Google 리다이렉트 URL → 원문 URL 해석 시도
    if news_cfg.get("resolve_google_urls"):
        for a in top[:RESOLVE_LIMIT]:
            if a.get("url_is_google_redirect"):
                real = resolve_google_url(a["url"])
                if real:
                    a["url"] = real
                    a["url_resolved"] = True
                    a["publisher_domain"] = domain_of(real) or a.get("publisher_domain", "")
                else:
                    a["url_resolved"] = False
                    a["url_note"] = "원문 URL 해석 실패 — Google News 리다이렉트 링크 유지(브라우저에서는 정상 이동)"
            else:
                a["url_resolved"] = True

    for a in top:
        a.pop("_cluster_size", None)

    # 요청이 한 건도 성공하지 못했다면 '결과 없음'이 아니라 '검색 실패'다.
    # 둘을 구분하지 않으면 네트워크 장애를 "해당 뉴스가 없다"로 오독하게 된다.
    net_errors = [e for e in errors if "API 키 없음" not in e]
    search_ran = bool(queries_used)
    status = "ok" if top else ("no_results" if search_ran else "fetch_failed")
    reason = None
    if not top:
        if search_ran:
            reason = "검색은 수행됐으나 조건(최근 %d일 이내)에 맞는 기사가 없습니다." % max_age
            if net_errors:
                reason += " 일부 요청 실패: " + "; ".join(net_errors[:2])
        else:
            reason = ("검색 요청이 모두 실패했습니다 — 기사가 없다는 뜻이 아닙니다. "
                      "네트워크를 확인한 뒤 다시 실행하십시오. 오류: "
                      + "; ".join(net_errors[:2] or ["원인 불명"]))
    elif len(top) < news_cfg["articles_per_indicator"]:
        status = "partial"
        reason = "중복 제거 후 %d건만 확보됨 (목표 %d건)" % (len(top), news_cfg["articles_per_indicator"])

    log("  %-32s raw=%-4d fresh=%-4d dedup=%-3d 무관제외=%-3d → %d건 (%s)"
        % (key, len(raw), len(fresh), len(deduped) + dropped_irrelevant,
           dropped_irrelevant, len(top), status))

    return {
        "key": key,
        "status": status,
        "relevance_terms": rel_terms or [],
        "dropped_irrelevant": dropped_irrelevant,
        "status_reason": reason,
        "found_count": len(deduped),
        "returned_count": len(top),
        "keywords": keywords,
        "queries": queries_used,
        "providers_used": providers_used,
        "errors": errors,
        "fetched_at": now.isoformat(),
        "articles": top,
    }


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    with open(MANUAL_PATH, encoding="utf-8") as f:
        manual = json.load(f)

    only = set(sys.argv[1:])

    def log(msg):
        print(msg, flush=True)

    print("뉴스 수집 시작 — 경로 우선순위: %s" % " → ".join(cfg["news"]["provider_priority"]))
    print("  NewsAPI 키: %s / Bing 키: %s"
          % ("있음" if NEWSAPI_KEY else "없음", "있음" if BING_NEWS_KEY else "없음"))

    result = {}

    def snapshot(partial=True):
        """지표 하나를 끝낼 때마다 news.json 을 갱신한다.

        49개 키 수집에 40분 이상 걸리는데 마지막에만 저장하면 중간에 끊겼을 때
        전부 날아간다. 진행분을 계속 반영하고, 미완료 상태임을 메타에 남긴다.
        """
        merged = dict(result)
        if partial or only:
            try:
                with open(OUT_PATH, encoding="utf-8") as f:
                    prev = json.load(f).get("indicators") or {}
                base = dict(prev)
                base.update(merged)
                merged = base
            except Exception:  # noqa: BLE001
                pass
        _write_out(cfg, merged, partial=partial)

    for ind in cfg["indicators"]:
        kws = ind.get("news_keywords") or []
        if not kws or (only and ind["key"] not in only):
            continue
        result[ind["key"]] = collect_for(ind["key"], kws, cfg, log)
        snapshot()

    # 작은 파도: 종목 × 조건
    sw = cfg["small_wave"]
    for cand in manual.get("small_wave_candidates", []):
        ticker = (cand.get("ticker") or "").strip()
        if not ticker:
            continue
        for cond in sw["conditions"]:
            key = "small:%s:%s" % (ticker, cond["key"])
            if only and key not in only:
                continue
            # 한국 종목은 종목코드(000500.KS)로 기사를 찾을 수 없으므로
            # fetch_leaders.py 가 넣어준 search_ticker(한글 회사명)를 우선 쓴다.
            search_t = (cand.get("search_ticker") or "").strip() or ticker
            kws = [t.format(ticker=search_t,
                            company=cand.get("company_en") or search_t,
                            company_ko=cand.get("company_ko") or search_t)
                   for t in cond["news_templates"]]
            kws = list(dict.fromkeys(kws))
            kws = [k for k in kws if k.strip()]
            result[key] = collect_for(key, kws, cfg, log, rel_terms=relevance_terms(cand))
            snapshot()

    if only:
        # 특정 지표만 다시 받은 경우 기존 결과와 병합한다 (snapshot 이 이미 병합해 뒀다).
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                prev = json.load(f).get("indicators") or {}
            base = dict(prev)
            base.update(result)
            result = base
            print("기존 news.json 과 병합 (이번에 갱신: %s)" % ", ".join(sorted(only)))
        except Exception as e:  # noqa: BLE001
            print("경고: 병합 실패(%s)" % e, file=sys.stderr)

    _write_out(cfg, result, partial=False)

    total = sum(v["returned_count"] for v in result.values())
    print("\nnews.json 생성 완료 — 지표 %d개, 기사 총 %d건" % (len(result), total))


def _write_out(cfg, result, partial):
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "complete": not partial,
        "meta": {
            "provider_priority": cfg["news"]["provider_priority"],
            "provider_actually_used": sorted({p for v in result.values()
                                              for p in v["providers_used"]}) or ["없음"],
            "google_rss_endpoint": "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}",
            "url_resolution": "Google News 리다이렉트 URL은 batchexecute(DotsSplashUi) 호출로 원문 URL 해석을 시도하며, 실패 시 원래 링크를 유지합니다.",
            "articles_per_indicator": cfg["news"]["articles_per_indicator"],
            "max_age_days": cfg["news"]["max_age_days"],
            "importance_formula": cfg["news"]["importance"],
            "disclaimer_ko": "중요도 산식은 검증되지 않은 휴리스틱입니다. 뉴스는 정성 참고 자료이며 종합 점수에 자동 반영되지 않습니다.",
            "newsapi_key_present": bool(NEWSAPI_KEY),
            "bing_key_present": bool(BING_NEWS_KEY),
        },
        "indicators": result,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
