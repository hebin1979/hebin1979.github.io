#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
红利交易助手 (Dividend Trading Assistant)
========================================================
目的：综合多个市场指标，判断"现在是否适合买入/累积红利低波"，并给出
      合适的买入/卖出价位区间与最佳时间窗口（标的：563020 易方达中证红利低波动ETF）。

指标框架（7 个，权重合计 100）—— 分数越高 = 越适合"买入/累积"红利低波：
  1. 股债利差 (沪深300 E/P − 10Y)  权重 22  —— 核心：股票相对债券越便宜越买
  2. 利差分位 (股债利差历史分位)   权重 12  —— 利差处历史越高配置价值越突出
  3. 估值分位 (沪深300 PE 分位)    权重 16  —— PE 越低越便宜
  4. 股息率 (563020 股息率)        权重 16  —— 现金流吸引力
  5. 利率环境 (10Y 国债分位)       权重 14  —— 债券收益率越低红利相对越香
  6. 趋势 (价格/MA12)              权重 12  —— 站上中期均线顺势
  7. 动量 RSI(14)                 权重 8   —— 超卖(低)=更好买点

综合评分 -> 行动信号（双向）：
  >=68  强烈买入（分批建仓/加仓）
  55-67 条件合适·可小仓位建仓
  45-54 中性·观望
  35-44 估值偏高·持有为主
  <35   高估·建议减仓

数据来源：沪深300(东财 PE) + 中国债券信息网(10Y) + 563020(东财价格) + 中证指数(股息率)
用法：
  python dividend_trading_assistant.py
  python dividend_trading_assistant.py --manual '{"pe":14.24,"bond10y":1.73,"etf_price":1.253}'
  python dividend_trading_assistant.py --out index.html
"""

import argparse
import json
import math
import os
from datetime import datetime

# ----------------------------------------------------------------------------
# 0. 内嵌历史序列 (月度: [日期, 沪深300点位, 沪深300 PE(TTM), 10Y国债%, 563020后复权价])
#    用于计算 PE分位 / 利差分位 / MA12 / RSI。历史序列为估算校准，仅最新行为实时值。
# ----------------------------------------------------------------------------
RAW = [
['2014-01',2160,8.35,4.50,1480],['2014-06',2165,7.90,4.20,1560],
['2014-12',3533,12.73,3.60,1950],['2015-06',4473,18.27,3.60,2250],
['2015-12',3731,13.91,2.85,2080],['2016-06',3154,11.30,2.88,2020],
['2016-12',3310,12.20,3.05,2120],['2017-06',3666,13.00,3.55,2300],
['2017-12',4030,14.40,3.88,2600],['2018-06',3510,12.00,3.55,2480],
['2018-12',3010,10.40,3.25,2350],['2019-06',3825,12.20,3.22,2780],
['2019-12',4096,12.50,3.15,2950],['2020-06',4163,13.10,2.85,2950],
['2020-12',5211,16.10,3.18,3720],['2021-06',5224,15.30,3.10,3880],
['2021-12',4940,14.10,2.80,3820],['2022-06',4485,13.80,2.82,3720],
['2022-12',3871,12.40,2.84,3580],['2023-06',3842,11.90,2.68,3600],
['2023-08',3765,11.60,2.58,3580],
['2023-12',3431,11.20,2.58,1.000],['2024-03',3537,11.50,2.30,1.098],
['2024-06',3461,11.50,2.20,1.150],['2024-09',4017,13.50,2.08,1.215],
['2024-12',3935,13.50,1.78,1.219],['2025-03',3887,13.50,1.82,1.209],
['2025-06',3924,13.60,1.68,1.288],['2025-09',3950,13.80,1.70,1.244],
['2025-12',4081,14.31,1.68,1.277],['2026-03',4180,14.60,1.82,1.305],
['2026-04',4250,14.70,1.75,1.292],['2026-05',4380,14.55,1.72,1.272],
['2026-06',4480,14.40,1.74,1.170],['2026-07',4810,14.24,1.73,1.253],
]

# ----------------------------------------------------------------------------
# 0b. 快照 (最新实时值, 可用 --manual 覆盖)
# ----------------------------------------------------------------------------
SNAPSHOT = {
    "as_of": "2026-07-15",
    "pe": 14.24,            # 沪深300 PE(TTM)
    "bond10y": 1.73,        # 10Y 国债收益率 (%)
    "div_yield": 4.52,      # 563020 股息率 (%)
    "etf_price": 1.253,     # 563020 现价(后复权口径)
    "etf_52w_high": 1.310,
    "etf_52w_low": 1.170,
    "etf_ma": 1.263,        # 中期均线 (MA12 月)
    "etf_rsi": 48.0,        # RSI(14)
}

FEEDBACK_URL = "https://github.com/hebin1979/hebin1979.github.io/issues"
HOMEPAGE_URL = "https://hebin1979.github.io/"

# ----------------------------------------------------------------------------
# 1. 工具
# ----------------------------------------------------------------------------
def pct_rank(sorted_vals, x):
    """x 在历史序列中的分位 (0-100)。"""
    if not sorted_vals:
        return 50.0
    n = sum(1 for v in sorted_vals if v <= x)
    return n / len(sorted_vals) * 100.0

def rsi(closes, n=14):
    if len(closes) < n + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0)); losses.append(max(-ch, 0))
    g = sum(gains[-n:]) / n; l = sum(losses[-n:]) / n
    if l == 0:
        return 100.0
    rs = g / l
    return 100.0 - 100.0 / (1.0 + rs)

def lerp(anchors, x):
    if x <= anchors[0][0]:
        return float(anchors[0][1])
    if x >= anchors[-1][0]:
        return float(anchors[-1][1])
    for i in range(1, len(anchors)):
        if x <= anchors[i][0]:
            x0, y0 = anchors[i-1]; x1, y1 = anchors[i]
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0)
    return float(anchors[-1][1])

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

# ----------------------------------------------------------------------------
# 2. 指标打分 (0-100, 越高越适合买入/累积红利低波)
# ----------------------------------------------------------------------------
def score_spread(spread):
    """股债利差(%) = 沪深300 E/P − 10Y。越高=股票相对债券越便宜。"""
    return lerp([(2,15),(3.5,32),(4.5,50),(5.5,66),(6.5,82),(8,95)], spread)

def score_spread_pct(pct):
    """利差历史分位：处历史越高越划算。"""
    return lerp([(0,12),(25,32),(50,55),(75,78),(100,95)], pct)

def score_valuation(pe_pct):
    """PE 分位：越低(越便宜)分越高。"""
    return 100.0 - pe_pct

def score_dividend(dy):
    """股息率(%)：越高现金流吸引力越强。"""
    return lerp([(2,20),(3,38),(4,58),(4.5,70),(5,82),(6,93)], dy)

def score_rate(bond_pct):
    """10Y 国债分位：利率越低(分位越低)红利相对越香。"""
    return 100.0 - bond_pct

def score_trend(ratio):
    """价格/MA12：站上均线顺势，破位防守。"""
    return lerp([(0.90,25),(0.96,42),(1.0,60),(1.03,73),(1.08,86)], ratio)

def score_rsi(r):
    """RSI(14)：超卖(低)=更好买点。"""
    return lerp([(20,92),(30,85),(45,70),(55,55),(65,40),(75,22)], r)

WEIGHTS = {
    "spread": 22, "spread_pct": 12, "valuation": 16, "dividend": 16,
    "rate": 14, "trend": 12, "rsi": 8,
}

# ----------------------------------------------------------------------------
# 3. 综合分析
# ----------------------------------------------------------------------------
def analyze(d):
    # 历史序列派生
    pe_series = sorted(r[2] for r in RAW)
    bond_series = sorted(r[3] for r in RAW)
    spread_series = sorted(100.0 / r[2] - r[3] for r in RAW)

    ey = 100.0 / d["pe"]
    spread = ey - d["bond10y"]
    pe_pct = pct_rank(pe_series, d["pe"])
    bond_pct = pct_rank(bond_series, d["bond10y"])
    spread_pct = pct_rank(spread_series, spread)
    trend_ratio = d["etf_price"] / d["etf_ma"] if d["etf_ma"] > 0 else 1.0

    ind = {
        "spread": score_spread(spread),
        "spread_pct": score_spread_pct(spread_pct),
        "valuation": score_valuation(pe_pct),
        "dividend": score_dividend(d["div_yield"]),
        "rate": score_rate(bond_pct),
        "trend": score_trend(trend_ratio),
        "rsi": score_rsi(d["etf_rsi"]),
    }
    composite = sum(WEIGHTS[k] * ind[k] for k in WEIGHTS) / 100.0

    if composite >= 68:
        signal = "强烈买入 · 分批建仓"; color = "#16a34a"
        advice = "多项指标共振利多：股债利差高、估值便宜、利率低位。建议分批买入红利低波(563020)，现金仅留逆回购机动。"
    elif composite >= 55:
        signal = "条件合适 · 可小仓位建仓"; color = "#65a30d"
        advice = "环境偏友好但非极致。建议先小仓位(1/3)建仓，待利差进一步走阔或估值回落再加码。"
    elif composite >= 45:
        signal = "中性 · 观望"; color = "#d97706"
        advice = "利差与估值中性，性价比一般。可少量定投收息，主仓等待利差走阔或回撤更充分。"
    elif composite >= 35:
        signal = "估值偏高 · 持有为主"; color = "#ea580c"
        advice = "股票相对债券吸引力下降。持有现有仓位收息，逼近前高分批止盈，不宜追高。"
    else:
        signal = "高估 · 建议减仓"; color = "#dc2626"
        advice = "利差收窄、估值偏贵。建议减仓转逆回购锁定收益，保留底仓等待更好时点。"

    ctx = dict(
        ey=ey, spread=spread, pe_pct=pe_pct, bond_pct=bond_pct, spread_pct=spread_pct,
        trend_ratio=trend_ratio, indicators=ind, composite=composite,
        signal=signal, color=color, advice=advice,
    )
    return ctx

# ----------------------------------------------------------------------------
# 4. 买入/卖出价位区间 (563020 价格, 基于 52周高低 + MA)
# ----------------------------------------------------------------------------
def compute_zones(d):
    low, high, ma = d["etf_52w_low"], d["etf_52w_high"], d["etf_ma"]
    rng = high - low
    return [
        ("强力买入区", low, ma * 0.97,
         "接近/跌破 52 周低位，长期价值凸显，可重仓分批", "#16a34a"),
        ("分批建仓区", ma * 0.97, ma * 1.03,
         "围绕中期均线上下，逢低累积的主战场", "#65a30d"),
        ("持有观望区", ma * 1.03, high * 0.96,
         "估值合理偏高，持有收息、不追高不加仓", "#d97706"),
        ("分批止盈区", high * 0.96, high * 0.995,
         "接近 52 周高位，开始分批减仓锁定利润", "#ea580c"),
        ("强力减仓区", high * 0.995, high * 1.03,
         "刷新/逼近 52 周高位，仅留底仓收息", "#dc2626"),
    ]

def current_zone(d):
    p = d["etf_price"]
    zs = compute_zones(d)
    for name, lo, hi, desc, color in zs:
        if lo <= p <= hi:
            return name, color, desc
    if p < zs[0][1]:
        z = zs[0]; return z[0], z[4], z[3]
    z = zs[-1]; return z[0], z[4], z[3]

# ----------------------------------------------------------------------------
# 5. HTML 报告
# ----------------------------------------------------------------------------
def gauge(value, color, label, sub=""):
    import html
    angle = 180 - (value / 100.0) * 180
    rad = math.radians(angle)
    x = 100 + 80 * math.cos(rad)
    y = 100 - 80 * math.sin(rad)
    arc_bg = '<path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="#e5e7eb" stroke-width="14" stroke-linecap="round"/>'
    arc_fg = f'<path d="M 20 100 A 80 80 0 0 1 {x:.1f} {y:.1f}" fill="none" stroke="{color}" stroke-width="14" stroke-linecap="round"/>'
    return f'''<div class="gauge">
      <svg viewBox="0 0 200 120" width="100%" height="120">
        {arc_bg}{arc_fg}
        <text x="100" y="92" text-anchor="middle" font-size="30" font-weight="700" fill="{color}">{value:.0f}</text>
        <text x="100" y="112" text-anchor="middle" font-size="11" fill="#6b7280">/100</text>
      </svg>
      <div class="g-label">{html.escape(label)}</div>
      <div class="g-sub">{html.escape(sub)}</div>
    </div>'''

def build_html(d, ctx):
    ind = ctx["indicators"]
    comp = ctx["composite"]
    cz_name, cz_color, cz_desc = current_zone(d)

    def card(name, val, score, color, note):
        return (f'<div class="card"><div class="c-head"><span class="c-name">{name}</span>'
                f'<span class="c-score" style="color:{color}">{score:.0f}</span></div>'
                f'<div class="c-val">{val}</div>'
                f'<div class="bar"><div class="bar-fill" style="width:{score:.0f}%;background:{color}"></div></div>'
                f'<div class="c-note">{note}</div></div>')

    cards = [
        card("股债利差 (E/P−10Y)", f"利差 {ctx['spread']:.2f}% (E/P {ctx['ey']:.2f}%−{d['bond10y']:.2f}%)",
             ind["spread"], "#dc2626", "核心：股票相对债券越便宜越买"),
        card("利差历史分位", f"分位 {ctx['spread_pct']:.0f}%",
             ind["spread_pct"], "#ea580c", "利差处历史越高，配置价值越突出"),
        card("估值分位 (沪深300 PE)", f"PE {d['pe']:.2f} (分位{ctx['pe_pct']:.0f}%)",
             ind["valuation"], "#2563eb", "PE 越低越便宜"),
        card("股息率 (563020)", f"股息率 {d['div_yield']:.2f}%",
             ind["dividend"], "#16a34a", "现金流吸引力，越高越香"),
        card("利率环境 (10Y国债)", f"10Y {d['bond10y']:.2f}% (分位{ctx['bond_pct']:.0f}%)",
             ind["rate"], "#0891b2", "利率越低红利相对越香"),
        card("趋势 (价格/MA12)", f"比值 {ctx['trend_ratio']:.2f}×",
             ind["trend"], "#7c3aed", "站上中期均线顺势，破位防守"),
        card("动量 RSI(14)", f"RSI = {d['etf_rsi']:.0f}",
             ind["rsi"], "#db2777", "超卖(低)=更好买点"),
    ]

    zone_rows = ""
    for name, lo, hi, desc, color in compute_zones(d):
        mark = "◀ 当前" if name == cz_name else ""
        zone_rows += (f"<tr><td><b style='color:{color}'>{name}</b></td>"
                      f"<td>{lo:.3f}</td><td>{hi:.3f}</td>"
                      f"<td style='text-align:left;font-size:12px;color:#475569'>{desc}</td>"
                      f"<td>{mark}</td></tr>")

    bull = [("利差走阔", f"股债利差回升至 6.0%+（现 {ctx['spread']:.2f}%）全仓"),
            ("利率新低", f"10Y 国债破 1.5%（现 {d['bond10y']:.2f}%）利差走阔"),
            ("估值回落", f"沪深300 PE 分位回落至 <30%（现 {ctx['pe_pct']:.0f}%）"),
            ("技术买点", f"563020 回撤至 MA12({d['etf_ma']:.3f}) 下方或 RSI<35（现 {d['etf_rsi']:.0f}）")]
    bear = [("利差收窄", f"股债利差跌破 4.0%（现 {ctx['spread']:.2f}%）减仓50%"),
            ("利率上行", f"10Y 国债升破 2.2%（现 {d['bond10y']:.2f}%）利差承压"),
            ("估值偏贵", f"沪深300 PE 分位 >70%（现 {ctx['pe_pct']:.0f}%）吸引力下降"),
            ("风格拥挤", f"563020 逼近 52 周高({d['etf_52w_high']:.3f}) 且 RSI>70 超买")]

    bull_html = "".join(f'<div class="rule rule-bull"><span class="r-kind">买入</span><b>{t}</b><br>{v}</div>' for t, v in bull)
    bear_html = "".join(f'<div class="rule rule-bear"><span class="r-kind">止盈</span><b>{t}</b><br>{v}</div>' for t, v in bear)

    status = {
        "沪深300 PE / 10Y国债": "东财 / 中国债券信息网 (快照)",
        "563020 价格·MA·RSI": "东财 (快照)",
        "股息率": "中证指数 (中证红利低波动 930914)",
        "PE分位 / 利差分位": "基于内嵌历史序列计算",
    }
    st_lines = "".join(f"<li>{k}: {v}</li>" for k, v in status.items())

    html_doc = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>红利交易助手 · 报告</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif;
 background:#f8fafc;color:#0f172a;padding:24px}}
.wrap{{max-width:1080px;margin:0 auto}}
header{{background:linear-gradient(135deg,#7f1d1d,#b91c1c);color:#fff;border-radius:16px;
 padding:24px 28px;margin-bottom:20px}}
header h1{{font-size:22px;margin-bottom:6px}}
header .meta{{font-size:12px;opacity:.85}}
.signal{{display:inline-block;margin-top:12px;padding:8px 18px;border-radius:10px;
 background:{ctx['color']};color:#fff;font-size:18px;font-weight:700}}
.advice{{margin-top:12px;font-size:14px;line-height:1.6;opacity:.96}}
.comp-wrap{{display:flex;gap:20px;align-items:center;background:#fff;border-radius:16px;
 padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.08);flex-wrap:wrap}}
.comp-gauge{{flex:0 0 220px}}
.gauge .g-label{{text-align:center;font-size:13px;font-weight:600;margin-top:2px}}
.gauge .g-sub{{text-align:center;font-size:11px;color:#94a3b8}}
.section{{background:#fff;border-radius:16px;padding:20px;margin-bottom:20px;
 box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.section h2{{font-size:16px;margin-bottom:14px;color:#1e293b}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}}
.card{{border:1px solid #e2e8f0;border-radius:12px;padding:14px}}
.c-head{{display:flex;justify-content:space-between;align-items:baseline}}
.c-name{{font-size:13px;font-weight:600}}
.c-score{{font-size:22px;font-weight:800}}
.c-val{{font-size:12px;color:#475569;margin:6px 0}}
.bar{{height:8px;background:#e2e8f0;border-radius:4px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:4px}}
.c-note{{font-size:11px;color:#94a3b8;margin-top:6px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:10px 8px;text-align:center;border-bottom:1px solid #eef2f7}}
th{{background:#f8fafc;color:#475569;font-weight:600}}
.rules{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.rule{{border-left:4px solid #16a34a;background:#f8fafc;border-radius:8px;padding:12px;font-size:13px;line-height:1.6}}
.rule-bull{{border-left-color:#16a34a}}
.rule-bear{{border-left-color:#dc2626}}
.r-kind{{display:inline-block;font-size:11px;background:#e2e8f0;color:#475569;
 border-radius:4px;padding:1px 8px;margin-bottom:6px}}
.now-card{{border:2px solid {cz_color};border-radius:12px;padding:16px;background:#fffdf7;margin-bottom:14px}}
.now-card .nc-title{{font-size:14px;font-weight:700;margin-bottom:8px}}
.now-card .nc-zone{{font-size:20px;font-weight:800;color:{cz_color}}}
.now-card .nc-desc{{font-size:12px;color:#64748b;margin-top:6px}}
.status{{font-size:12px;color:#64748b}}
.status li{{margin:2px 0;list-style-position:inside}}
footer{{text-align:center;font-size:11px;color:#94a3b8;margin-top:10px}}
@media (max-width:600px){{
  body{{padding:12px}}
  .cards{{grid-template-columns:1fr 1fr}}
  .rules{{grid-template-columns:1fr}}
  .comp-gauge{{flex:1 1 100%}}
}}
</style></head><body><div class="wrap">
<header>
  <h1>📊 红利交易助手</h1>
  <div class="meta">数据时间：{d.get('as_of','')} ｜ 标的：563020 易方达中证红利低波动ETF ｜ 框架：7 指标加权打分 + 买卖价位区间 + 时间窗口</div>
  <div class="signal">{ctx['signal']}　综合评分 {comp:.0f}/100</div>
  <div class="advice">{ctx['advice']}</div>
</header>

<div class="comp-wrap">
  <div class="comp-gauge">{gauge(comp, ctx['color'], "综合评分", ctx['signal'])}</div>
  <div style="flex:1;min-width:280px">
    <h2 style="margin-bottom:10px;font-size:15px">指标仪表盘（分数越高 = 越适合买入/累积红利低波）</h2>
    <div class="cards" style="grid-template-columns:repeat(auto-fill,minmax(140px,1fr))">
      {''.join(gauge(ind[k], '#b91c1c', n, '') for k,n in
        [('spread','股债利差'),('spread_pct','利差分位'),('valuation','估值'),('dividend','股息率'),
         ('rate','利率'),('trend','趋势'),('rsi','RSI')])}
    </div>
  </div>
</div>

<div class="now-card">
  <div class="nc-title">📍 当前 563020 现价 {d['etf_price']:.3f} 所处区间 ｜ 股债利差 {ctx['spread']:.2f}%</div>
  <div class="nc-zone">{cz_name}</div>
  <div class="nc-desc">{cz_desc}</div>
</div>

<div class="section"><h2>① 七维指标明细</h2><div class="cards">{''.join(cards)}</div></div>

<div class="section"><h2>② 买入 / 卖出价位区间（563020 价格，动态计算）</h2>
  <table><thead><tr>
    <th>区间</th><th>下沿</th><th>上沿</th><th>策略含义</th><th>状态</th>
  </tr></thead><tbody>{zone_rows}</tbody></table>
  <p style="font-size:12px;color:#64748b;margin-top:10px">
  区间基于 563020 的 52 周高低({d['etf_52w_low']:.3f}–{d['etf_52w_high']:.3f}) 与 MA12({d['etf_ma']:.3f}) 动态生成；
  绿区越跌越买，红区越涨越卖。红利低波宜长期持有、分红再投，择时仅用于加减仓，不必清仓。</p>
</div>

<div class="section"><h2>③ 时间窗口与触发条件</h2>
  <div class="rules">
    <div><div style="font-weight:700;margin-bottom:8px;color:#16a34a">买入 / 加仓触发条件</div>{bull_html}</div>
    <div><div style="font-weight:700;margin-bottom:8px;color:#dc2626">止盈 / 减仓触发条件</div>{bear_html}</div>
  </div>
  <p style="font-size:12px;color:#64748b;margin-top:12px">
  核心逻辑：股债利差走阔(股票相对债券便宜)+估值低位+利率下行时"逢低累积"；利差收窄+估值偏贵时"分批止盈"。
  红利低波以收息为本，宜长线持有、分红再投，择时仅用于加减仓。</p>
</div>

<div class="section"><h2>④ 数据来源与新鲜度</h2>
  <ul class="status">{st_lines}</ul>
  <p style="font-size:12px;color:#94a3b8;margin-top:8px">
  股债利差 = 沪深300 盈利收益率(100/PE) − 10Y 国债收益率；分位基于内嵌 2014 年以来历史序列计算。
  数值为快照校准，请以实时数据为准。</p>
</div>

<footer>
  红利交易助手 · 仅供研究参考，非投资建议<br>
  生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} ·
  <a href="{HOMEPAGE_URL}" style="color:#b91c1c">返回工具箱首页</a> ·
  <a href="{FEEDBACK_URL}" style="color:#b91c1c">反馈/建议</a>
</footer>
</div></body></html>'''
    return html_doc

# ----------------------------------------------------------------------------
# 6. 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="红利交易助手 (Dividend Trading Assistant)")
    ap.add_argument("--manual", default=None, help="手动覆盖JSON(覆盖SNAPSHOT字段)")
    ap.add_argument("--out", default=None, help="HTML 输出路径")
    args = ap.parse_args()

    d = dict(SNAPSHOT)
    if args.manual:
        try:
            d.update(json.loads(args.manual))
        except Exception as e:
            print("manual JSON 解析失败:", e)

    print("=" * 60)
    print("  红利交易助手  ·  计算指标 ...")
    print("=" * 60)
    ctx = analyze(d)
    cz_name, _, _ = current_zone(d)

    print(f"\n  数据时间    : {d.get('as_of','')}")
    print(f"  563020 现价 : {d['etf_price']:.3f}  (52周 {d['etf_52w_low']:.3f}-{d['etf_52w_high']:.3f})")
    print(f"  沪深300 PE  : {d['pe']:.2f}  (分位 {ctx['pe_pct']:.0f}%)   E/P {ctx['ey']:.2f}%")
    print(f"  10Y 国债    : {d['bond10y']:.2f}%  (分位 {ctx['bond_pct']:.0f}%)")
    print(f"  股债利差    : {ctx['spread']:.2f}%  (分位 {ctx['spread_pct']:.0f}%)   股息率 {d['div_yield']:.2f}%")
    print(f"  当前区间    : {cz_name}")
    print("-" * 60)
    print("  指标评分(0-100, 越高越适合买入):")
    names = {"spread":"股债利差","spread_pct":"利差分位","valuation":"估值分位","dividend":"股息率",
             "rate":"利率环境","trend":"趋势MA","rsi":"RSI动量"}
    for k in WEIGHTS:
        print(f"    {names[k]:<8} {ctx['indicators'][k]:5.1f}   (权重{WEIGHTS[k]})")
    print("-" * 60)
    print(f"  ★ 综合评分  : {ctx['composite']:.1f} / 100")
    print(f"  ★ 行动信号  : {ctx['signal']}")
    print(f"  ★ 建议      : {ctx['advice']}")
    print("=" * 60)

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "dividend_trading_report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_html(d, ctx))
    print(f"  ✅ 报告已生成: {out}\n")

if __name__ == "__main__":
    main()
