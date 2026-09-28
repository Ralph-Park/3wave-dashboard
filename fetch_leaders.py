#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_leaders.py — 주도주(상대강도 모멘텀 상위) 자동 선정

하는 일:
  1. 미국(Nasdaq Screener) / 한국(KRX KIND) 에서 상장종목 유니버스를 받는다
  2. yfinance 로 가격을 배치 다운로드한다
  3. 벤치마크 대비 초과수익 기반 RS 모멘텀 점수를 계산한다
  4. 시장별 상위 N 종목을 leaders.json 에 쓰고, manual_inputs.json 의 후보 슬롯을 갱신한다

하지 않는 일:
  * 작은 파도 4개 조건(캐파 공백/증설 완료/가격 결정권/병목)은 자동 판정하지 않는다.
    이 스크립트는 '후보 목록'만 만든다. 조건 체크는 뉴스를 읽고 사람이 한다.
  * 사용자가 이미 입력한 조건 체크는 절대 덮어쓰지 않는다. 후보에서 탈락하면
    archived_candidates 로 옮겨 보존하고, 다시 후보가 되면 복원한다.

실행: .venv/bin/python fetch_leaders.py [--dry-run] [--market US|KR]
"""

import argparse
import bisect
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "scoring_config.json")
MANUAL_PATH = os.path.join(HERE, "manual_inputs.json")
OUT_PATH = os.path.join(HERE, "leaders.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

try:
    import warnings
    warnings.filterwarnings("ignore")
    import pandas as pd
    import requests
    import yfinance as yf
except ImportError as e:  # noqa: BLE001
    sys.exit(
        "필요한 라이브러리가 없습니다 (%s).\n"
        "이 스크립트는 가상환경에서 실행해야 합니다:\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/python -m pip install yfinance pandas lxml requests\n"
        "  .venv/bin/python fetch_leaders.py" % e
    )


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------------------
# 유니버스
# ----------------------------------------------------------------------------
def universe_us(cfg):
    """Nasdaq Stock Screener API. 시가총액이 함께 오므로 사전 필터가 가능하다."""
    rows, errors = [], []
    for ex in cfg["exchanges"]:
        url = ("https://api.nasdaq.com/api/screener/stocks"
               "?tableonly=true&limit=10000&exchange=" + ex)
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"},
                             timeout=60)
            r.raise_for_status()
            for x in r.json()["data"]["table"]["rows"]:
                cap_raw = (x.get("marketCap") or "").replace(",", "").strip()
                if not cap_raw.isdigit():
                    continue
                cap = int(cap_raw)
                if cap < cfg["min_market_cap"]:
                    continue
                sym = (x.get("symbol") or "").strip()
                # BRK/B 같은 클래스 표기는 yfinance 에서 BRK-B 형식이다
                if not sym or " " in sym:
                    continue
                rows.append({"symbol": sym.replace("/", "-"), "name": (x.get("name") or "").strip(),
                             "market_cap": cap, "exchange": ex, "sector": None})
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (ex, e))
    # 중복 심볼 제거 (동일 기업 복수 상장)
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: -r["market_cap"]):
        if r["symbol"] in seen:
            continue
        seen.add(r["symbol"])
        out.append(r)
    cap_n = cfg.get("top_by_market_cap")
    if cap_n:
        out = out[:cap_n]
    return out, errors


def _kind_lookup():
    """KRX KIND 상장법인목록 → {종목코드: {업종, 주요제품, 시장구분}}. 실패해도 치명적이지 않다."""
    out = {}
    for st in ("13", "14"):
        try:
            r = requests.get(
                "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=" + st,
                headers={"User-Agent": UA}, timeout=60)
            r.raise_for_status()
            df = pd.read_html(io.BytesIO(r.content), encoding="euc-kr")[0]
            df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
            for _, x in df.iterrows():
                out.setdefault(x["종목코드"], {
                    "sector": str(x.get("업종")).strip() if pd.notna(x.get("업종")) else None,
                    "products": str(x.get("주요제품")).strip() if pd.notna(x.get("주요제품")) else None,
                    "segment": x.get("시장구분"),
                })
        except Exception:  # noqa: BLE001
            continue
    return out


def universe_kr(cfg):
    """네이버 금융 모바일 API 의 시가총액 순위에서 상위 N종목만 가져온다.

    전 종목(4,000개 이상)을 yfinance 로 내려받으면 레이트리밋에 걸려 데이터가
    대량 누락되고, 그 잔여물로 만든 순위는 주도주가 아니라 '살아남은 종목'이 된다.
    """
    rows, errors = [], []
    limit = cfg.get("top_by_market_cap", 400)
    suffix = {"KOSPI": ".KS", "KOSDAQ": ".KQ"}
    skip_pat = ("스팩", "기업인수목적")

    for nm in cfg.get("naver_markets", ["KOSPI", "KOSDAQ"]):
        got, page = 0, 1
        while got < limit and page <= 40:
            u = ("https://m.stock.naver.com/api/stocks/marketValue/%s?page=%d&pageSize=100"
                 % (nm, page))
            try:
                r = requests.get(u, headers={"User-Agent": UA, "Accept": "application/json"},
                                 timeout=30)
                r.raise_for_status()
                stocks = r.json().get("stocks") or []
            except Exception as e:  # noqa: BLE001
                errors.append("%s p%d: %s" % (nm, page, e))
                break
            if not stocks:
                break
            for x in stocks:
                if got >= limit:
                    break
                code = (x.get("itemCode") or "").strip()
                name = (x.get("stockName") or "").strip()
                if not code.isdigit() or len(code) != 6:
                    continue
                if any(k in name for k in skip_pat):
                    continue
                # 우선주 제외: KRX 종목코드 6번째 자리가 0이어야 보통주다
                # (삼성전자 005930 vs 삼성전자우 005935). 네이버 시총 순위에는
                # 우선주도 함께 들어오므로 여기서 걸러낸다.
                if cfg.get("exclude_preferred_shares", True) and not code.endswith("0"):
                    continue
                if re.search(r"\d?우[B]?$", name):
                    continue
                if x.get("stockEndType") and x["stockEndType"] != "stock":
                    continue
                # marketValue 단위는 억원, accumulatedTradingValue 단위는 백만원
                try:
                    cap = int(str(x.get("marketValue", "")).replace(",", "")) * 100000000
                except ValueError:
                    cap = None
                try:
                    tv = int(str(x.get("accumulatedTradingValue", "")).replace(",", "")) * 1000000
                except ValueError:
                    tv = None
                rows.append({"symbol": code + suffix[nm], "name": name,
                             "market_cap": cap, "exchange": nm,
                             "sector": None, "products": None,
                             "naver_trading_value": tv})
                got += 1
            page += 1
        log("    %s 시총 상위 %d종목 확보" % (nm, got))

    # KIND 로 업종·주요제품 보강 (실패해도 진행)
    kind = _kind_lookup()
    if kind:
        for r in rows:
            info = kind.get(r["symbol"][:6])
            if info:
                r["sector"] = info.get("sector")
                r["products"] = info.get("products")
    else:
        errors.append("KIND 업종 정보 보강 실패 (순위 계산에는 영향 없음)")

    seen, out = set(), []
    for r in sorted(rows, key=lambda r: -(r["market_cap"] or 0)):
        if r["symbol"] in seen:
            continue
        seen.add(r["symbol"])
        out.append(r)
    return out, errors


# ----------------------------------------------------------------------------
# 가격 + 모멘텀
# ----------------------------------------------------------------------------
def _cache_paths(dcfg, mkey, period):
    d = os.path.join(HERE, dcfg.get("cache_dir", ".cache"))
    stamp = datetime.now(timezone.utc).date().isoformat()
    base = "%s_%s_%s" % (mkey, period, stamp)
    return d, os.path.join(d, base + "_close.pkl"), os.path.join(d, base + "_volume.pkl")


def download_prices(symbols, period, dcfg, mkey, use_cache=True):
    """yfinance 배치 다운로드.

    Yahoo 는 대량 요청에 레이트리밋을 건다. 청크를 작게 쪼개고 사이에 쉬며,
    실패한 청크는 백오프 후 재시도한다. 같은 날 재실행은 캐시를 쓴다.
    """
    cache_dir, cpath, vpath = _cache_paths(dcfg, mkey, period)
    if use_cache and dcfg.get("cache_enabled") and os.path.exists(cpath) and os.path.exists(vpath):
        try:
            c, v = pd.read_pickle(cpath), pd.read_pickle(vpath)
            log("    캐시 사용: %s (%d종목)" % (os.path.basename(cpath), c.shape[1]))
            return c, v
        except Exception:  # noqa: BLE001
            pass

    chunk = dcfg.get("chunk_size", 100)
    pause = dcfg.get("sleep_between_chunks_sec", 1.5)
    retries = dcfg.get("max_retries", 3)
    backoff = dcfg.get("retry_backoff_sec", 20)

    closes, vols, failed = [], [], []
    total = len(symbols)
    for i in range(0, total, chunk):
        part = symbols[i:i + chunk]
        got = False
        for attempt in range(retries):
            try:
                d = yf.download(part, period=period, interval="1d", auto_adjust=True,
                                progress=False, threads=True, group_by="column")
            except Exception as e:  # noqa: BLE001
                d = None
                log("      청크 예외: %s" % str(e)[:80])
            if d is not None and not d.empty and "Close" in d:
                c = d["Close"]
                got_syms = int(c.notna().any().sum())
                closes.append(c)
                if "Volume" in d:
                    vols.append(d["Volume"])
                log("    %d–%d / %d  →  %d종목 수신%s"
                    % (i + 1, min(i + chunk, total), total, got_syms,
                       "" if attempt == 0 else " (재시도 %d회)" % attempt))
                # 절반도 못 받았으면 레이트리밋으로 보고 한 번 더 시도
                if got_syms >= max(1, len(part) // 2) or attempt == retries - 1:
                    got = True
                    break
                closes.pop()
                if vols:
                    vols.pop()
            if attempt < retries - 1:
                wait = backoff * (attempt + 1)
                log("      수신 부족 — %d초 후 재시도" % wait)
                time.sleep(wait)
        if not got:
            failed.extend(part)
        time.sleep(pause)

    if not closes:
        return None, None
    close = pd.concat(closes, axis=1)
    volume = pd.concat(vols, axis=1) if vols else None
    if failed:
        log("    끝내 실패한 종목 %d개" % len(failed))

    if dcfg.get("cache_enabled"):
        try:
            os.makedirs(cache_dir, exist_ok=True)
            close.to_pickle(cpath)
            if volume is not None:
                volume.to_pickle(vpath)
        except Exception:  # noqa: BLE001
            pass
    return close, volume


def ret_over(series, days):
    """days 거래일 전 대비 수익률. 데이터가 모자라면 None."""
    s = series.dropna()
    if len(s) <= days:
        return None
    past = float(s.iloc[-1 - days])
    if past <= 0:
        return None
    return float(s.iloc[-1]) / past - 1.0


def score_market(mkey, mcfg, mom, universe, period, dcfg, use_cache=True):
    lb = mom["lookback_trading_days"]
    weights = mom["weights"]
    wsum = sum(weights.values()) or 1.0
    filt = mom["filters"]

    symbols = [u["symbol"] for u in universe]
    by_sym = {u["symbol"]: u for u in universe}
    log("  유니버스 %d종목 — 가격 수집 시작" % len(symbols))

    close, volume = download_prices(symbols, period, dcfg, mkey, use_cache)
    if close is None:
        return [], ["가격 데이터를 전혀 받지 못했습니다"], None, {}, 0.0

    bench_sym = mcfg["benchmark"]
    b = yf.download(bench_sym, period=period, interval="1d", auto_adjust=True,
                    progress=False)["Close"]
    if isinstance(b, pd.DataFrame):
        b = b.iloc[:, 0]
    b = b.dropna()
    bench_rets = {k: ret_over(b, d) for k, d in lb.items()}
    log("  벤치마크 %s 수익률: %s" % (bench_sym,
        {k: (None if v is None else round(v * 100, 1)) for k, v in bench_rets.items()}))

    as_of = str(close.dropna(how="all").index[-1].date())
    rows, dropped = [], {"history": 0, "dma200": 0, "turnover": 0, "drawdown": 0, "nodata": 0}

    for sym in symbols:
        if sym not in close.columns:
            dropped["nodata"] += 1
            continue
        s = close[sym].dropna()
        if len(s) < filt["min_history_days"]:
            dropped["history"] += 1
            continue

        # 유동성: 최근 60일 평균 거래대금
        turnover = None
        if volume is not None and sym in volume.columns:
            v = volume[sym].dropna()
            j = s.index.intersection(v.index)[-60:]
            if len(j) >= 20:
                turnover = float((s.loc[j] * v.loc[j]).mean())
        min_to = mcfg.get("min_avg_turnover")
        if min_to and (turnover is None or turnover < min_to):
            dropped["turnover"] += 1
            continue

        # 추세 필터: 200일 이동평균 위
        dma200 = float(s.tail(200).mean())
        last = float(s.iloc[-1])
        if filt.get("above_200dma") and last < dma200:
            dropped["dma200"] += 1
            continue

        # 52주 신고가 대비 낙폭
        high52 = float(s.tail(lb["r12m"]).max())
        dd = 0.0 if high52 <= 0 else 1.0 - last / high52
        if filt.get("max_drawdown_from_52w_high") is not None and dd > filt["max_drawdown_from_52w_high"]:
            dropped["drawdown"] += 1
            continue

        rets = {k: ret_over(s, d) for k, d in lb.items()}
        excess = {}
        for k in weights:
            r = rets.get(k)
            if r is None:
                continue
            e = r
            if mom.get("excess_over_benchmark") and bench_rets.get(k) is not None:
                e = r - bench_rets[k]
            excess[k] = e
        if not excess:
            dropped["history"] += 1
            continue

        u = by_sym[sym]
        rows.append({
            "symbol": sym, "name": u["name"], "exchange": u["exchange"],
            "sector": u.get("sector"), "products": u.get("products"),
            "market_cap": u.get("market_cap"),
            "price": round(last, 2), "as_of": as_of,
            "_rets": rets, "_excess": excess,
            "avg_turnover_60d": None if turnover is None else round(turnover),
            "pct_above_200dma": round((last / dma200 - 1) * 100, 2),
            "drawdown_from_52w_high_pct": round(dd * 100, 2),
        })

    # --- 기간별 백분위 순위로 점수화 ---------------------------------------
    # 원시 수익률을 그대로 가중합하면 스핀오프/액면병합 등으로 생긴 극단값
    # (예: 12개월 +1800%) 하나가 순위를 지배한다. 기간마다 교차 단면
    # 백분위를 구한 뒤 그 백분위를 가중평균하면 이상치의 영향이 제한된다.
    n = len(rows)
    for k in weights:
        vals = sorted((r["_excess"][k] for r in rows if k in r["_excess"]))
        m = len(vals)
        for r in rows:
            if k not in r["_excess"]:
                continue
            lo = bisect.bisect_left(vals, r["_excess"][k])
            hi = bisect.bisect_right(vals, r["_excess"][k])
            r.setdefault("_pct", {})[k] = 100.0 * ((lo + hi) / 2.0) / m if m else 0.0

    for r in rows:
        parts, acc, used = [], 0.0, 0.0
        for k, w in weights.items():
            if k not in r.get("_pct", {}):
                continue
            acc += r["_pct"][k] * w
            used += w
            parts.append({
                "period": k,
                "return_pct": round(r["_rets"][k] * 100, 2),
                "benchmark_pct": (None if bench_rets.get(k) is None
                                  else round(bench_rets[k] * 100, 2)),
                "excess_pct": round(r["_excess"][k] * 100, 2),
                "percentile": round(r["_pct"][k], 1),
                "weight": w,
            })
        r["components"] = parts
        r["rs_score"] = round(acc / used, 1) if used else None
        r["rs_excess_sum_pct"] = round(sum(r["_excess"][k] * w for k, w in weights.items()
                                           if k in r["_excess"]) / wsum * 100, 2)
        for junk in ("_rets", "_excess", "_pct"):
            r.pop(junk, None)

    rows = [r for r in rows if r["rs_score"] is not None]
    rows.sort(key=lambda r: -r["rs_score"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    n = len(rows)

    fetched = len(symbols) - dropped["nodata"]
    coverage = (fetched / len(symbols)) if symbols else 0.0
    log("  통과 %d종목 (제외: 이력부족 %d, 거래대금 %d, 200일선 아래 %d, 낙폭 %d, 데이터없음 %d)"
        % (n, dropped["history"], dropped["turnover"], dropped["dma200"],
           dropped["drawdown"], dropped["nodata"]))
    log("  유니버스 가격 수신률: %.1f%% (%d / %d)" % (coverage * 100, fetched, len(symbols)))
    return rows, [], as_of, dropped, coverage


# ----------------------------------------------------------------------------
# manual_inputs.json 갱신 (사용자 입력 보존)
# ----------------------------------------------------------------------------
COND_KEYS = ["capacity_gap", "supply_locked", "pricing_power", "theme_bottleneck"]

# Nasdaq 스크리너의 종목명에는 "Common Stock", "Class C" 같은 증권 종류 표기가 붙는다.
# 그대로 두면 뉴스 검색 키워드가 오염되므로("Dell Technologies Inc. Class C Common Stock
# pricing power") 회사 이름만 남긴다.
_NAME_NOISE = re.compile(
    r"\s*(?:\(new\)|Common Stock|Ordinary Shares?|Class\s+[A-Z]\b|American Depositary Shares?"
    r"|Depositary Shares?|Units?\b.*$|,?\s*Inc\.?$|,?\s*Incorporated$|,?\s*Corp\.?$"
    r"|,?\s*Corporation$|,?\s*Ltd\.?$|,?\s*plc$|,?\s*S\.A\.$)",
    re.IGNORECASE)


def clean_company_name(name):
    out = name or ""
    for _ in range(4):
        new_out = _NAME_NOISE.sub("", out).strip(" ,.-")
        if new_out == out:
            break
        out = new_out
    return out or (name or "")


def blank_conditions():
    return {k: {"value": None, "basis": "", "evidence_url": "", "entered_at": None}
            for k in COND_KEYS}


def has_user_input(cand):
    for c in (cand.get("conditions") or {}).values():
        if c.get("value") is not None or (c.get("basis") or "").strip() or (c.get("evidence_url") or "").strip():
            return True
    return False


def update_manual_inputs(leaders_by_market, cfg_out):
    with open(MANUAL_PATH, encoding="utf-8") as f:
        manual = json.load(f)

    prev = manual.get("small_wave_candidates") or []
    archived = manual.get("archived_candidates") or []

    # 티커 → 기존 입력 (현재 후보 + 보관함 모두에서 찾는다)
    saved = {}
    for c in list(prev) + list(archived):
        t = (c.get("ticker") or "").strip()
        if t:
            saved[t] = c

    new_list, picked = [], set()
    for mkey, rows in leaders_by_market.items():
        for r in rows:
            t = r["symbol"]
            picked.add(t)
            old = saved.get(t)
            clean = clean_company_name(r["name"])
            # 뉴스 검색에서 {ticker} 자리에 쓸 문자열.
            # 한국 언론은 종목코드를 쓰지 않으므로 한글 회사명을 넣는다.
            search_ticker = clean if mkey == "KR" else t
            cand = {
                "ticker": t,
                "search_ticker": search_ticker,
                "company_en": clean,
                "company_ko": clean,
                "market": mkey,
                "auto_selected": True,
                "rs_score": r["rs_score"],
                "rank": r["rank"],
                "selected_at": datetime.now(timezone.utc).date().isoformat(),
                "conditions": (old.get("conditions") if old else None) or blank_conditions(),
            }
            # 사용자가 직접 넣은 회사명 표기만 존중한다.
            # 이전 실행이 자동으로 채운 이름(auto_selected=True)까지 보존하면
            # 이름 정리 로직을 고쳐도 옛 값이 계속 남는다.
            if old and old.get("auto_selected") is not True:
                for k in ("company_ko", "company_en"):
                    if (old.get(k) or "").strip():
                        cand[k] = old[k]
            new_list.append(cand)

    # 후보에서 빠졌지만 사용자가 입력해둔 종목은 보관함으로
    new_archived = []
    for c in list(prev) + list(archived):
        t = (c.get("ticker") or "").strip()
        if not t or t in picked:
            continue
        if has_user_input(c):
            c = dict(c)
            c["archived_at"] = datetime.now(timezone.utc).date().isoformat()
            c["auto_selected"] = False
            if not any((a.get("ticker") or "") == t for a in new_archived):
                new_archived.append(c)

    manual["small_wave_candidates"] = new_list
    manual["archived_candidates"] = new_archived
    manual["_archived_note_ko"] = ("후보에서 탈락했지만 조건 체크가 입력되어 있던 종목입니다. "
                                   "다시 주도주로 선정되면 입력값이 자동 복원됩니다.")
    manual["updated_at"] = datetime.now(timezone.utc).isoformat()

    with open(MANUAL_PATH, "w", encoding="utf-8") as f:
        json.dump(manual, f, ensure_ascii=False, indent=2)
    return len(new_list), len(new_archived)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="manual_inputs.json 을 건드리지 않는다")
    ap.add_argument("--market", choices=["US", "KR"], help="한쪽 시장만 처리")
    ap.add_argument("--no-cache", action="store_true", help="같은 날 캐시를 무시하고 다시 받는다")
    args = ap.parse_args()

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    lc = cfg.get("leaders")
    if not lc or not lc.get("enabled"):
        sys.exit("scoring_config.json 의 leaders.enabled 가 꺼져 있습니다.")

    dcfg = lc.get("download", {})
    markets = {args.market: lc["markets"][args.market]} if args.market else lc["markets"]
    results, meta_errors, per_market_meta = {}, {}, {}

    for mkey, mcfg in markets.items():
        log("\n[%s] %s 주도주 선정" % (mkey, mcfg["label_ko"]))
        if mkey == "US":
            uni, errs = universe_us(mcfg)
        else:
            uni, errs = universe_kr(mcfg)
        log("  유니버스 소스: %s → %d종목" % (mcfg["universe_source"], len(uni)))
        if errs:
            log("  유니버스 경고: %s" % "; ".join(errs))
        if not uni:
            meta_errors[mkey] = errs or ["유니버스가 비어 있음"]
            results[mkey] = []
            continue

        # 순위 모집단을 시가총액 상위 N종목으로 제한한다.
        # 제한하지 않으면 초과수익 백분위 특성상 변동성이 큰 소형 테마주가
        # 상위권을 독식해 통상적인 '주도주'와 멀어진다.
        full_n = len(uni)
        cap_n = mcfg.get("rank_universe_top_by_cap")
        if cap_n:
            uni = sorted(uni, key=lambda u: -(u.get("market_cap") or 0))[:cap_n]
            log("  순위 모집단: 시가총액 상위 %d종목으로 제한 (전체 %d종목 중)" % (len(uni), full_n))

        rows, errs2, as_of, dropped, coverage = score_market(
            mkey, mcfg, lc["momentum"], uni, lc.get("price_period", "2y"),
            dcfg, use_cache=not args.no_cache)
        if errs2:
            meta_errors[mkey] = errs2

        min_cov = dcfg.get("min_universe_coverage", 0.85)
        reliable = coverage >= min_cov
        top = rows[:lc["top_n_per_market"]] if reliable else []

        per_market_meta[mkey] = {
            "universe_size": len(uni), "universe_full_size": full_n,
            "rank_universe_top_by_cap": cap_n, "passed_filters": len(rows),
            "as_of": as_of, "benchmark": mcfg["benchmark"],
            "dropped": dropped, "universe_source": mcfg["universe_source"],
            "universe_url": mcfg["universe_url"],
            "price_coverage": round(coverage, 4),
            "min_required_coverage": min_cov,
            "reliable": reliable,
        }
        if not reliable:
            # 유니버스가 훼손된 상태에서 만든 순위는 '주도주'가 아니라
            # '레이트리밋을 피해 살아남은 종목'이다. 발표하지 않는다.
            msg = ("가격 수신률 %.1f%% 가 기준 %.0f%% 에 미달 — 순위를 발표하지 않습니다. "
                   "유니버스가 훼손된 상태의 순위는 주도주가 아니라 '데이터를 받은 종목'일 뿐입니다."
                   % (coverage * 100, min_cov * 100))
            per_market_meta[mkey]["withheld_reason_ko"] = msg
            meta_errors.setdefault(mkey, []).append(msg)
            log("  [보류] " + msg)
        results[mkey] = top
        for r in top:
            log("    #%-2d %-12s RS %-5s  %s" % (r["rank"], r["symbol"], r["rs_score"], r["name"][:34]))

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_version": cfg["_meta"]["config_version"],
        "method": {
            "definition_ko": "벤치마크 대비 초과수익 기반 상대강도(RS) 모멘텀 상위 종목",
            "weights": lc["momentum"]["weights"],
            "lookbacks": lc["momentum"]["lookback_trading_days"],
            "filters": lc["momentum"]["filters"],
            "excess_over_benchmark": lc["momentum"]["excess_over_benchmark"],
            "price_source": "yfinance (Yahoo Finance)",
            "note_ko": lc["note_ko"],
            "disclaimer_ko": lc["disclaimer_ko"],
        },
        "markets": per_market_meta,
        "errors": meta_errors,
        "leaders": results,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log("\nleaders.json 생성 완료")

    if args.dry_run:
        log("--dry-run: manual_inputs.json 은 수정하지 않았습니다.")
        return
    if not any(results.values()):
        log("모든 시장이 보류되어 manual_inputs.json 을 수정하지 않았습니다.")
        return
    if lc["output"].get("also_update_manual_inputs"):
        n, a = update_manual_inputs(results, lc["output"])
        log("manual_inputs.json 갱신: 후보 %d종목, 보관 %d종목" % (n, a))
        log("다음 단계: .venv/bin/python fetch_news.py  (후보별 조건 뉴스 수집)")


if __name__ == "__main__":
    main()
