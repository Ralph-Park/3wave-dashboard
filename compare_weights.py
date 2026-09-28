#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare_weights.py — 가중치안을 20년/5년 두 구간에서 동시에 평가 (연구용)

한 구간에서만 좋은 가중치는 그 국면에 맞춘 것일 뿐이다.
두 구간 모두에서 무너지지 않는 안을 고르기 위한 비교 도구.
"""
import json, os, sys, warnings
warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import pandas as pd, yfinance as yf
except ImportError as e:
    sys.exit("가상환경에서 실행하세요 (%s)" % e)

sys.path.insert(0, HERE)
import importlib.util
spec = importlib.util.spec_from_file_location("ll", os.path.join(HERE, "analyze_leadlag.py"))
ll = importlib.util.module_from_spec(spec); spec.loader.exec_module(ll)

cfg = json.load(open(os.path.join(HERE, "scoring_config.json"), encoding="utf-8"))
data = json.load(open(os.path.join(HERE, "data.json"), encoding="utf-8"))
mh = data["metrics_history"]; dates = mh["dates"]
scored = [i for i in cfg["indicators"] if i["role"] == "scored"]

# 실업률 '수준' 두 방향을 모두 평가
LEVEL_CONTRARIAN = [{"max":3.6,"score":-1},{"max":4.4,"score":0},{"max":6.5,"score":1},{"max":None,"score":0}]
LEVEL_HUMP       = [{"max":3.6,"score":0},{"max":4.4,"score":1},{"max":5.2,"score":0},{"max":6.0,"score":-1},{"max":None,"score":-2}]

SCHEMES = {
    "S1 균등":        {k: 1 for k in [i["key"] for i in scored]},
    "S2 현행(5년기반)": {"cpi":.65,"unemployment":.35,"reserves":.24,"net_liquidity":.24,
                      "rrp":.24,"fed_stance":.12,"tga":.08,"fed_balance_sheet":.08},
    "S3 CPI집중":     {"cpi":.80,"unemployment":.20,"reserves":.15,"net_liquidity":.15,
                      "rrp":.30,"fed_stance":.15,"tga":.10,"fed_balance_sheet":.15},
    "S4 완만한틸트":    {"cpi":.60,"unemployment":.40,"reserves":.18,"net_liquidity":.18,
                      "rrp":.22,"fed_stance":.16,"tga":.12,"fed_balance_sheet":.14},
    # 20년 측정 반영: RRP 만 유동성 계열에서 일관되게 양(+), 지급준비금·순유동성은 0 근처
    "S5 RRP중심":     {"cpi":.65,"unemployment":.35,"rrp":.32,"reserves":.16,"net_liquidity":.16,
                      "fed_stance":.16,"tga":.10,"fed_balance_sheet":.10},
    "S6 순유동성유지":   {"cpi":.65,"unemployment":.35,"rrp":.28,"net_liquidity":.24,"reserves":.14,
                      "fed_stance":.14,"tga":.10,"fed_balance_sheet":.10},
}

def scores_for(level_bps):
    out = {}
    for ind in scored:
        mm = mh["indicators"].get(ind["key"]) or {}
        comps = []
        for c in (ind.get("scoring") or {}).get("components", []):
            c = dict(c)
            if ind["key"] == "unemployment" and c["metric"] == "level_pct":
                c["breakpoints"] = level_bps
            comps.append(c)
        row = []
        for idx in range(len(dates)):
            acc = w = 0.0
            for c in comps:
                v = (mm.get(c["metric"]) or [None]*len(dates))[idx]
                sc = ll.apply_breakpoints(v, c["breakpoints"])
                if sc is None: continue
                acc += sc*c["weight"]; w += c["weight"]
            row.append(None if w == 0 else max(-2, min(2, int(round(acc/w)))))
        out[ind["key"]] = row
    return out

def composite(sc, share):
    by_wave = {}
    for i in scored: by_wave.setdefault(i["wave"], []).append(i)
    base = {}
    for wave, lst in by_wave.items():
        ww = cfg["wave_weights"].get(wave, 0)
        tot = sum(share.get(i["key"], 1) for i in lst) or 1
        for i in lst: base[i["key"]] = ww*share.get(i["key"], 1)/tot
    out = []
    for idx in range(len(dates)):
        acc = w = 0.0
        for i in scored:
            s = sc[i["key"]][idx]
            if s is None or base[i["key"]] <= 0: continue
            acc += s*base[i["key"]]; w += base[i["key"]]
        out.append(None if w == 0 else ((acc/w)+2)/4*100)
    return out

px = yf.download("SPY", start="2005-01-01", interval="1d", auto_adjust=True, progress=False)["Close"]
if isinstance(px, pd.DataFrame): px = px.iloc[:, 0]
px = px.dropna(); idx_dates = [d.date().isoformat() for d in px.index]

def fwd(ds, n):
    lo, hi = 0, len(idx_dates)
    while lo < hi:
        mid = (lo+hi)//2
        if idx_dates[mid] < ds: lo = mid+1
        else: hi = mid
    if lo+n >= len(px): return None
    a, b = float(px.iloc[lo]), float(px.iloc[lo+n])
    return None if a <= 0 else (b/a-1)*100

def evaluate(comp, start):
    xs, ys = [], []
    for i, d in enumerate(dates):
        if d < start or comp[i] is None: continue
        f = fwd(d, 126)
        if f is None: continue
        xs.append(comp[i]); ys.append(f)
    rho = ll.spearman(xs, ys)
    # 구간 단조성: 4분위 평균이 계속 증가하는가
    pairs = sorted(zip(xs, ys))
    q = len(pairs)//4
    means = [sum(y for _, y in pairs[i*q:(i+1)*q])/max(q,1) for i in range(4)] if q else []
    mono = all(means[i] <= means[i+1] for i in range(len(means)-1)) if means else False
    return rho, means, mono, len(xs)

print("SPY 이후 6개월 수익률 기준 · 순위상관 / 4분위 평균 / 단조성\n")
for lname, lbps in [("수준=언덕(저실업 +)", LEVEL_HUMP), ("수준=역방향(고실업 +)", LEVEL_CONTRARIAN)]:
    sc = scores_for(lbps)
    print("=" * 92)
    print("실업률 '수준' 정의: %s" % lname)
    print("%-16s%-34s%-34s" % ("가중치안", "20년 (2006~)", "최근 5년 (2021~)"))
    for sname, share in SCHEMES.items():
        comp = composite(sc, share)
        r20, m20, mo20, n20 = evaluate(comp, "2006-01-01")
        r5,  m5,  mo5,  n5  = evaluate(comp, "2021-09-01")
        f = lambda r, m, mo, n: "ρ=%+.2f %s [%s]" % (
            r or 0, " ".join("%+.0f" % x for x in m), "단조" if mo else "비단조")
        print("%-16s%-34s%-34s" % (sname, f(r20,m20,mo20,n20), f(r5,m5,mo5,n5)))
    print()
