#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_data.py — 3-Wave Dashboard 수치 데이터 수집기

원칙:
  * 가져오지 못한 값은 절대 추정하지 않는다. value=None, status="no_data"로 남긴다.
  * 모든 값에 source / series_id / as_of / fetched_at 를 붙인다.
  * API 키가 없으면 FRED 공개 CSV 엔드포인트(fredgraph.csv)로 폴백하고, 어떤 경로를 썼는지 기록한다.

출력: data.json
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "scoring_config.json")
OUT_PATH = os.path.join(HERE, "data.json")

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
# 주의: fred.stlouisfed.org 는 브라우저를 사칭한 User-Agent 로 오는 요청을 응답 없이 끊는다
# (Mozilla/... 로 보내면 연결이 타임아웃됨). urllib 기본 UA 를 그대로 쓰는 것이 정상 동작한다.
FRED_HEADERS = {}
TIMEOUT = 30

SSL_CTX = ssl.create_default_context()

# FRED 시리즈 단위. billions of USD 로 정규화하기 위한 배수.
SERIES_UNITS = {
    "WALCL":     {"native": "Millions of USD", "to_bn": 0.001},
    "WRESBAL":   {"native": "Millions of USD", "to_bn": 0.001},
    "WTREGEN":   {"native": "Millions of USD", "to_bn": 0.001},
    "RRPONTSYD": {"native": "Billions of USD", "to_bn": 1.0},
}

SERIES_NEEDED = [
    "CPIAUCSL", "CPILFESL", "UNRATE",
    "DFEDTARU", "DFEDTAR", "FEDFUNDS",
    "WALCL", "WRESBAL", "RRPONTSYD", "WTREGEN",
]


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def http_get(url, headers=None, timeout=TIMEOUT, retries=2):
    """headers=None 이면 urllib 기본 User-Agent 를 사용한다 (FRED 가 요구하는 동작)."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last


# ----------------------------------------------------------------------------
# FRED
# ----------------------------------------------------------------------------
def fred_via_api(series_id):
    """공식 FRED API. FRED_API_KEY 필요."""
    q = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
    })
    raw = http_get("https://api.stlouisfed.org/fred/series/observations?" + q, headers=FRED_HEADERS)
    payload = json.loads(raw.decode("utf-8"))
    out = []
    for obs in payload.get("observations", []):
        if obs.get("value") in (".", "", None):
            continue
        try:
            out.append((obs["date"], float(obs["value"])))
        except (ValueError, KeyError):
            continue
    return out, "fred_api"


def fred_via_csv(series_id):
    """키가 필요 없는 FRED 공개 CSV 다운로드 엔드포인트."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + urllib.parse.quote(series_id)
    text = http_get(url, headers=FRED_HEADERS).decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("빈 CSV 응답")
    out = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        date, val = parts[0].strip(), parts[1].strip()
        if val in (".", "", "NA"):
            continue
        try:
            out.append((date, float(val)))
        except ValueError:
            continue
    if not out:
        raise ValueError("CSV에서 유효한 관측치를 찾지 못함")
    return out, "fredgraph_csv"


def fetch_series(series_id):
    """(observations, transport, error) 를 반환. 실패 시 observations=None."""
    attempts = []
    if FRED_API_KEY:
        attempts.append(("fred_api", fred_via_api))
    attempts.append(("fredgraph_csv", fred_via_csv))

    errors = []
    for name, fn in attempts:
        try:
            obs, transport = fn(series_id)
            if obs:
                return obs, transport, None
            errors.append("%s: 관측치 0건" % name)
        except urllib.error.HTTPError as e:
            errors.append("%s: HTTP %s" % (name, e.code))
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (name, e))
    return None, None, "; ".join(errors)


# ----------------------------------------------------------------------------
# 지표 계산 헬퍼
# ----------------------------------------------------------------------------
def last(obs):
    return obs[-1] if obs else (None, None)


def value_n_ago(obs, n):
    """n개 관측치 이전 값. 없으면 None."""
    if obs is None or len(obs) <= n:
        return None
    return obs[-1 - n][1]


def pct_change(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    return (cur / prev - 1.0) * 100.0


def rnd(x, n=4):
    return None if x is None else round(x, n)


def align_weekly(series_map, keys):
    """주간 시리즈들을 공통 날짜로 정렬. [(date, {key: value}), ...] 반환."""
    dsets = []
    for k in keys:
        obs = series_map.get(k)
        if not obs:
            return []
        dsets.append({d: v for d, v in obs})
    common = set(dsets[0])
    for d in dsets[1:]:
        common &= set(d)
    out = []
    for date in sorted(common):
        out.append((date, {k: dsets[i][date] for i, k in enumerate(keys)}))
    return out


def nearest_on_or_before(obs, target_date):
    """target_date(YYYY-MM-DD) 이하의 가장 최근 관측치 값."""
    best = None
    for d, v in obs:
        if d <= target_date:
            best = v
        else:
            break
    return best


def month_key(date_str):
    return date_str[:7]


def month_offset_key(date_str, months):
    y, m = int(date_str[:4]), int(date_str[5:7])
    total = (y * 12 + (m - 1)) - months
    return "%04d-%02d" % (total // 12, total % 12 + 1)


def value_months_ago(obs, as_of, months):
    """as_of 기준 정확히 N개월 전 관측치. 해당 월이 없으면 None (추정하지 않는다)."""
    want = month_offset_key(as_of, months)
    for d, v in obs:
        if month_key(d) == want:
            return v
    return None


def value_weeks_ago(obs, as_of, weeks):
    """as_of 기준 N주 전 날짜 이하의 가장 최근 관측치."""
    from datetime import date as _date
    y, m, d = (int(x) for x in as_of.split("-"))
    target = _date(y, m, d).toordinal() - 7 * weeks
    target_str = _date.fromordinal(target).isoformat()
    return nearest_on_or_before(obs, target_str)


def months_ago_str(date_str, months):
    y, m, d = (int(p) for p in date_str.split("-"))
    total = (y * 12 + (m - 1)) - months
    return "%04d-%02d-%02d" % (total // 12, total % 12 + 1, d)


HISTORY_YEARS = 20
MAX_HISTORY_POINTS = 1100


def spark(obs, n=None, scale=1.0):
    """차트용 이력. 기간 토글(5년/3년/1년/6개월/3개월)을 위해 5년치를 담는다.

    일별 시리즈는 5년이면 1,300개가 넘으므로 균등 솎아내기로 400개 이하로 줄인다.
    가장 최근 관측치는 항상 남긴다.
    """
    if not obs:
        return []
    cutoff = months_ago_str(obs[-1][0], HISTORY_YEARS * 12)
    window = [(d, v) for d, v in obs if d >= cutoff]
    if n:
        window = window[-n:]
    step = max(1, (len(window) + MAX_HISTORY_POINTS - 1) // MAX_HISTORY_POINTS)
    if step > 1:
        thinned = window[::step]
        if thinned[-1][0] != window[-1][0]:
            thinned.append(window[-1])
        window = thinned
    return [{"d": d, "v": round(v * scale, 4)} for d, v in window]


# ----------------------------------------------------------------------------
# 지표별 metric 계산
# ----------------------------------------------------------------------------
def build_indicator(key, value, unit, as_of, metrics, history, source, prev_value=None,
                    status="ok", status_reason=None, transport=None, extra=None):
    return {
        "key": key,
        "status": status,
        "status_reason": status_reason,
        "value": rnd(value),
        "prev_value": rnd(prev_value),
        "change": rnd(None if (value is None or prev_value is None) else value - prev_value),
        "unit": unit,
        "as_of": as_of,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "metrics": {k: rnd(v) for k, v in (metrics or {}).items()},
        "history": history or [],
        "source": source,
        "transport": transport,
        "extra": extra or {},
    }


def missing(key, reason, source):
    return build_indicator(key, None, None, None, {}, [], source,
                           status="no_data", status_reason=reason)


# ----------------------------------------------------------------------------
# metric 계산 — 인자로 받은 시리즈의 마지막 관측치를 "기준 시점"으로 본다.
# 과거 시점을 보려면 그 시점까지 잘라낸 시리즈를 넘기면 된다.
# 라이브 값과 이력이 같은 함수를 쓰므로 둘이 어긋날 수 없다.
# ----------------------------------------------------------------------------
def m_cpi(obs):
    if not obs or len(obs) < 14:
        return {}
    as_of, level = obs[-1]
    yoy = pct_change(level, value_months_ago(obs, as_of, 12))
    m3 = value_months_ago(obs, as_of, 3)
    ann3m = ((level / m3) ** 4 - 1.0) * 100.0 if m3 else None
    return {"yoy_pct": yoy, "ann3m_pct": ann3m,
            "accel_pp": None if (ann3m is None or yoy is None) else ann3m - yoy,
            "index_level": level}


def m_unrate(obs):
    if not obs or len(obs) < 16:
        return {}
    as_of, level = obs[-1]
    bymonth = {month_key(d): v for d, v in obs}
    win12 = [bymonth.get(month_offset_key(as_of, i)) for i in range(12)]
    vals12 = [v for v in win12 if v is not None]
    low_12m = min(vals12) if vals12 else None

    def avg3_at(off):
        vs = [bymonth.get(month_offset_key(as_of, off + j)) for j in range(3)]
        return None if any(v is None for v in vs) else sum(vs) / 3.0

    cur3 = avg3_at(0)
    prior = [a for a in (avg3_at(i) for i in range(0, 13)) if a is not None]
    sahm = (cur3 - min(prior)) if (cur3 is not None and prior) else None
    y_ago = bymonth.get(month_offset_key(as_of, 12))
    return {"level_pct": level, "low_12m_pct": low_12m,
            "rise_from_12m_low_pp": None if low_12m is None else level - low_12m,
            "chg_12m_pp": None if y_ago is None else level - y_ago,
            "sahm_3m_avg_pct": cur3, "sahm_gap_pp": sahm}


def m_fed_stance(obs):
    if not obs:
        return {}
    as_of, level = obs[-1]
    six_m = nearest_on_or_before(obs, months_ago_str(as_of, 6))
    return {"level_pct": level, "level_6m_ago_pct": six_m,
            "change_6m_pp": None if six_m is None else level - six_m}


def m_weekly(obs, scale=1.0, need_52w=False):
    if not obs or len(obs) < 14:
        return {}
    as_of = obs[-1][0]
    level = obs[-1][1] * scale
    b4 = value_weeks_ago(obs, as_of, 4)
    b13 = value_weeks_ago(obs, as_of, 13)
    b52 = value_weeks_ago(obs, as_of, 52) if need_52w else None
    b4 = None if b4 is None else b4 * scale
    b13 = None if b13 is None else b13 * scale
    b52 = None if b52 is None else b52 * scale
    out = {"level_usd_bn": level,
           "chg_4w_pct": pct_change(level, b4),
           "chg_13w_pct": pct_change(level, b13),
           "chg_4w_usd_bn": None if b4 is None else level - b4,
           "chg_13w_usd_bn": None if b13 is None else level - b13}
    if need_52w:
        out["chg_52w_pct"] = pct_change(level, b52)
        out["chg_52w_usd_bn"] = None if b52 is None else level - b52
    return out


# DFEDTARU(목표범위 상단)는 2008-12-16 부터만 존재한다. 그 이전에는 단일 목표금리
# DFEDTAR 를 썼다. 20년 구간에서 2007-2008 금리 인하 국면이 통째로 비면 안 되므로
# 두 시리즈를 이어 붙여 하나의 정책금리로 쓴다.
POLICY_SPLICE_DATE = "2008-12-16"


def policy_rate_series(sm):
    new_obs = sm.get("DFEDTARU") or []
    old_obs = sm.get("DFEDTAR") or []
    if not new_obs:
        return old_obs
    if not old_obs:
        return new_obs
    return [(d, v) for d, v in old_obs if d < POLICY_SPLICE_DATE] + list(new_obs)


def calc_cpi(sm, errs, src):
    obs = sm.get("CPIAUCSL")
    if not obs or len(obs) < 14:
        return missing("cpi", errs.get("CPIAUCSL") or "CPIAUCSL 관측치 부족", src)
    as_of, level = obs[-1]
    met = m_cpi(obs)
    yoy, ann3m = met["yoy_pct"], met["ann3m_pct"]
    y_ago = value_months_ago(obs, as_of, 12)
    m3 = value_months_ago(obs, as_of, 3)
    prev_as_of = obs[-2][0]
    prev_yoy = pct_change(obs[-2][1], value_months_ago(obs, prev_as_of, 12))

    core = sm.get("CPILFESL")
    core_yoy = None
    core_as_of = None
    if core and len(core) > 12:
        core_as_of = core[-1][0]
        core_yoy = pct_change(core[-1][1], value_months_ago(core, core_as_of, 12))

    yoy_hist = []
    cpi_cutoff = months_ago_str(as_of, HISTORY_YEARS * 12)
    for d, v in obs:
        if d < cpi_cutoff:
            continue
        p = pct_change(v, value_months_ago(obs, d, 12))
        if p is not None:
            yoy_hist.append({"d": d, "v": round(p, 3)})

    return build_indicator(
        "cpi", yoy, "%", as_of,
        dict(met, core_yoy_pct=core_yoy),
        yoy_hist, src["cpi"], prev_value=prev_yoy, transport=sm["_transport"].get("CPIAUCSL"),
        extra={"core_series_id": "CPILFESL", "core_as_of": core_as_of,
               "core_yoy_pct": rnd(core_yoy), "index_as_of": as_of,
               "yoy_base_month": month_offset_key(as_of, 12),
               "yoy_base_value": rnd(y_ago),
               "ann3m_base_month": month_offset_key(as_of, 3),
               "ann3m_base_value": rnd(m3),
               "calc_note_ko": "YoY = (최신월 / 12개월 전 같은 달 − 1) × 100. "
                               "3개월 연율 = (최신월 / 3개월 전)^4 − 1. "
                               "해당 월이 결측이면 계산하지 않고 null 로 둡니다."},
    )


def calc_unrate(sm, errs, src):
    obs = sm.get("UNRATE")
    if not obs or len(obs) < 16:
        return missing("unemployment", errs.get("UNRATE") or "UNRATE 관측치 부족", src)
    as_of, level = obs[-1]
    met = m_unrate(obs)
    bymonth = {month_key(d): v for d, v in obs}
    missing_12m = [month_offset_key(as_of, i) for i in range(12)
                   if bymonth.get(month_offset_key(as_of, i)) is None]

    def avg3_at(offset):
        vs = [bymonth.get(month_offset_key(as_of, offset + j)) for j in range(3)]
        return None if any(v is None for v in vs) else sum(vs) / 3.0

    sahm = met["sahm_gap_pp"]
    skipped_windows = 13 - len([a for a in (avg3_at(i) for i in range(0, 13)) if a is not None])

    return build_indicator(
        "unemployment", level, "%", as_of,
        met,
        spark(obs), src["unemployment"], prev_value=obs[-2][1],
        transport=sm["_transport"].get("UNRATE"),
        extra={"sahm_trigger_threshold_pp": 0.50,
               "sahm_triggered": None if sahm is None else bool(sahm >= 0.50),
               "missing_months_in_12m_window": missing_12m,
               "sahm_windows_skipped_for_missing_data": skipped_windows,
               "calc_note_ko": "결측월이 포함된 3개월 창은 계산에서 제외합니다. "
                               "Sahm 갭 = 최근 3개월 평균 − 직전 12개월 내 3개월 이동평균 최저치."},
    )


def calc_fed_stance(sm, errs, src):
    obs = policy_rate_series(sm)
    if not obs:
        return missing("fed_stance", errs.get("DFEDTARU") or "정책금리 관측치 없음", src)
    as_of, level = obs[-1]
    met = m_fed_stance(obs)
    six_m = met["level_6m_ago_pct"]
    effr = sm.get("FEDFUNDS")
    effr_val, effr_as_of = (effr[-1][1], effr[-1][0]) if effr else (None, None)

    return build_indicator(
        "fed_stance", level, "%", as_of,
        met,
        spark(obs), src["fed_stance"],
        prev_value=six_m, transport=sm["_transport"].get("DFEDTARU"),
        extra={"effr_series_id": "FEDFUNDS", "effr_pct": rnd(effr_val), "effr_as_of": effr_as_of,
               "six_months_ago_date": months_ago_str(as_of, 6),
               "spliced_series_ko": ("2008-12-16 이전은 단일 목표금리 DFEDTAR, 이후는 "
                                     "목표범위 상단 DFEDTARU 를 이어 붙였습니다.")},
    )


def calc_weekly_level(key, series_id, sm, errs, src, need_52w=False):
    obs = sm.get(series_id)
    if not obs or len(obs) < 14:
        return missing(key, errs.get(series_id) or ("%s 관측치 부족" % series_id), src)
    scale = SERIES_UNITS.get(series_id, {}).get("to_bn", 1.0)
    as_of = obs[-1][0]
    level = obs[-1][1] * scale
    prev = obs[-2][1] * scale
    v13 = value_weeks_ago(obs, as_of, 13)
    v4 = value_weeks_ago(obs, as_of, 4)
    v52 = value_weeks_ago(obs, as_of, 52) if need_52w else None
    b4 = None if v4 is None else v4 * scale
    b13 = None if v13 is None else v13 * scale
    b52 = None if v52 is None else v52 * scale
    metrics = {
        "level_usd_bn": level,
        "chg_4w_pct": pct_change(level, b4),
        "chg_13w_pct": pct_change(level, b13),
        # 잔고가 0 근처인 시리즈(RRP)는 퍼센트 변화가 극단적으로 튀어 해석이 안 된다.
        # 채점에는 단위가 살아 있는 절대 증감액($B)을 쓴다.
        "chg_4w_usd_bn": None if b4 is None else level - b4,
        "chg_13w_usd_bn": None if b13 is None else level - b13,
    }
    if need_52w:
        metrics["chg_52w_pct"] = pct_change(level, b52)
        metrics["chg_52w_usd_bn"] = None if b52 is None else level - b52
    return build_indicator(
        key, level, "$B", as_of, metrics, spark(obs, None, scale), src[key],
        prev_value=prev, transport=sm["_transport"].get(series_id),
        extra={"native_unit": SERIES_UNITS.get(series_id, {}).get("native", "unknown"),
               "normalized_to": "Billions of USD",
               "base_4w": rnd(None if v4 is None else v4 * scale),
               "base_13w": rnd(None if v13 is None else v13 * scale),
               "base_52w": rnd(None if v52 is None else v52 * scale),
               "calc_note_ko": "N주 전 기준일 이하의 가장 최근 관측치를 기준값으로 사용합니다."},
    )


def calc_net_liquidity(sm, errs, src):
    need = ["WALCL", "WTREGEN", "RRPONTSYD"]
    miss = [s for s in need if not sm.get(s)]
    if miss:
        reason = "구성 시리즈 결측: " + ", ".join("%s(%s)" % (s, errs.get(s, "사유 미상")) for s in miss)
        return missing("net_liquidity", reason, src)

    # WALCL/WTREGEN 은 주간(수요일 기준), RRPONTSYD 는 일별 → 주간 날짜에 맞춰 조회
    walcl = sm["WALCL"]
    tga = {d: v for d, v in sm["WTREGEN"]}
    rrp = sm["RRPONTSYD"]

    # 차트 기간 토글(최대 5년)을 지원하려면 5년치보다 넉넉히 계산해야 한다.
    hist_cutoff = months_ago_str(walcl[-1][0], (HISTORY_YEARS + 1) * 12)
    pts = []
    for d, v in walcl:
        if d < hist_cutoff:
            continue
        if d not in tga:
            continue
        r = nearest_on_or_before(rrp, d)
        if r is None:
            continue
        # billions 로 정규화: WALCL/TGA 는 millions, RRP 는 이미 billions
        pts.append((d, v * 0.001 - tga[d] * 0.001 - r * 1.0))

    if len(pts) < 14:
        return missing("net_liquidity", "공통 날짜로 정렬 가능한 관측치가 14주 미만", src)

    as_of, level = pts[-1]
    prev = pts[-2][1]
    return build_indicator(
        "net_liquidity", level, "$B", as_of, m_weekly(pts, 1.0),
        spark(pts), src["net_liquidity"],
        prev_value=prev, transport="derived",
        extra={"formula": "WALCL(M)/1000 - WTREGEN(M)/1000 - RRPONTSYD(B)",
               "component_as_of": {"WALCL": sm["WALCL"][-1][0], "WTREGEN": sm["WTREGEN"][-1][0],
                                   "RRPONTSYD": sm["RRPONTSYD"][-1][0]},
               "unit_note": "WALCL/WTREGEN 은 백만 달러, RRPONTSYD 는 십억 달러 단위이므로 정규화 후 계산"},
    )


# ----------------------------------------------------------------------------
# 종합점수 시계열용 metric 이력
# ----------------------------------------------------------------------------
def _shift_days(date_str, days):
    from datetime import date as _d
    y, m, dd = (int(x) for x in date_str.split("-"))
    return _d.fromordinal(_d(y, m, dd).toordinal() - days).isoformat()


def _upto(obs, cutoff):
    """cutoff 이하의 관측치만 남긴 시리즈."""
    if not obs:
        return []
    out = []
    for d, v in obs:
        if d > cutoff:
            break
        out.append((d, v))
    return out


def build_metrics_history(sm, cfg):
    """과거 각 시점의 지표 metric 을 다시 계산한다.

    점수화는 하지 않는다 — 임계값 적용은 화면(JS)이 라이브 값과 똑같은 엔진으로 처리해야
    둘이 어긋나지 않기 때문이다. 여기서는 metric 값만 시점별로 만들어 넘긴다.
    """
    hcfg = cfg.get("composite_history") or {}
    if not hcfg.get("enabled", True):
        return None
    walcl = sm.get("WALCL")
    if not walcl:
        return None

    lag = hcfg.get("publication_lag_days", {})
    years = hcfg.get("years", HISTORY_YEARS)
    start = months_ago_str(walcl[-1][0], years * 12)
    grid = [d for d, _ in walcl if d >= start]

    out = {k: {} for k in ("cpi", "unemployment", "fed_stance", "fed_balance_sheet",
                           "reserves", "net_liquidity", "tga", "rrp")}

    def put(key, idx, met, n):
        for mk, mv in (met or {}).items():
            arr = out[key].setdefault(mk, [None] * n)
            arr[idx] = rnd(mv)

    n = len(grid)
    for i, t in enumerate(grid):
        def cut(sid):
            return _upto(sm.get(sid), _shift_days(t, lag.get(sid, 3)))

        put("cpi", i, m_cpi(cut("CPIAUCSL")), n)
        put("unemployment", i, m_unrate(cut("UNRATE")), n)
        pol = policy_rate_series(sm)
        put("fed_stance", i, m_fed_stance(
            _upto(pol, _shift_days(t, lag.get("DFEDTARU", 2)))), n)
        put("fed_balance_sheet", i, m_weekly(cut("WALCL"), 0.001, True), n)
        put("reserves", i, m_weekly(cut("WRESBAL"), 0.001), n)
        put("tga", i, m_weekly(cut("WTREGEN"), 0.001), n)
        put("rrp", i, m_weekly(cut("RRPONTSYD"), 1.0), n)

        w, g, r = cut("WALCL"), cut("WTREGEN"), cut("RRPONTSYD")
        if w and g and r:
            tg = {d: v for d, v in g}
            pts = []
            for d, v in w:
                if d not in tg:
                    continue
                rr = nearest_on_or_before(r, d)
                if rr is None:
                    continue
                pts.append((d, v * 0.001 - tg[d] * 0.001 - rr))
            put("net_liquidity", i, m_weekly(pts, 1.0), n)

    return {
        "dates": grid,
        "indicators": out,
        "publication_lag_days": lag,
        "publication_lag_note_ko": hcfg.get("publication_lag_note_ko"),
        "caveats_ko": hcfg.get("caveats_ko", []),
        "method_ko": ("각 시점까지 잘라낸 원자료로 지표를 다시 계산합니다. "
                      "라이브 값과 동일한 계산 함수를 씁니다."),
    }


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    src = {i["key"]: i["source"] for i in cfg["indicators"]}

    print("FRED 수집 경로: %s" % ("공식 API (FRED_API_KEY 감지됨)" if FRED_API_KEY
                                 else "공개 CSV 엔드포인트 fredgraph.csv (API 키 없음 — 키 불필요 경로)"))

    sm = {"_transport": {}}
    errs = {}
    for sid in SERIES_NEEDED:
        obs, transport, err = fetch_series(sid)
        if obs:
            sm[sid] = obs
            sm["_transport"][sid] = transport
            print("  [OK]   %-10s %5d obs  latest=%s  via %s" % (sid, len(obs), obs[-1][0], transport))
        else:
            sm[sid] = None
            errs[sid] = err or "알 수 없는 오류"
            print("  [FAIL] %-10s %s" % (sid, errs[sid]), file=sys.stderr)

    indicators = {
        "cpi": calc_cpi(sm, errs, src),
        "unemployment": calc_unrate(sm, errs, src),
        "fed_stance": calc_fed_stance(sm, errs, src),
        "fed_balance_sheet": calc_weekly_level("fed_balance_sheet", "WALCL", sm, errs, src, need_52w=True),
        "reserves": calc_weekly_level("reserves", "WRESBAL", sm, errs, src),
        "net_liquidity": calc_net_liquidity(sm, errs, src),
        "tga": calc_weekly_level("tga", "WTREGEN", sm, errs, src),
        "rrp": calc_weekly_level("rrp", "RRPONTSYD", sm, errs, src),
    }

    mh = build_metrics_history(sm, cfg)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics_history": mh,
        "config_version": cfg["_meta"]["config_version"],
        "fetch_environment": {
            "fred_api_key_present": bool(FRED_API_KEY),
            "fred_transport": "fred_api" if FRED_API_KEY else "fredgraph_csv",
            "python": sys.version.split()[0],
        },
        "series_errors": errs,
        "indicators": indicators,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    ok = sum(1 for v in indicators.values() if v["status"] == "ok")
    print("\ndata.json 생성 완료 — 지표 %d개 중 %d개 수집 성공" % (len(indicators), ok))
    for k, v in indicators.items():
        print("  %-18s %-8s %s" % (k, v["status"], v.get("as_of") or (v.get("status_reason") or "")[:60]))


if __name__ == "__main__":
    main()
