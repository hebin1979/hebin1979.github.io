#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄金交易助手 (Gold Trading Assistant)
====================================
目的：综合多个市场指标，判断"现在是否适合买入/卖出黄金"，并给出
      合适的价位区间与最佳时间窗口。

指标框架（7 个，权重合计 100）—— 分数越高 = 越适合"买入/累积"黄金：
  1. 估值分位 (价格 vs 52周区间)  权重 18  —— 越接近年内低位越便宜
  2. 趋势 (价格/长期均线)        权重 15  —— 不逆强下行趋势
  3. 动量 RSI(14)                 权重 18  —— 超卖(低) = 更好的买点
  4. 实际利率 (10Y TIPS)          权重 20  —— 黄金核心驱动，实际利率越低越利多
  5. 美元指数 DXY                 权重 15  —— 黄金计价货币，美元越弱越利多
  6. 避险情绪 VIX                 权重 9   —— 避险需求支撑金价
  7. 季节因子 (当月)              权重 5   —— 9-11月/12-1月旺季，夏秋淡季

综合评分 -> 行动信号（双向）：
  >=68  强烈买入（分批建仓/加仓）
  55-67 条件合适·可小仓位建仓
  45-54 中性·观望（可极少量试仓）
  35-44 估值偏高·持有为主，考虑部分止盈
  <35   高估过热·建议减仓/卖出

标的：518850 黄金ETF华夏（A股可直接交易，价格与现货金价高度联动）。
  —— ETF 自身价格技术面(价格/52周高低/均线/RSI)：东方财富口径(fqt=0 不复权)，经 --manual 刷新；
  —— 宏观驱动(VIX/DXY/10Y TIPS)：Yahoo Finance / FRED 实时抓取，失败回退快照。

用法：
  python gold_trading_assistant.py                # 拉取实时数据并生成报告
  python gold_trading_assistant.py --offline      # 强制使用内置快照(演示/离线)
  python gold_trading_assistant.py --fred-key X  # 提供 FRED API Key(获取 TIPS)
  python gold_trading_assistant.py --manual json # 手动覆盖关键数值(见 SNAPSHOT)
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None

# ----------------------------------------------------------------------------
# 0. 内置快照 (真实数据校准, 截至 2026-07-13)
#    —— ETF 价格技术面：东方财富 518850 黄金ETF华夏, fqt=0 不复权
#    —— 宏观驱动：Yahoo/FRED 抓取失败时的回退值
# ----------------------------------------------------------------------------
SNAPSHOT = {
    "as_of": "2026-07-13",
    # —— ETF 自身价格技术面（东财 518850, fqt=0 不复权）——
    "gold_price": 8.452,            # 518850 现价 (元)
    "gold_52w_high": 12.087,
    "gold_52w_low": 7.310,
    "gold_ma": 9.011,               # 长期均线(约 200 日/月线收盘均值)
    "gold_rsi": 54.2,
    # —— 宏观驱动（实时抓取 Yahoo/FRED，失败回退）——
    "dxy": 100.91,                  # 美元指数
    "dxy_52w_high": 101.80,
    "dxy_52w_low": 95.55,
    "tips": 2.31,                   # 10Y TIPS 实际收益率 (%)
    "vix": 16.25,
    "vix_52w_high": 35.30,
    "vix_52w_low": 13.38,
    "vix_1y_pct": 13.1,             # VIX 在过去1年中的分位(手动校准)
}

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_cache.json")

# 部署站点相关（反馈入口 / 首页）—— 如需更换反馈地址改这里即可
FEEDBACK_URL = "https://github.com/hebin1979/hebin1979.github.io/issues"
HOMEPAGE_URL = "https://hebin1979.github.io/"

# ----------------------------------------------------------------------------
# 1. 数据抓取层
# ----------------------------------------------------------------------------
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"}

def _session():
    s = requests.Session()
    s.headers.update(UA)
    try:
        s.get("https://fc.yahoo.com", timeout=10)
    except Exception:
        pass
    return s

def _yf_chart(sym, rng="1y", interval="1d", s=None):
    if s is None:
        s = _session()
    for host in ("query1", "query2"):
        try:
            r = s.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}",
                      params={"range": rng, "interval": interval}, timeout=25)
            if r.status_code == 200:
                j = r.json()
                res = j.get("chart", {}).get("result")
                if res:
                    return res[0]
        except Exception:
            continue
    return None

def _fred_obs(series_id, api_key=None, s=None):
    if s is None:
        s = _session()
    if api_key:
        try:
            r = s.get("https://api.stlouisfed.org/fred/series/observations",
                      params={"series_id": series_id, "api_key": api_key,
                              "file_type": "json", "limit": 1}, timeout=25)
            if r.status_code == 200:
                obs = r.json().get("observations", [])
                if obs and obs[-1].get("value") not in (None, "."):
                    return float(obs[-1]["value"])
        except Exception:
            pass
    try:
        r = s.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", timeout=25)
        if r.status_code == 200:
            lines = [l for l in r.text.strip().split("\n") if l and not l.startswith("//")]
            if len(lines) >= 2:
                val = lines[-1].split(",")[-1].strip()
                if val not in (".", ""):
                    return float(val)
    except Exception:
        pass
    return None

def fetch_market_data(fred_key=None, offline=False):
    """返回 (data_dict, status_dict)。"""
    status = {}
    d = {}

    # ---- 黄金ETF 518850 自身价格技术面：始终来自 SNAPSHOT（东财口径, 经 --manual 刷新）----
    for k in ("gold_price", "gold_52w_high", "gold_52w_low", "gold_ma", "gold_rsi"):
        d[k] = SNAPSHOT[k]
        status[k] = "快照/手动"

    if offline or requests is None:
        for k in ("dxy", "dxy_52w_high", "dxy_52w_low", "tips",
                  "vix", "vix_52w_high", "vix_52w_low", "vix_1y_pct"):
            d[k] = SNAPSHOT[k]
            status[k] = "快照/离线"
        d["as_of"] = datetime.now().strftime("%Y-%m-%d") + "(快照/离线)"
        return d, status

    s = _session()

    # ---- 美元指数 DX-Y.NYB ----
    try:
        x = _yf_chart("DX-Y.NYB", "1y", "1d", s)
        if x:
            m = x["meta"]
            d["dxy"] = m["regularMarketPrice"]
            d["dxy_52w_high"] = m.get("fiftyTwoWeekHigh", d["dxy"])
            d["dxy_52w_low"] = m.get("fiftyTwoWeekLow", d["dxy"])
            status["dxy"] = "实时(Yahoo)"
        else:
            raise ValueError("empty")
    except Exception:
        for k in ("dxy", "dxy_52w_high", "dxy_52w_low"):
            d[k] = SNAPSHOT[k]
        status["dxy"] = "回退-快照"

    # ---- VIX ----
    try:
        v = _yf_chart("^VIX", "1y", "1d", s)
        if v:
            m = v["meta"]
            closes = [c for c in v["indicators"]["quote"][0]["close"] if c is not None]
            d["vix"] = m["regularMarketPrice"]
            d["vix_52w_high"] = m.get("fiftyTwoWeekHigh", max(closes))
            d["vix_52w_low"] = m.get("fiftyTwoWeekLow", min(closes))
            d["vix_1y_pct"] = pct_of(d["vix"], d["vix_52w_low"], d["vix_52w_high"])
            status["vix"] = "实时(Yahoo)"
        else:
            raise ValueError("empty")
    except Exception:
        for k in ("vix", "vix_52w_high", "vix_52w_low", "vix_1y_pct"):
            d[k] = SNAPSHOT[k]
        status["vix"] = "回退-快照"

    # ---- 10Y TIPS 实际利率 (FRED DFII10) ----
    try:
        t = _fred_obs("DFII10", fred_key, s)
        d["tips"] = t if t is not None else SNAPSHOT["tips"]
        status["tips"] = "实时(FRED)" if t is not None else "回退-快照"
    except Exception:
        d["tips"] = SNAPSHOT["tips"]
        status["tips"] = "回退-快照"

    d["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"data": d, "status": status, "ts": time.time()},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return d, status

# ----------------------------------------------------------------------------
# 2. 指标计算工具
# ----------------------------------------------------------------------------
def pct_of(x, lo, hi):
    if hi <= lo:
        return 50.0
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100.0))

def sma(vals, n):
    if len(vals) < n:
        return sum(vals) / len(vals)
    return sum(vals[-n:]) / n

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
    """在 (x,y) 锚点列表中线性插值；越界则取端点。"""
    if x <= anchors[0][0]:
        return float(anchors[0][1])
    if x >= anchors[-1][0]:
        return float(anchors[-1][1])
    for i in range(1, len(anchors)):
        if x <= anchors[i][0]:
            x0, y0 = anchors[i-1]; x1, y1 = anchors[i]
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0)
    return float(anchors[-1][1])

# ----------------------------------------------------------------------------
# 3. 指标打分 (每个返回 0-100, 越高越适合买入/累积黄金)
# ----------------------------------------------------------------------------
def score_valuation(gold_pct):
    """价格在 52周区间中的分位：越低(越便宜)分越高。"""
    return lerp([(0,95),(20,82),(40,62),(60,42),(80,22),(100,10)], gold_pct)

def score_trend(ratio):
    """价格/长期均线：远离下方=趋势恶化。"""
    return lerp([(0.85,20),(0.95,45),(1.0,60),(1.05,75),(1.15,88)], ratio)

def score_rsi(rsi):
    """RSI(14)：超卖(低)=更好买点。"""
    return lerp([(20,95),(30,85),(45,72),(55,58),(65,42),(70,28),(80,15)], rsi)

def score_real_yield(tips):
    """10Y TIPS 实际利率(%)：越低越利多黄金。"""
    return lerp([(-1,92),(0,85),(1,68),(2,50),(3,32),(4,18)], tips)

def score_dxy(dxy_pct):
    """美元指数在 52周区间中的分位：越高(美元强)越压制金价。"""
    return lerp([(0,90),(20,82),(50,58),(80,30),(100,12)], dxy_pct)

def score_vix(vix_pct):
    """VIX 分位：越高=避险需求越强(支撑金价)。"""
    return lerp([(0,30),(20,35),(50,60),(80,82),(100,90)], vix_pct)

# 季节因子：黄金历史季节性强弱（0-100），旺季在 9-11 与 12-1 月
SEASON_SCORE = {
    1:75, 2:58, 3:45, 4:42, 5:40, 6:42,
    7:40, 8:40, 9:72, 10:80, 11:78, 12:70,
}
SEASON_NOTE = {
    1:"春节前中国实物需求", 2:"需求回落", 3:"淡季", 4:"淡季", 5:"淡季", 6:"夏淡",
    7:"夏淡·观望", 8:"夏淡·观望", 9:"印度婚季+排灯节备货", 10:"排灯节旺季",
    11:"旺季延续", 12:"圣诞+春节前中国需求",
}
MONTH_CN = {1:"1月",2:"2月",3:"3月",4:"4月",5:"5月",6:"6月",
            7:"7月",8:"8月",9:"9月",10:"10月",11:"11月",12:"12月"}

# ----------------------------------------------------------------------------
# 4. 综合评分
# ----------------------------------------------------------------------------
WEIGHTS = {
    "valuation": 18, "trend": 15, "rsi": 18, "real_yield": 20,
    "dxy": 15, "vix": 9, "season": 5,
}

def analyze(d):
    gold_pct = pct_of(d["gold_price"], d["gold_52w_low"], d["gold_52w_high"])
    trend_ratio = d["gold_price"] / d["gold_ma"] if d["gold_ma"] > 0 else 1.0
    dxy_pct = pct_of(d["dxy"], d["dxy_52w_low"], d["dxy_52w_high"])

    month = datetime.now().month
    season_score = SEASON_SCORE[month]

    ind = {
        "valuation": score_valuation(gold_pct),
        "trend": score_trend(trend_ratio),
        "rsi": score_rsi(d["gold_rsi"]),
        "real_yield": score_real_yield(d["tips"]),
        "dxy": score_dxy(dxy_pct),
        "vix": score_vix(d["vix_1y_pct"]),
        "season": season_score,
    }
    composite = sum(WEIGHTS[k] * ind[k] for k in WEIGHTS) / 100.0

    if composite >= 68:
        signal = "强烈买入 · 分批建仓"
        color = "#16a34a"
        advice = "多项指标共振利多，金价处于低估/回撤区。建议分批建仓，急跌至强支撑可加仓。"
    elif composite >= 55:
        signal = "条件合适 · 可小仓位建仓"
        color = "#65a30d"
        advice = "环境偏友好但非极致。建议先小仓位(1/3)试仓，待实际利率回落或美元走弱再加码。"
    elif composite >= 45:
        signal = "中性 · 观望"
        color = "#d97706"
        advice = "估值不贵但宏观(强美元/正实际利率)与淡季构成压制。可极少量试仓，主仓等待更好时点。"
    elif composite >= 35:
        signal = "估值偏高 · 持有为主"
        color = "#ea580c"
        advice = "金价偏高或动能转弱。持有现有仓位，逼近前高区域分批止盈，不宜追高。"
    else:
        signal = "高估过热 · 建议减仓"
        color = "#dc2626"
        advice = "金价处于年内高位且动能过热/宏观转空。建议减仓锁定利润，等待回调。"

    ctx = dict(
        gold_pct=gold_pct, trend_ratio=trend_ratio, dxy_pct=dxy_pct,
        month=month, season_score=season_score,
        indicators=ind, composite=composite, signal=signal, color=color, advice=advice,
    )
    return ctx

# ----------------------------------------------------------------------------
# 5. 价位区间参考（中性位置描述，仅标示 ETF 价格相对 52 周区间的位置）
# ----------------------------------------------------------------------------
def compute_zones(d):
    low, high = d["gold_52w_low"], d["gold_52w_high"]
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
    p = d["gold_price"]
    zs = compute_zones(d)
    for name, lo, hi, desc, color in zs:
        if lo <= p <= hi:
            return name, color, desc
    if p < zs[0][1]:
        z = zs[0]; return z[0], z[4], z[3]
    z = zs[-1]; return z[0], z[4], z[3]

def reconcile_text(d, ctx, zone_name, gold_pct):
    """说明「价位区间」(价格安全边际) 与「综合信号」(7指标加权) 的关系。
    关键修复：措辞随价格位置走，不再写死"技术面低位提供安全边际"；
    并只列举与价格位置方向相反、用于解释分歧的因子。"""
    comp = ctx["composite"]; sig = ctx["signal"]; ind = ctx["indicators"]
    if gold_pct < 20:   pos, cheap = "处于年内低位、安全边际高", True
    elif gold_pct < 40: pos, cheap = "处于年内偏低位置、具备吸引力", True
    elif gold_pct < 65: pos, cheap = "处于历史中枢附近", True
    elif gold_pct < 85: pos, cheap = "处于年内偏高位置、安全边际有限", False
    else:               pos, cheap = "逼近年内高位、性价比下降", False

    # 压制金价的负面因子（解释"便宜却不强买"）
    bear = []
    if d["tips"] > 2.0: bear.append(f"实际利率偏高(TIPS {d['tips']:.2f}% 压制金价)")
    if ctx["dxy_pct"] > 70: bear.append(f"美元处历史偏强位(分位 {ctx['dxy_pct']:.0f}%)")
    if ctx["trend_ratio"] < 0.98: bear.append(f"价格低于长期均线({d['gold_ma']:.3f})")
    if ind["season"] <= 45: bear.append(f"当前为淡季(季节评分 {ind['season']:.0f})")
    # 支撑金价的正面因子（解释"偏贵却仍买"）
    bull = []
    if d["vix_1y_pct"] > 80: bull.append(f"VIX 处历史高位({d['vix_1y_pct']:.0f}%分位,避险需求升温)")

    if cheap and comp < 55:
        reasons = "；".join(bear) or "宏观面暂无共振利多"
        return (f"当前 518850 现价 {d['gold_price']:.3f}，价位「{zone_name}」{pos}（52 周区间分位 {gold_pct:.0f}%）。"
                f"综合评分 {comp:.0f}/100 得出信号「{sig}」。二者并不冲突：价格虽处低位、安全边际高，"
                f"但{reasons}，宏观面压制金价，故采用「小仓位、逢低分批」而非一次性满仓强买。")
    if (not cheap) and comp >= 55:
        reasons = "；".join(bull) or "动能与风险偏好利多"
        return (f"当前 518850 现价 {d['gold_price']:.3f}，价位「{zone_name}」{pos}（52 周区间分位 {gold_pct:.0f}%）。"
                f"综合评分 {comp:.0f}/100 得出信号「{sig}」。二者并不冲突：价格虽偏高、安全边际有限，"
                f"但{reasons}，动能与宏观支撑金价，故采用「小仓位、逢低分批」而非一次性满仓强买。")
    return (f"当前 518850 现价 {d['gold_price']:.3f}，价位「{zone_name}」{pos}（52 周区间分位 {gold_pct:.0f}%）。"
            f"综合评分 {comp:.0f}/100 亦指向「{sig}」，技术面与基本面共振，可参照上方区间分批操作。")

# ----------------------------------------------------------------------------
# 6. HTML 报告
# ----------------------------------------------------------------------------
def gauge(value, color, label, sub=""):
    import html
    angle = 180 - (value / 100.0) * 180
    rad = math.radians(angle)
    x = 100 + 80 * math.cos(rad)
    y = 100 - 80 * math.sin(rad)
    arc_bg = f'<path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="#e5e7eb" stroke-width="14" stroke-linecap="round"/>'
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

def build_html(d, ctx, status):
    ind = ctx["indicators"]; comp = ctx["composite"]
    cz_name, cz_color, cz_desc = current_zone(d)
    rec = reconcile_text(d, ctx, cz_name, ctx["gold_pct"])

    def card(name, val, score, color, note):
        return (f'<div class="card"><div class="c-head"><span class="c-name">{name}</span>'
                f'<span class="c-score" style="color:{color}">{score:.0f}</span></div>'
                f'<div class="c-val">{val}</div>'
                f'<div class="bar"><div class="bar-fill" style="width:{score:.0f}%;background:{color}"></div></div>'
                f'<div class="c-note">{note}</div></div>')
    cards = [
        card("估值分位 (价格 vs 52周)", f"金价位于年内 {ctx['gold_pct']:.0f}% 分位",
             ind["valuation"], "#b45309", "越接近年内低位越便宜"),
        card("趋势 (价格/长期均线)", f"比值 {ctx['trend_ratio']:.2f}×",
             ind["trend"], "#16a34a", "不逆强下行趋势"),
        card("动量 RSI(14)", f"RSI = {d['gold_rsi']:.0f}",
             ind["rsi"], "#db2777", "超卖(低)=更好买点"),
        card("实际利率 (10Y TIPS)", f"实际收益率 {d['tips']:.2f}%",
             ind["real_yield"], "#7c3aed", "黄金核心驱动：越低越利多"),
        card("美元指数 DXY", f"DXY {d['dxy']:.1f} (分位{ctx['dxy_pct']:.0f}%)",
             ind["dxy"], "#0891b2", "美元越强越压制金价"),
        card("避险情绪 VIX", f"VIX {d['vix']:.1f} (分位{d['vix_1y_pct']:.0f}%)",
             ind["vix"], "#dc2626", "避险需求支撑金价"),
        card("季节因子", f"{MONTH_CN[ctx['month']]} 评分 {ind['season']:.0f}",
             ind["season"], "#ca8a04", SEASON_NOTE[ctx['month']]),
    ]

    # 价位区间表（中性位置描述）
    zone_rows = ""
    for name, lo, hi, desc, color in compute_zones(d):
        mark = "◀ 当前" if name == cz_name else ""
        zone_rows += (f"<tr><td><b style='color:{color}'>{name}</b></td>"
                      f"<td>{lo:.3f}</td><td>{hi:.3f}</td>"
                      f"<td style='text-align:left;font-size:12px;color:#475569'>{desc}</td>"
                      f"<td>{mark}</td></tr>")

    # 时间窗口 / 触发条件
    bull = [("美元转弱", f"DXY 跌破 98（现 {d['dxy']:.1f}）时分批买入，美元走弱利多金价"),
            ("实际利率回落", f"10Y TIPS 降至 1.5% 以下（现 {d['tips']:.2f}%）黄金持有成本下降"),
            ("避险升温", f"VIX 升至 25+（现 {d['vix']:.1f}）地缘/衰退担忧推动避险买盘"),
            ("回撤到位", f"金价回撤至长期均线({d['gold_ma']:.3f}) 或较高点 -10% 附近")]
    bear = [("逼近前高", f"金价升至 {d['gold_52w_high']*0.90:.3f}+ 且 RSI>65 超买分批止盈"),
            ("美元转强", f"DXY 突破 102（现 {d['dxy']:.1f}）压制金价"),
            ("实际利率上行", f"10Y TIPS 升破 3.0%（现 {d['tips']:.2f}%）持有成本上升"),
            ("动能转空", f"金价跌破长期均线({d['gold_ma']:.3f}) 且 RSI<35 转弱防守")]

    bull_html = "".join(f'<div class="rule rule-bull"><span class="r-kind">加仓信号</span><b>{t}</b><br>{v}</div>' for t, v in bull)
    bear_html = "".join(f'<div class="rule rule-bear"><span class="r-kind">止盈信号</span><b>{t}</b><br>{v}</div>' for t, v in bear)

    st_lines = "".join(f"<li>{k}: {v}</li>" for k, v in status.items())

    season_win = ("当前为 <b>{}</b>（季节评分 {}），属{}。<br>历史旺季在 "
                  "<b>9–11 月</b>（印度婚季与排灯节实物需求）及 <b>12–1 月</b>（春节前中国买盘）；"
                  "夏秋(6–8 月)通常偏弱，宜耐心等待回调分批布局。"
                  ).format(MONTH_CN[ctx['month']], ind['season'], SEASON_NOTE[ctx['month']])

    html_doc = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>黄金交易助手 · 报告</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif;
 background:#f1f5f9;color:#0f172a;padding:24px}}
.wrap{{max-width:1080px;margin:0 auto}}
header{{background:linear-gradient(135deg,#78350f,#b45309);color:#fff;border-radius:16px;
 padding:24px 28px;margin-bottom:20px}}
header h1{{font-size:22px;margin-bottom:6px}}
header .meta{{font-size:12px;opacity:.8}}
.signal{{display:inline-block;margin-top:12px;padding:8px 18px;border-radius:10px;
 background:{ctx['color']};color:#fff;font-size:18px;font-weight:700}}
.advice{{margin-top:12px;font-size:14px;line-height:1.6;opacity:.95}}
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
.recon{{display:flex;gap:20px;align-items:stretch;background:linear-gradient(135deg,#fff7ed,#f8fafc);
 border:2px solid {cz_color};border-radius:14px;padding:18px;margin-bottom:20px;flex-wrap:wrap}}
.recon .r-col{{flex:1;min-width:200px}}
.recon .r-label{{font-size:12px;color:#64748b;margin-bottom:4px}}
.recon .r-val{{font-size:18px;font-weight:800}}
.recon .r-zone{{color:{cz_color}}}
.recon .r-text{{flex:2 1 100%;font-size:13px;line-height:1.7;color:#334155;margin-top:14px;
 border-top:1px dashed #fcd9b6;padding-top:12px}}
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
  <h1>🪙 黄金交易助手</h1>
  <div class="meta">数据时间：{d.get('as_of','')} ｜ 标的：518850 黄金ETF华夏（A股可直接交易，与现货金价高度联动）｜ 框架：7 指标加权打分 + 价位区间 + 时间窗口（直接买入现货 ETF）</div>
  <div class="signal">{ctx['signal']}　综合评分 {comp:.0f}/100</div>
  <div class="advice">{ctx['advice']}</div>
</header>

<div class="comp-wrap">
  <div class="comp-gauge">{gauge(comp, ctx['color'], "综合评分", ctx['signal'])}</div>
  <div style="flex:1">
    <h2 style="margin-bottom:10px;font-size:15px">指标仪表盘（分数越高 = 越适合买入/累积黄金）</h2>
    <div class="cards" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">
      {''.join(gauge(ind[k], '#b45309', n, '') for k,n in
        [('valuation','估值'),('trend','趋势'),('rsi','RSI'),('real_yield','实际利率'),
         ('dxy','美元'),('vix','避险'),('season','季节')])}
    </div>
  </div>
</div>

<div class="recon">
  <div class="r-col">
    <div class="r-label">📍 技术面价位（价格相对 52 周区间）</div>
    <div class="r-val r-zone">{cz_name}</div>
    <div style="font-size:12px;color:#64748b;margin-top:4px">现价 {d['gold_price']:.3f} ｜ 52 周区间分位 {ctx['gold_pct']:.0f}%</div>
  </div>
  <div class="r-col">
    <div class="r-label">🎯 综合信号（全页唯一结论）</div>
    <div class="r-val" style="color:{ctx['color']}">{ctx['signal']}</div>
    <div style="font-size:12px;color:#64748b;margin-top:4px">综合评分 {comp:.0f}/100</div>
  </div>
  <div class="r-text">{rec}</div>
</div>

<div class="section"><h2>① 七维指标明细</h2><div class="cards">{''.join(cards)}</div></div>

<div class="section"><h2>② 价位区间参考（仅标示价格相对 52 周区间的位置，安全边际参考）</h2>
  <table><thead><tr>
    <th>价位区间</th><th>下沿</th><th>上沿</th><th>位置描述</th><th>状态</th>
  </tr></thead><tbody>{zone_rows}</tbody></table>
  <p style="font-size:12px;color:#64748b;margin-top:10px">
  区间基于 518850 的 52 周高低({d['gold_52w_low']:.3f}–{d['gold_52w_high']:.3f}) 按百分比切分，颜色仅表示"低=绿 / 高=红"。
  此表为<b>安全边际参考</b>，不单独下买卖命令；具体买入/卖出操作一律以上方「综合信号」为准。
  黄金长期保值属性强，减仓区宜"分批减/留底仓"，不必清仓。建议分批操作。</p>
</div>

<div class="section"><h2>③ 时间窗口与触发条件（何时加仓 / 何时止盈）</h2>
  <div class="recon" style="border-color:#ca8a04;background:linear-gradient(135deg,#fffbeb,#f8fafc);margin-bottom:14px">
    <div class="r-col" style="flex:2 1 100%">
      <div class="r-label">🗓️ 季节窗口</div>
      <div class="r-text" style="border-top:none;padding-top:0;margin-top:6px">{season_win}</div>
    </div>
  </div>
  <div class="rules">
    <div><div style="font-weight:700;margin-bottom:8px;color:#16a34a">加仓信号</div>{bull_html}</div>
    <div><div style="font-weight:700;margin-bottom:8px;color:#dc2626">止盈信号</div>{bear_html}</div>
  </div>
  <p style="font-size:12px;color:#64748b;margin-top:12px">
  核心逻辑：低估值+弱美元+低实际利率+避险升温时"买在恐惧"；逼近前高+超买+强美元时"分批止盈"。
  黄金宜作为长期保值配置、定投为主，择时仅用于加减仓，避免频繁全进全出。</p>
</div>

<div class="section"><h2>④ 数据来源与新鲜度</h2>
  <ul class="status">{st_lines}</ul>
  <p style="font-size:12px;color:#94a3b8;margin-top:8px">
  ETF 价格/52周/均线/RSI：东方财富(518850, 不复权)，经 --manual 刷新；
  宏观驱动：Yahoo Finance (美元指数 DXY / VIX) + FRED (10Y TIPS 实际利率 DFII10)，实时抓取失败回退快照。
  若标注"回退-快照"表示实时抓取失败，请以实时数据为准。</p>
</div>

<footer>
  黄金交易助手 · 仅供研究参考，非投资建议<br>
  生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} ·
  <a href="{HOMEPAGE_URL}" style="color:#b45309">返回工具箱首页</a> ·
  <a href="{FEEDBACK_URL}" style="color:#b45309">反馈/建议</a>
</footer>
</div></body></html>'''
    return html_doc

# ----------------------------------------------------------------------------
# 7. 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="黄金交易助手 (Gold Trading Assistant)")
    ap.add_argument("--offline", action="store_true", help="强制使用内置快照")
    ap.add_argument("--fred-key", default=None, help="FRED API Key")
    ap.add_argument("--manual", default=None, help="手动覆盖JSON(覆盖SNAPSHOT字段, 含 gold_*)")
    ap.add_argument("--out", default=None, help="HTML 输出路径")
    args = ap.parse_args()

    offline = args.offline
    if args.manual:
        try:
            over = json.loads(args.manual)
            SNAPSHOT.update(over)
            offline = True
        except Exception as e:
            print("manual JSON 解析失败:", e)

    print("=" * 60)
    print("  黄金交易助手  ·  拉取市场数据 ...")
    print("=" * 60)
    d, status = fetch_market_data(fred_key=args.fred_key, offline=offline)

    ctx = analyze(d)
    cz_name, _, _ = current_zone(d)

    print(f"\n  数据时间      : {d.get('as_of','')}")
    print(f"  518850 现价   : {d['gold_price']:.3f}  (52周 {d['gold_52w_low']:.3f}-{d['gold_52w_high']:.3f}, 分位 {ctx['gold_pct']:.0f}%)")
    print(f"  长期均线      : {d['gold_ma']:.3f}  → 价格/MA = {ctx['trend_ratio']:.2f}×   RSI {d['gold_rsi']:.0f}")
    print(f"  DXY           : {d['dxy']:.1f}  (分位 {ctx['dxy_pct']:.0f}%)")
    print(f"  10Y TIPS      : {d['tips']:.2f}%   VIX {d['vix']:.1f} (分位 {d['vix_1y_pct']:.0f}%)")
    print(f"  当前区间      : {cz_name}")
    print("-" * 60)
    print("  指标评分(0-100, 越高越适合买入):")
    names = {"valuation":"估值分位","trend":"趋势","rsi":"RSI动量","real_yield":"实际利率",
             "dxy":"美元指数","vix":"避险VIX","season":"季节因子"}
    for k in WEIGHTS:
        print(f"    {names[k]:<10} {ctx['indicators'][k]:5.1f}   (权重{WEIGHTS[k]})")
    print("-" * 60)
    print(f"  ★ 综合评分    : {ctx['composite']:.1f} / 100")
    print(f"  ★ 行动信号    : {ctx['signal']}")
    print(f"  ★ 建议        : {ctx['advice']}")
    print("=" * 60)

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "gold_trading_report.html")
    html_doc = build_html(d, ctx, status)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"  ✅ 报告已生成: {out}\n")

if __name__ == "__main__":
    main()
