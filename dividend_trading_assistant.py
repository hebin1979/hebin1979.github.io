#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
红利交易助手 (Dividend Trading Assistant)
========================================================
目的：综合多个市场指标，判断"现在是否适合买入/累积红利低波"，并给出
      合适的价位区间与时间窗口。双卡对比：563020 中证红利低波动ETF 与
      159545 恒生红利低波ETF，二者共用同一套两层框架（宏观环境共享、各自取数）。

评分机制（两层）：563020 自身价格在 52 周区间的分位锚定买卖**方向**占 60%，
  其余 7 指标作为**环境分**调制**力度**占 40%（env = 7 指标按原权重归一化平均）。
  故价位区间(便宜/贵)与综合信号方向必然一致——便宜→偏多、贵→偏谨慎，
  宏观/估值/趋势只决定买多卖少的力度，杜绝"便宜反让等、贵反让买"的反转。

环境分 7 指标（权重合计 100，分数越高 = 越适合"买入/累积"红利低波）：
  1. 股债利差 (沪深300 E/P − 10Y)  权重 22  —— 核心：股票相对债券越便宜越买
  2. 利差分位 (股债利差历史分位)   权重 12  —— 利差处历史越高配置价值越突出
  3. 估值分位 (沪深300 PE 分位)    权重 16  —— PE 越低越便宜
  4. 股息率 (563020 股息率)        权重 16  —— 现金流吸引力
  5. 利率环境 (10Y 国债分位)       权重 14  —— 债券收益率越低红利相对越香
  6. 趋势 (价格/MA12)              权重 12  —— 站上中期均线顺势
  7. 动量 RSI(14)                 权重 8   —— 超卖(低)=更好买点

综合评分 -> 唯一行动信号（双向，全页以此为准）：
  >=68  强烈买入（分批建仓/加仓）
  55-67 条件合适·可小仓位建仓
  45-54 中性·观望（可极少量试仓）
  35-44 估值偏高·持有为主
  <35   高估·建议减仓

重要约定（避免上下矛盾）：
  - 顶部「综合信号」是全页唯一结论，买卖动作只由此给出。
  - ②「价位区间参考」仅描述"价格在 52 周区间中的相对位置"（安全边际参考），
    使用中性标签（深度价值区/价值区/合理区/偏高区/高估区），不下买卖命令。
  - 新增「当前定位与结论」卡片，显式说明技术面价位与综合信号的关系，使结论一致可追溯。
数据来源：沪深300(东财 PE) + 中国债券信息网(10Y) + 563020(东财价格) + 中证指数(股息率)
用法：
  python dividend_trading_assistant.py
  python dividend_trading_assistant.py --manual '{"pe":14.24,"bond10y":1.73,"etf_price":1.144}'
  python dividend_trading_assistant.py --out index.html
"""

import argparse
import json
import math
import os
from datetime import datetime

import realtime_js  # 浏览器端实时行情脚本（共享）

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
    "div_yield": 4.52,      # 563020 股息率 (%)（中证红利低波动930914指数口径）
    "etf_price": 1.144,     # 563020 现价(不复权, 东财实时)
    "etf_52w_high": 1.283,  # 52周高 (2025-11-12, 不复权)
    "etf_52w_low": 1.055,   # 52周低 (2026-06-29, 不复权)
    "etf_ma": 1.186,        # 中期均线 MA12 月线(不复权)
    "etf_rsi": 70.0,        # RSI(14) 日线(不复权)
}

FEEDBACK_URL = "https://github.com/hebin1979/hebin1979.github.io/issues"
HOMEPAGE_URL = "https://hebin1979.github.io/"

# ----------------------------------------------------------------------------
# 0c. 双基金配置：563020 与 159545 共用同一套两层框架。
#     - 宏观环境(沪深300 PE / 10Y / 股债利差) 为两只红利低波 ETF 共享的中国市场背景，
#       以保证"同标准、可对比"；差异来自各 ETF 自身的：价格52周分位(锚定方向)、股息率、趋势、RSI。
#     - 159545 为港股通红利低波 ETF，股息率显著高于 563020，价格已贴近其 1 年低位。
#     刷新某只基金数据：直接改对应 snapshot，或用 --manual 覆盖 563020(默认首只)。
# ----------------------------------------------------------------------------
SHARED_MACRO = {
    "pe": 14.24,            # 沪深300 PE(TTM) —— 共享宏观估值背景
    "bond10y": 1.73,        # 10Y 国债收益率(%) —— 共享宏观利率背景
}
FUNDS = [
    {
        "code": "563020",
        "name": "易方达中证红利低波动ETF",
        "index": "中证红利低波动(930914)",
        "market": "A股",
        "secid": "1.563020",
        "gtimg": "sh563020",
        "as_of": "2026-07-22",
        "snapshot": dict(SHARED_MACRO, **{
            "div_yield": 4.52,     # 563020 股息率(%)（中证红利低波动930914指数口径）
            "etf_price": 1.149,    # 563020 现价(不复权, 东财 2026-07-22)
            "etf_52w_high": 1.283, # 52周高 (2025-11-12, 不复权)
            "etf_52w_low": 1.055,  # 52周低 (2026-06-29, 不复权)
            "etf_ma": 1.189,       # 中期均线 MA200 日线(不复权)
            "etf_rsi": 71.1,       # RSI(14) 日线(不复权)
        }),
    },
    {
        "code": "159545",
        "name": "易方达恒生红利低波ETF",
        "index": "恒生港股通高股息低波动",
        "market": "港股通",
        "secid": "0.159545",
        "gtimg": "sz159545",
        "as_of": "2026-07-22",
        "snapshot": dict(SHARED_MACRO, **{
            "div_yield": 5.80,     # 159545 股息率(%)（恒生港股通高股息低波动指数口径 ~5.8%）
            "etf_price": 1.318,    # 159545 现价(不复权, 东财 2026-07-22)
            "etf_52w_high": 1.541, # 52周高 (不复权)
            "etf_52w_low": 1.193,  # 52周低 (不复权；现价贴近1年低位)
            "etf_ma": 1.425,       # 中期均线 MA200 日线(不复权)
            "etf_rsi": 81.3,       # RSI(14) 日线(不复权)
        }),
    },
]

# ----------------------------------------------------------------------------
# 1. 工具
# ----------------------------------------------------------------------------
def pct_rank(sorted_vals, x):
    """x 在历史序列中的分位 (0-100)。"""
    if not sorted_vals:
        return 50.0
    n = sum(1 for v in sorted_vals if v <= x)
    return n / len(sorted_vals) * 100.0

def pct_of(x, lo, hi):
    """x 在 [lo,hi] 区间中的百分比位置 (0-100)。"""
    if hi <= lo:
        return 50.0
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100.0))

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

def score_price_pct(pct):
    """563020 自身价格在 52 周区间中的分位：越低(越便宜)分越高。锚定买卖方向。"""
    return lerp([(0,95),(20,82),(40,62),(60,42),(80,22),(100,10)], pct)

WEIGHTS = {
    "spread": 22, "spread_pct": 12, "valuation": 16, "dividend": 16,
    "rate": 14, "trend": 12, "rsi": 8,
}

# ----------------------------------------------------------------------------
# 3. 综合分析 (产出全页唯一信号 + 各子指标)
# ----------------------------------------------------------------------------
def analyze(d):
    pe_series = sorted(r[2] for r in RAW)
    bond_series = sorted(r[3] for r in RAW)
    spread_series = sorted(100.0 / r[2] - r[3] for r in RAW)

    ey = 100.0 / d["pe"]
    spread = ey - d["bond10y"]
    pe_pct = pct_rank(pe_series, d["pe"])
    bond_pct = pct_rank(bond_series, d["bond10y"])
    spread_pct = pct_rank(spread_series, spread)
    trend_ratio = d["etf_price"] / d["etf_ma"] if d["etf_ma"] > 0 else 1.0
    etf_pct = pct_of(d["etf_price"], d["etf_52w_low"], d["etf_52w_high"])  # 价格在52周区间位置

    ind = {
        "spread": score_spread(spread),
        "spread_pct": score_spread_pct(spread_pct),
        "valuation": score_valuation(pe_pct),
        "dividend": score_dividend(d["div_yield"]),
        "rate": score_rate(bond_pct),
        "trend": score_trend(trend_ratio),
        "rsi": score_rsi(d["etf_rsi"]),
        "price_pct": score_price_pct(etf_pct),   # 价格锚定方向(不参与环境分)
    }
    # —— 两层框架：563020 自身 52周价格分位锚定买卖方向(60%)，其余 7 指标为环境分调制力度(40%) ——
    # 保证"便宜→偏多 / 贵→偏谨慎"的方向与价位区间一致，宏观/估值/趋势只调节买多卖少的力度。
    anchor = ind["price_pct"]
    env = sum(WEIGHTS[k] * ind[k] for k in WEIGHTS) / sum(WEIGHTS.values())
    composite = 0.6 * anchor + 0.4 * env

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
        trend_ratio=trend_ratio, etf_pct=etf_pct, price_score=anchor,
        indicators=ind, composite=composite, env=env, env_fav=(env >= 50),
        signal=signal, color=color, advice=advice,
    )
    return ctx

# ----------------------------------------------------------------------------
# 4. 价位区间参考（中性位置描述，仅标示价格在 52 周区间的相对位置，
#    不下买卖命令；颜色用于直观表示"低=绿 / 高=红"）
# ----------------------------------------------------------------------------
def compute_zones(d):
    low, high = d["etf_52w_low"], d["etf_52w_high"]
    rng = high - low
    return [
        ("深度价值区", low, low + rng * 0.15,
         "价格贴近 52 周低位，安全边际最高", "#16a34a"),
        ("价值区", low + rng * 0.15, low + rng * 0.40,
         "价格处于年内偏低位置，具备吸引力", "#65a30d"),
        ("合理区", low + rng * 0.40, low + rng * 0.65,
         "价格处于历史中枢，估值合理", "#d97706"),
        ("偏高区", low + rng * 0.65, low + rng * 0.85,
         "价格接近年内高位，注意追高风险", "#ea580c"),
        ("高估区", low + rng * 0.85, high * 1.03,
         "价格逼近/刷新 52 周高位，性价比下降", "#dc2626"),
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

def reconcile_text(d, ctx, zone_name, etf_pct, fund):
    """两层框架的桥梁：标的自身价格在 52 周区间分位锚定买卖方向，其余指标(环境分)只调制力度。
    方向必与价位区间一致：便宜→偏多，贵→偏谨慎；环境好则加大力度，环境差则减小。"""
    comp = ctx["composite"]; sig = ctx["signal"]; ind = ctx["indicators"]; env = ctx["env"]
    code = fund["code"]; name = fund["name"]
    cheap = etf_pct < 40
    pos = ("处于年内偏低位置、安全边际高" if cheap else
           "处于历史中枢附近" if etf_pct < 65 else
           "处于年内偏高位置、安全边际有限")
    bear = []
    if ctx["pe_pct"] >= 60: bear.append(f"沪深300 PE 分位偏高({ctx['pe_pct']:.0f}%)")
    if ctx["spread_pct"] < 60: bear.append(f"股债利差仅处历史{ctx['spread_pct']:.0f}%分位、未达极端")
    if ctx["trend_ratio"] < 0.98: bear.append(f"价格低于中期均线(MA12 {d['etf_ma']:.3f})")
    if d["etf_rsi"] > 65: bear.append(f"RSI {d['etf_rsi']:.0f} 偏高位、追高性价比低")
    bull = []
    if ctx["spread"] >= 5.5: bull.append(f"股债利差走阔({ctx['spread']:.2f}%) 股票相对债券便宜")
    if d["bond10y"] < 1.8: bull.append(f"10Y 国债低位({d['bond10y']:.2f}%) 红利相对吸引力强")
    if d["etf_rsi"] < 35: bull.append(f"RSI {d['etf_rsi']:.0f} 超卖、提供均值回归买点")
    if cheap:
        tail = ("；".join(bear) + "，故以红利低波收息为本、小仓位分批、不一次性满仓") if bear else "，可直接小仓位分批布局、长期持有收息"
        return (f"当前 {code} 现价 {d['etf_price']:.3f}，价位「{zone_name}」{pos}（52 周区间分位 {etf_pct:.0f}%）。"
                f"价格处低位→方向偏多；综合评分 {comp:.0f}/100 得出信号「{sig}」。本体系以价格(52周分位)锚定方向、"
                f"其余指标(环境分 {env:.0f})调制力度：虽便宜可逢低布局，但{tail}。")
    tail = ("；".join(bull) + "，但价格已偏高、安全边际有限，故以持有/观望为主、不追高") if bull else "，且价格偏高、安全边际有限，故以持有/观望为主、不追高"
    return (f"当前 {code} 现价 {d['etf_price']:.3f}，价位「{zone_name}」{pos}（52 周区间分位 {etf_pct:.0f}%）。"
            f"价格偏高→方向偏谨慎；综合评分 {comp:.0f}/100 得出信号「{sig}」。本体系以价格锚定方向、"
            f"其余指标(环境分 {env:.0f})调制力度：{tail}。")

# ----------------------------------------------------------------------------
# 5. HTML 报告
# ----------------------------------------------------------------------------
def gauge(value, color, label, sub="", rt_tag=None):
    import html
    angle = 180 - (value / 100.0) * 180
    rad = math.radians(angle)
    x = 100 + 80 * math.cos(rad)
    y = 100 - 80 * math.sin(rad)
    arc_bg = '<path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="#e5e7eb" stroke-width="14" stroke-linecap="round"/>'
    arc_cls = f' class="rt-{rt_tag}-arc"' if rt_tag else ''
    arc_fg = f'<path{arc_cls} d="M 20 100 A 80 80 0 0 1 {x:.1f} {y:.1f}" fill="none" stroke="{color}" stroke-width="14" stroke-linecap="round"/>'
    val_cls = f' class="rt-{rt_tag}-val"' if rt_tag else ''
    div_cls = f'gauge rt-{rt_tag}' if rt_tag else 'gauge'
    return f'''<div class="{div_cls}">
      <svg viewBox="0 0 200 120" width="100%" height="120">
        {arc_bg}{arc_fg}
        <text{val_cls} x="100" y="92" text-anchor="middle" font-size="30" font-weight="700" fill="{color}">{value:.0f}</text>
        <text x="100" y="112" text-anchor="middle" font-size="11" fill="#6b7280">/100</text>
      </svg>
      <div class="g-label">{html.escape(label)}</div>
      <div class="g-sub">{html.escape(sub)}</div>
    </div>'''

def render_panel(d, ctx, fund, active=False):
    import html
    ind = ctx["indicators"]; comp = ctx["composite"]
    code = fund["code"]; name = fund["name"]; idx = fund["index"]; market = fund["market"]
    cz_name, cz_color, cz_desc = current_zone(d)
    rec = reconcile_text(d, ctx, cz_name, ctx["etf_pct"], fund)

    def card(cname, val, score, color, note, cid=None):
        cls = f'card rt-card-{cid}' if cid else 'card'
        return (f'<div class="{cls}"><div class="c-head"><span class="c-name">{cname}</span>'
                f'<span class="c-score" style="color:{color}">{score:.0f}</span></div>'
                f'<div class="c-val">{val}</div>'
                f'<div class="bar"><div class="bar-fill" style="width:{score:.0f}%;background:{color}"></div></div>'
                f'<div class="c-note">{note}</div></div>')

    cards = [
        card(f"股价分位 ({code} vs 52周)", f"价格位于年内 {ctx['etf_pct']:.0f}% 分位",
             ind["price_pct"], "#b45309", "⚓ 锚定方向：价格越低越便宜(偏多)", cid='price_pct'),
        card("股债利差 (E/P−10Y)", f"利差 {ctx['spread']:.2f}% (E/P {ctx['ey']:.2f}%−{d['bond10y']:.2f}%)",
             ind["spread"], "#dc2626", "核心：股票相对债券越便宜越买"),
        card("利差历史分位", f"分位 {ctx['spread_pct']:.0f}%",
             ind["spread_pct"], "#ea580c", "利差处历史越高，配置价值越突出"),
        card("PE分位 (沪深300)", f"PE {d['pe']:.2f} (分位{ctx['pe_pct']:.0f}%)",
             ind["valuation"], "#2563eb", "环境因子：PE 越低越便宜"),
        card(f"股息率 ({code})", f"股息率 {d['div_yield']:.2f}%",
             ind["dividend"], "#16a34a", "现金流吸引力，越高越香"),
        card("利率环境 (10Y国债)", f"10Y {d['bond10y']:.2f}% (分位{ctx['bond_pct']:.0f}%)",
             ind["rate"], "#0891b2", "利率越低红利相对越香"),
        card("趋势 (价格/MA12)", f"比值 {ctx['trend_ratio']:.2f}×",
             ind["trend"], "#7c3aed", "站上中期均线顺势，破位防守", cid='trend'),
        card("动量 RSI(14)", f"RSI = {d['etf_rsi']:.0f}",
             ind["rsi"], "#db2777", "超卖(低)=更好买点"),
    ]

    zone_rows = ""
    for zn, lo, hi, desc, color in compute_zones(d):
        mark = "◀ 当前" if zn == cz_name else ""
        zone_rows += (f"<tr><td><b style='color:{color}'>{zn}</b></td>"
                      f"<td>{lo:.3f}</td><td>{hi:.3f}</td>"
                      f"<td style='text-align:left;font-size:12px;color:#475569'>{desc}</td>"
                      f"<td>{mark}</td></tr>")

    bull = [("利差极端便宜", f"股债利差升至历史前25%便宜区（分位≥75%，现 {ctx['spread_pct']:.0f}%）→ 重仓分批"),
            ("利率新低", f"10Y 国债跌破 1.5%（现 {d['bond10y']:.2f}%）→ 利差被动走阔"),
            ("估值回落", f"沪深300 PE 分位回落至 <30%（现 {ctx['pe_pct']:.0f}%）"),
            ("技术买点", f"{code} 跌破 MA12({d['etf_ma']:.3f}) 或 RSI<35（现 RSI {d['etf_rsi']:.0f}）")]
    bear = [("利差极端昂贵", f"股债利差跌至历史后25%便宜区（分位≤25% 或 绝对值<3.5%，现 {ctx['spread_pct']:.0f}% / {ctx['spread']:.2f}%）→ 降至底仓"),
            ("利率上行", f"10Y 国债升破 2.5%（现 {d['bond10y']:.2f}%）→ 利差承压"),
            ("估值偏贵", f"沪深300 PE 分位 >70%（现 {ctx['pe_pct']:.0f}%）→ 吸引力下降"),
            ("风格拥挤", f"{code} 逼近 52 周高({d['etf_52w_high']:.3f}) 且 RSI>70 超买")]

    bull_html = "".join(f'<div class="rule rule-bull"><span class="r-kind">加仓信号</span><b>{t}</b><br>{v}</div>' for t, v in bull)
    bear_html = "".join(f'<div class="rule rule-bear"><span class="r-kind">止盈信号</span><b>{t}</b><br>{v}</div>' for t, v in bear)

    status = {
        "沪深300 PE / 10Y国债": "东财 / 中国债券信息网（共享宏观·快照）",
        f"{code} 价格·MA·RSI": "东财（快照）",
        "股息率": f"{idx} 指数口径",
        "PE分位 / 利差分位": "基于内嵌历史序列计算",
    }
    st_lines = "".join(f"<li>{k}: {v}</li>" for k, v in status.items())

    return f'''<div class="fund-panel" id="panel-{code}" style="display:{'block' if active else 'none'}">
<div class="comp-wrap">
  <div class="comp-gauge">{gauge(comp, ctx['color'], "综合评分", ctx['signal'], rt_tag='comp')}</div>
  <div style="flex:1;min-width:280px">
    <h2 style="margin-bottom:10px;font-size:15px">{code} 指标仪表盘（分数越高 = 越适合买入/累积红利低波）</h2>
    <div class="cards" style="grid-template-columns:repeat(auto-fill,minmax(140px,1fr))">
      {''.join(gauge(ind[k], '#b91c1c', n, '', rt_tag=('price_pct' if k=='price_pct' else ('trend' if k=='trend' else None))) for k,n in
        [('price_pct','股价分位⚓'),('spread','股债利差'),('spread_pct','利差分位'),('valuation','PE分位'),('dividend','股息率'),
         ('rate','利率'),('trend','趋势'),('rsi','RSI')])}
    </div>
  </div>
</div>

<div class="recon" style="border:2px solid {cz_color}">
  <div class="r-col">
    <div class="r-label">📍 技术面价位（价格相对 52 周区间）</div>
    <div class="r-val" style="color:{cz_color}">{cz_name}</div>
    <div style="font-size:12px;color:#64748b;margin-top:4px" class="rt-recon-price">现价 {d['etf_price']:.3f} ｜ 52 周区间分位 {ctx['etf_pct']:.0f}%</div>
  </div>
  <div class="r-col">
    <div class="r-label">🎯 综合信号（{code} 唯一结论）</div>
    <div class="r-val rt-signal" style="color:{ctx['color']}">{ctx['signal']}</div>
    <div style="font-size:12px;color:#64748b;margin-top:4px" class="rt-comp-score">综合评分 {comp:.0f}/100</div>
  </div>
  <div class="r-text">{rec}</div>
</div>

<div class="section"><h2>① 七维指标明细（{code}）</h2><div class="cards">{''.join(cards)}</div></div>

<div class="section"><h2>② 价位区间参考（仅标示价格相对 52 周区间的位置，安全边际参考）</h2>
  <table><thead><tr>
    <th>价位区间</th><th>下沿</th><th>上沿</th><th>位置描述</th><th>状态</th>
  </tr></thead><tbody>{zone_rows}</tbody></table>
  <p style="font-size:12px;color:#64748b;margin-top:10px">
  区间基于 {code} 的 52 周高低({d['etf_52w_low']:.3f}–{d['etf_52w_high']:.3f}) 按百分比切分，颜色仅表示"低=绿 / 高=红"。
  此表为<b>安全边际参考</b>，不单独下买卖命令；具体买入/卖出操作一律以上方「综合信号」为准。
  红利低波宜长期持有、分红再投，择时仅用于加减仓，不必清仓。</p>
</div>

<div class="section"><h2>③ 时间窗口与触发条件（何时加仓 / 何时止盈）</h2>
  <div class="rules">
    <div><div style="font-weight:700;margin-bottom:8px;color:#16a34a">加仓信号</div>{bull_html}</div>
    <div><div style="font-weight:700;margin-bottom:8px;color:#dc2626">止盈信号</div>{bear_html}</div>
  </div>
  <p style="font-size:12px;color:#64748b;margin-top:12px">
  核心逻辑：股债利差走阔(股票相对债券便宜)+估值低位+利率下行时"逢低累积"；利差收窄+估值偏贵时"分批止盈"。
  触发阈值基于 2014 年以来历史分位<b>自适应</b>设定，贴合红利低波的收息属性。
  红利低波以收息为本，宜长线持有、分红再投，择时仅用于加减仓。</p>
</div>

<div class="section"><h2>④ 数据来源与新鲜度（{code}）</h2>
  <ul class="status">{st_lines}</ul>
  <p style="font-size:12px;color:#94a3b8;margin-top:8px">
  股债利差 = 沪深300 盈利收益率(100/PE) − 10Y 国债收益率；分位基于内嵌 2014 年以来历史序列计算。
  两只 ETF 共用同一宏观背景(沪深300 PE / 10Y)，以保证"同标准、可对比"；差异来自各自价格52周分位(锚定方向)、股息率、趋势与RSI。
  数值为快照校准，请以实时数据为准。</p>
</div>
</div>'''


def build_html(funds_data):
    import html
    first_code = funds_data[0][2]["code"]
    cmp_items = ""
    for d, ctx, fund in funds_data:
        cz = current_zone(d)[0]
        cmp_items += (f'<div class="cmp-card">'
            f'<div class="cmp-code">{fund["code"]} <span class="cmp-mkt">{fund["market"]}</span></div>'
            f'<div class="cmp-name">{fund["name"]}</div>'
            f'<div class="cmp-sig" id="cmp-sig-{fund["code"]}" style="color:{ctx["color"]}">{ctx["signal"]}</div>'
            f'<div class="cmp-score" id="cmp-score-{fund["code"]}">综合 {ctx["composite"]:.0f}/100 ｜ 价位「{cz}」</div>'
            f'</div>')
    tabs = ""
    for i, (d, ctx, fund) in enumerate(funds_data):
        active = " active" if i == 0 else ""
        tabs += (f'<button class="tab-btn{active}" id="tab-{fund["code"]}" '
                 f'onclick="showFund(\'{fund["code"]}\')">{fund["code"]} {fund["name"]}</button>')
    panels = "".join(render_panel(d, ctx, fund, i == 0) for i, (d, ctx, fund) in enumerate(funds_data))
    as_of = " ｜ ".join(f'{fund["code"]}:{fund["as_of"]}' for d, ctx, fund in funds_data)
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
 padding:24px 28px;margin-bottom:16px}}
header h1{{font-size:22px;margin-bottom:6px}}
header .meta{{font-size:12px;opacity:.85}}
.cmp-strip{{display:flex;gap:14px;margin-top:14px;flex-wrap:wrap}}
.cmp-card{{flex:1;min-width:220px;background:rgba(255,255,255,.12);border-radius:12px;padding:12px 14px}}
.cmp-code{{font-size:13px;font-weight:700}}
.cmp-mkt{{font-size:11px;background:rgba(255,255,255,.22);border-radius:4px;padding:1px 6px;margin-left:4px}}
.cmp-name{{font-size:12px;opacity:.85;margin:2px 0 6px}}
.cmp-sig{{font-size:17px;font-weight:800;color:#fff}}
.cmp-score{{font-size:12px;opacity:.9;margin-top:3px}}
.tabs{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}}
.tab-btn{{flex:1;min-width:180px;padding:12px;border:1px solid #e2e8f0;background:#fff;border-radius:12px;
 cursor:pointer;font-size:14px;font-weight:600;color:#475569;text-align:left}}
.tab-btn.active{{border-color:#b91c1c;background:#fef2f2;color:#b91c1c}}
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
.recon{{display:flex;gap:20px;align-items:stretch;background:linear-gradient(135deg,#fff7ed,#fef2f2);
 border-radius:14px;padding:18px;margin-bottom:20px;flex-wrap:wrap}}
.recon .r-col{{flex:1;min-width:200px}}
.recon .r-label{{font-size:12px;color:#64748b;margin-bottom:4px}}
.recon .r-val{{font-size:18px;font-weight:800}}
.recon .r-text{{flex:2 1 100%;font-size:13px;line-height:1.7;color:#334155;margin-top:14px;
 border-top:1px dashed #fcd9b6;padding-top:12px}}
.status{{font-size:12px;color:#64748b}}
.status li{{margin:2px 0;list-style-position:inside}}
footer{{text-align:center;font-size:11px;color:#94a3b8;margin-top:10px}}
.rt-stamp{{display:inline-block;margin-top:10px;padding:4px 12px;border-radius:8px;font-size:12px;font-weight:700}}
.rt-ok{{background:#dcfce7;color:#166534}}
.rt-warn{{background:#fef9c3;color:#854d0e}}
.rt-fail{{background:#fee2e2;color:#991b1b}}
@media (max-width:600px){{
  body{{padding:12px}}
  .cards{{grid-template-columns:1fr 1fr}}
  .rules{{grid-template-columns:1fr}}
  .comp-gauge{{flex:1 1 100%}}
  .cmp-strip{{flex-direction:column}}
}}
</style></head><body><div class="wrap">
<header>
  <h1>📊 红利交易助手</h1>
  <div class="meta">数据时间：{as_of} ｜ 双卡对比：563020 / 159545 ｜ 框架：价格锚定方向+环境调制力度 + 价位区间 + 时间窗口</div>
  <div class="cmp-strip">{cmp_items}</div>
  <div id="dataStamp" class="rt-stamp">⏳ 正在获取实时行情…</div>
</header>

<div class="tabs">{tabs}</div>

{panels}

<footer>
  红利交易助手 · 仅供研究参考，非投资建议<br>
  生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} ·
  <a href="{HOMEPAGE_URL}" style="color:#b91c1c">返回工具箱首页</a> ·
  <a href="{FEEDBACK_URL}" style="color:#b91c1c">反馈/建议</a>
</footer>
<script>
function showFund(id){{
  document.querySelectorAll('.fund-panel').forEach(function(p){{p.style.display='none';}});
  document.getElementById('panel-'+id).style.display='block';
  document.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active');}});
  document.getElementById('tab-'+id).classList.add('active');
}}
</script>
</div>'''
    # —— 浏览器端实时行情：逐基金拉实时价并刷新价格驱动评分（环境分沿用快照）——
    rt_funds = []
    for (d, ctx, fund) in funds_data:
        code = fund["code"]
        indd = ctx["indicators"]
        rt_funds.append({
            "scope": "#panel-" + code,
            "secid": fund["secid"], "gtimg": fund["gtimg"], "name": fund["name"],
            "lo": round(float(d["etf_52w_low"]), 4), "hi": round(float(d["etf_52w_high"]), 4), "ma": round(float(d["etf_ma"]), 4),
            "env": {"spread": round(float(indd["spread"]),1), "spread_pct": round(float(indd["spread_pct"]),1), "valuation": round(float(indd["valuation"]),1), "dividend": round(float(indd["dividend"]),1), "rate": round(float(indd["rate"]),1), "trend": round(float(indd["trend"]),1), "rsi": round(float(indd["rsi"]),1)},
            "weights": {"spread": 22, "spread_pct": 12, "valuation": 16, "dividend": 16, "rate": 14, "trend": 12, "rsi": 8},
            "anchors": {"val": [[0,95],[20,82],[40,62],[60,42],[80,22],[100,10]], "trend": [[0.90,25],[0.96,42],[1.0,60],[1.03,73],[1.08,86]]},
            "cardVal": "price_pct", "cmpSig": "#cmp-sig-" + code, "cmpScore": "#cmp-score-" + code
        })
    rt_cfg = {"kind": "dividend", "funds": rt_funds}
    rt_cfg_json = json.dumps(rt_cfg, ensure_ascii=False)
    html_doc = html_doc + '<script>var RT_CFG=' + rt_cfg_json + ';</script>\n<script>' + realtime_js.REALTIME_JS + '</script>\n</body></html>'
    return html_doc

# ----------------------------------------------------------------------------
# 6. 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="红利交易助手 (Dividend Trading Assistant)")
    ap.add_argument("--manual", default=None, help="手动覆盖JSON(覆盖首只基金563020的快照字段)")
    ap.add_argument("--out", default=None, help="HTML 输出路径")
    args = ap.parse_args()

    funds_data = []
    for fi, fund in enumerate(FUNDS):
        d = dict(fund["snapshot"])
        if args.manual and fi == 0:   # --manual 仅覆盖首只(563020)，保持向后兼容
            try:
                d.update(json.loads(args.manual))
            except Exception as e:
                print("manual JSON 解析失败:", e)
        ctx = analyze(d)
        funds_data.append((d, ctx, fund))

    print("=" * 60)
    print("  红利交易助手  ·  双卡对比（563020 / 159545）")
    print("=" * 60)
    for d, ctx, fund in funds_data:
        cz_name, _, _ = current_zone(d)
        print(f"\n  ▶ {fund['code']} {fund['name']}  (数据 {fund['as_of']})")
        print(f"    现价      : {d['etf_price']:.3f}  (52周 {d['etf_52w_low']:.3f}-{d['etf_52w_high']:.3f})")
        print(f"    价格52周分位: {ctx['etf_pct']:.0f}%  -> 价位区间「{cz_name}」")
        print(f"    股息率    : {d['div_yield']:.2f}%  沪深300 PE {d['pe']:.2f}(分位{ctx['pe_pct']:.0f}%)  10Y {d['bond10y']:.2f}%")
        print(f"    股债利差  : {ctx['spread']:.2f}% (分位{ctx['spread_pct']:.0f}%)")
        print(f"    ⚓ 价格锚定: {ctx['price_score']:.1f}   🌡 环境分: {ctx['env']:.1f}")
        print(f"    ★ 综合评分: {ctx['composite']:.1f} / 100  -> 信号「{ctx['signal']}」")
    print("=" * 60)

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "dividend_trading_report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_html(funds_data))
    print(f"  ✅ 报告已生成: {out}\n")

if __name__ == "__main__":
    main()
