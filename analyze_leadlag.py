#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_leadlag.py — 지표 점수와 '이후' 시장 수익률의 선행 관계 측정 (연구용 스크립트)

배경:
  지표 점수가 낮은 시점과 약세장 시점이 일치할 이유가 없다. 선행지표라면
  점수가 먼저 떨어지고 시장이 나중에 빠진다. 따라서 채점 기준을 손보기 전에
  "이 지표의 점수는 몇 개월 뒤 수익률과 관계가 있는가"를 먼저 재야 한다.

방법:
  metrics_history 의 각 주간 시점에 현재 임계값을 적용해 지표 점수를 만들고,
  그 시점 이후 1/3/6/12개월 벤치마크 수익률과의 관계를 본다.

한계 (반드시 읽을 것):
  * 주간 관측은 겹치는 구간이 대부분이라 독립 표본은 사실상 '연 단위 개수'에 가깝다.
  * 20년 구간에도 침체는 2008, 2020 두 번뿐이다. 하락 국면 표본은 여전히 적다.
  * 여기서 나온 상관계수로 임계값을 미세조정하면 그건 과최적화다.
    방향이 반대로 나오거나 관계가 아예 없는 경우처럼 '큰 신호'에만 반응해야 한다.

실행: .venv/bin/python analyze_leadlag.py
"""
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import pandas as pd
    import yfinance as yf
except ImportError as e:
    sys.exit("가상환경에서 실행하세요: .venv/bin/python analyze_leadlag.py (%s)" % e)

HORIZONS = {"1개월": 21, "3개월": 63, "6개월": 126, "12개월": 252}
BENCHMARKS = {"US": "SPY", "KR": "^KS11"}


def apply_breakpoints(value, bps):
    if value is None:
        return None
    for bp in bps:
        mx = bp.get("max")
        if mx is None or value <= mx:
            return bp["score"]
    return None


def score_indicator(ind, metrics):
    comps = (ind.get("scoring") or {}).get("components") or []
    if not comps:
        return None
    acc = wsum = 0.0
    for c in comps:
        sc = apply_breakpoints(metrics.get(c["metric"]), c["breakpoints"])
        if sc is None:
            continue
        acc += sc * c["weight"]
        wsum += c["weight"]
    if wsum == 0:
        return None
    return max(-2, min(2, int(round(acc / wsum))))


def spearman(xs, ys):
    """순위 상관. scipy 없이 계산."""
    n = len(xs)
    if n < 10:
        return None

    def rank(a):
        order = sorted(range(n), key=lambda i: a[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and a[order[j + 1]] == a[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    return None if dx == 0 or dy == 0 else num / (dx * dy)


def main():
    out_json = {"generated_at": None, "horizons": list(HORIZONS),
                "benchmarks": BENCHMARKS, "markets": {},
                "caveats_ko": [
                    "주간 관측은 구간이 겹쳐 독립 표본이 사실상 '연 단위 개수'에 가깝습니다.",
                    "20년 구간에는 2008 금융위기와 2020 코로나가 포함되지만, 침체는 여전히 2회뿐입니다.",
                    "이 상관계수로 임계값을 미세조정하면 과최적화입니다. 방향이 반대이거나 "
                    "관계가 없는 '큰 신호'에만 반응해야 합니다."]}
    cfg = json.load(open(os.path.join(HERE, "scoring_config.json"), encoding="utf-8"))
    data = json.load(open(os.path.join(HERE, "data.json"), encoding="utf-8"))
    mh = data.get("metrics_history")
    if not mh:
        sys.exit("data.json 에 metrics_history 가 없습니다. fetch_data.py 를 먼저 실행하세요.")

    dates = mh["dates"]
    scored_inds = [i for i in cfg["indicators"] if i["role"] == "scored"]

    # 지표별 시점 점수
    scores = {}
    for ind in scored_inds:
        mm = mh["indicators"].get(ind["key"]) or {}
        row = []
        for idx in range(len(dates)):
            met = {k: v[idx] for k, v in mm.items()}
            row.append(score_indicator(ind, met))
        scores[ind["key"]] = row

    # 종합점수
    by_wave = {}
    for i in scored_inds:
        by_wave.setdefault(i["wave"], []).append(i)
    base = {}
    for wave, lst in by_wave.items():
        wave_w = cfg["wave_weights"].get(wave, 0)
        shares = [(i.get("wave_share") if i.get("wave_share") is not None else 1) for i in lst]
        tot = sum(shares) or 1
        for i, sh in zip(lst, shares):
            base[i["key"]] = wave_w * sh / tot
    comp = []
    for idx in range(len(dates)):
        acc = wsum = 0.0
        for i in scored_inds:
            sc = scores[i["key"]][idx]
            if sc is None or base[i["key"]] <= 0:
                continue
            acc += sc * base[i["key"]]
            wsum += base[i["key"]]
        comp.append(None if wsum == 0 else ((acc / wsum) + 2) / 4 * 100)
    scores["__composite__"] = comp

    print("기간: %s ~ %s (주간 %d개 시점)\n" % (dates[0], dates[-1], len(dates)))

    for mkey, sym in BENCHMARKS.items():
        px = yf.download(sym, start="2005-01-01", interval="1d",
                         auto_adjust=True, progress=False)["Close"]
        if isinstance(px, pd.DataFrame):
            px = px.iloc[:, 0]
        px = px.dropna()
        idx_dates = [d.date().isoformat() for d in px.index]

        def fwd(date_str, n):
            """date_str 이후 첫 거래일 기준 n거래일 forward 수익률(%)."""
            lo, hi = 0, len(idx_dates)
            while lo < hi:
                mid = (lo + hi) // 2
                if idx_dates[mid] < date_str:
                    lo = mid + 1
                else:
                    hi = mid
            if lo >= len(px) or lo + n >= len(px):
                return None
            a, b = float(px.iloc[lo]), float(px.iloc[lo + n])
            return None if a <= 0 else (b / a - 1) * 100

        print("=" * 96)
        print("[%s] 벤치마크 %s — 지표 점수 vs 이후 수익률 (순위상관 / 표본)" % (mkey, sym))
        print("=" * 96)
        hdr = "%-22s" % "지표"
        for h in HORIZONS:
            hdr += "%14s" % h
        print(hdr)

        for key in list(scores):
            label = key if key == "__composite__" else \
                next(i["name_ko"] for i in scored_inds if i["key"] == key)
            line = "%-22s" % label[:20]
            for h, n in HORIZONS.items():
                xs, ys = [], []
                for i, d in enumerate(dates):
                    s = scores[key][i]
                    f = fwd(d, n)
                    if s is None or f is None:
                        continue
                    xs.append(s)
                    ys.append(f)
                rho = spearman(xs, ys)
                out_json["markets"].setdefault(mkey, {}).setdefault(
                    "indicators", {}).setdefault(key, {})[h] = (
                        None if rho is None else round(rho, 3))
                line += "%14s" % ("—" if rho is None else "%+.2f/%d" % (rho, len(xs)))
            print(line)

        # 구성요소 단위 분해 — 지표 안에서 어느 항목이 방향을 뒤집는지 본다
        print("\n  [%s] 구성요소별 순위상관 (지표 점수가 아니라 구성요소 점수 기준)" % mkey)
        print("    %-30s%12s%12s%12s%12s" % ("구성요소", *HORIZONS.keys()))
        for ind in scored_inds:
            mm = mh["indicators"].get(ind["key"]) or {}
            for comp_def in (ind.get("scoring") or {}).get("components", []):
                lab = "%s·%s" % (ind["name_ko"][:8], comp_def["label_ko"][:14])
                line = "    %-30s" % lab[:28]
                for h, n in HORIZONS.items():
                    xs, ys = [], []
                    for i, d in enumerate(dates):
                        v = (mm.get(comp_def["metric"]) or [None] * len(dates))[i]
                        sc = apply_breakpoints(v, comp_def["breakpoints"])
                        f = fwd(d, n)
                        if sc is None or f is None:
                            continue
                        xs.append(sc); ys.append(f)
                    rho = spearman(xs, ys)
                    line += "%12s" % ("—" if rho is None else "%+.2f" % rho)
                print(line)

        # 종합점수 구간별 이후 수익률
        print("\n  [%s] 종합점수 구간별 이후 %s 수익률 평균" % (mkey, "6개월"))
        buckets = [(0, 40), (40, 55), (55, 70), (70, 101)]
        for lo, hi in buckets:
            vals = [fwd(d, 126) for i, d in enumerate(dates)
                    if comp[i] is not None and lo <= comp[i] < hi and fwd(d, 126) is not None]
            if vals:
                out_json["markets"].setdefault(mkey, {}).setdefault("buckets_6m", []).append(
                    {"lo": lo, "hi": hi, "n": len(vals),
                     "mean_pct": round(sum(vals) / len(vals), 2),
                     "median_pct": round(sorted(vals)[len(vals) // 2], 2)})
                print("    %3d–%-3d  n=%-4d 평균 %+6.1f%%  중앙값 %+6.1f%%"
                      % (lo, hi, len(vals), sum(vals) / len(vals),
                         sorted(vals)[len(vals) // 2]))
            else:
                print("    %3d–%-3d  표본 없음" % (lo, hi))
        print()


    from datetime import datetime, timezone
    out_json["generated_at"] = datetime.now(timezone.utc).isoformat()
    out_json["window"] = {"start": dates[0], "end": dates[-1], "points": len(dates)}
    with open(os.path.join(HERE, "leadlag.json"), "w", encoding="utf-8") as f:
        json.dump(out_json, f, ensure_ascii=False, indent=2)
    print("leadlag.json 저장 완료")


if __name__ == "__main__":
    main()
