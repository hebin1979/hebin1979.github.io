#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纳指交易助手 (Nasdaq Trading Assistant)
========================================================
目的：综合多个市场指标，判断"现在是否适合买入纳指"，并给出
      合适的价位区间与时间窗口。标的为可在 A 股直接交易的
      159696 纳指ETF易方达（跟踪纳斯达克100指数，直接买入现货 ETF，非期权）。

指标框架（7 个，权重合计 100）—— 分数越高 = 越适合"买入/加仓"纳指：
  1. 估值分位 (ETF价格 vs 52周区间) 权重 16  —— 越接近年内低位越便宜
  2. 趋势 (价格/长期均线)          权重 16  —— 站上长期均线=顺势，破位=防守
  3. 动量 RSI(14)                 权重 16  —— 超卖(低) = 更好的买点
  4. 股票风险溢价 ERP             权重 18  —— 纳指盈利收益率−10Y，越高越有吸引力
  5. 波动率环境 VIX               权重 14  —— 恐慌(高)常是长线买点(买在恐惧)
  6. 高收益信用利差 HY OAS        权重 12  —— 越低=风险偏好健康
  7. VIX 期限结构                 权重 8   —— Contango 健康 / Backwardation 恐慌

评分机制（两层）：价格(估值分位)锚定买卖方向占 60%，其余 6 指标作为环境分调制力度占 40%，
  故价位区间(便宜/贵)与综合信号方向必然一致，环境只决定买多/卖少的力度。
综合评分 -> 唯一行动信号（双向）：
  >=68  强烈买入（分批建仓/加仓）
  55-67 条件合适·可小仓位建仓
  45-54 中性·观望（可极少量试仓）
  35-44 估值偏高·持有为主，考虑部分止盈
  <35   高估过热/趋势破位·建议减仓

重要约定（避免上下矛盾，同红利助手）：
  - 顶部「综合信号」是全页唯一结论，买卖动作只由此给出。
  - ②「价位区间参考」仅描述"ETF价格相对 52 周区间的位置"（安全边际参考），
    使用中性标签（深度价值区/价值区/合理区/偏高区/高估区），不下买卖命令。
  - 新增「当前定位与结论」卡片，显式说明技术面价位与综合信号的关系。

数据来源：
  - ETF 价格/52周/均线/RSI：东方财富(159696, secid=0.159696, fqt=0 不复权)，经 --manual 刷新。
  - 宏观驱动：Yahoo Finance (VIX/期限结构/10Y) + FRED (HY OAS)，本机直连通常可用，
    失败则回退快照并标注。
用法：
  python qqq_put_assistant.py --manual '{"etf_price":2.02,"etf_52w_high":2.168,...}' --out index.html
  python qqq_put_assistant.py --offline      # 全部用快照
  (注：以 159696 纳指ETF 为可交易标的，直接买入现货 ETF)
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import realtime_js  # 浏览器端实时行情脚本（共享）

try:
    import requests
except ImportError:
    requests = None

# ----------------------------------------------------------------------------
# 0. 内置快照 (真实数据校准, 截至 2026-07-15；etf_* 来自东财 159696 不复权)
# ----------------------------------------------------------------------------
SNAPSHOT = {
    "as_of": "2026-07-20",
    # —— ETF 自身价格技术面（东财 159696, fqt=0 不复权）——
    "etf_price": 1.944, "etf_52w_high": 2.168, "etf_52w_low": 1.505,
    "etf_ma": 1.785, "etf_rsi": 29.6,
    "ey": 3.0,            # 纳指100 盈利收益率(%) 用于 ERP（宏观驱动，非 ETF 价格）
    # —— 宏观驱动（美国市场，Yahoo/FRED 实时抓取，失败回退）——
    "vix": 16.25, "vix_52w_high": 35.30, "vix_52w_low": 13.38, "vix_1y_pct": 13.1,
    "vix3m": 17.5, "vix6m": 18.5,
    "tnx": 45.73,         # CBOE 10Y 收益率指数(值=收益率*10) → 10Y=4.573%
    "hy_oas": 270.0,      # 高收益债利差(bps)
}

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qqq_cache.json")

FEEDBACK_URL = "https://github.com/hebin1979/hebin1979.github.io/issues"
HOMEPAGE_URL = "https://hebin1979.github.io/"

# ----------------------------------------------------------------------------
# 1. 数据抓取层（仅宏观驱动；ETF 价格走 SNAPSHOT/--manual）
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
    """返回 (data_dict, status_dict)。etf_* 始终来自 SNAPSHOT（东财口径，手动刷新）；
       宏观驱动(VIX/期限/10Y/HY OAS) 尝试 Yahoo/FRED 实时抓取，失败回退快照。"""
    status = {}
    d = dict(SNAPSHOT)
    for k in ("etf_price","etf_52w_high","etf_52w_low","etf_ma","etf_rsi","ey"):
        status[k] = "快照/手动"

    if offline or requests is None:
        for k in ("vix","vix_52w_high","vix_52w_low","vix_1y_pct","vix3m","vix6m","tnx","hy_oas"):
            status[k] = "快照/离线"
        d["as_of"] = datetime.now().strftime("%Y-%m-%d") + "(快照/离线)"
        return d, status

    s = _session()
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
        for k in ("vix","vix_52w_high","vix_52w_low","vix_1y_pct"):
            status[k] = "回退-快照"

    # ---- VIX 期限结构 ----
    for sym, key in (("^VIX3M", "vix3m"), ("^VIX6M", "vix6m")):
        try:
            r = _yf_chart(sym, "5d", "1d", s)
            d[key] = r["meta"]["regularMarketPrice"] if r else SNAPSHOT[key]
            status[key] = "实时(Yahoo)" if r else "回退-快照"
        except Exception:
            d[key] = SNAPSHOT[key]; status[key] = "回退-快照"

    # ---- 10Y 国债 (^TNX = 收益率*10) ----
    try:
        r = _yf_chart("^TNX", "5d", "1d", s)
        d["tnx"] = r["meta"]["regularMarketPrice"] if r else SNAPSHOT["tnx"]
        status["tnx"] = "实时(Yahoo)" if r else "回退-快照"
    except Exception:
        d["tnx"] = SNAPSHOT["tnx"]; status["tnx"] = "回退-快照"

    # ---- HY OAS (FRED) ----
    try:
        oas = _fred_obs("BAMLH0A0HYM2", fred_key, s)
        d["hy_oas"] = oas if oas else SNAPSHOT["hy_oas"]
        status["hy_oas"] = "实时(FRED)" if oas else "回退-快照"
    except Exception:
        d["hy_oas"] = SNAPSHOT["hy_oas"]; status["hy_oas"] = "回退-快照"

    d["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"data": d, "status": status, "ts": time.time()}, f, ensure_ascii=False, indent=2)
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
# 3. 指标打分 (每个返回 0-100, 越高越适合买入/加仓纳指)
# ----------------------------------------------------------------------------
def score_valuation(etf_pct):
    return lerp([(0,95),(20,80),(40,60),(60,45),(80,28),(100,15)], etf_pct)

def score_trend(ratio):
    return lerp([(0.85,15),(0.92,32),(0.98,55),(1.0,65),(1.05,80),(1.12,90)], ratio)

def score_rsi(rsi):
    return lerp([(20,95),(30,88),(45,72),(55,58),(65,42),(70,28),(80,15)], rsi)

def score_erp(erp):
    return lerp([(-2,15),(-1,30),(0,45),(1,58),(2,72),(3,82),(4,92)], erp)

def score_vix(vix_pct):
    return lerp([(0,32),(20,40),(50,60),(80,82),(100,92)], vix_pct)

def score_hy_oas(oas):
    return lerp([(250,88),(350,70),(500,45),(700,25),(1000,10)], oas)

def score_term_structure(slope):
    return lerp([(0.90,18),(0.95,35),(1.0,55),(1.05,70),(1.15,88)], slope)

# ----------------------------------------------------------------------------
# 4. 综合分析
# ----------------------------------------------------------------------------
WEIGHTS = {
    "valuation": 16, "trend": 16, "rsi": 16, "erp": 18,
    "vix": 14, "hy": 12, "term": 8,
}

def analyze(d):
    vix = d["vix"]; vix_pct = d["vix_1y_pct"]
    slope = d["vix3m"] / vix if vix > 0 else 1.0
    treasury = d["tnx"] / 10.0
    ey = d["ey"]
    erp = ey - treasury
    trend_ratio = d["etf_price"] / d["etf_ma"] if d["etf_ma"] > 0 else 1.0
    etf_pct = pct_of(d["etf_price"], d["etf_52w_low"], d["etf_52w_high"])
    dist_high = d["etf_price"] / d["etf_52w_high"] if d["etf_52w_high"] > 0 else 1.0

    ind = {
        "valuation": score_valuation(etf_pct),
        "trend": score_trend(trend_ratio),
        "rsi": score_rsi(d["etf_rsi"]),
        "erp": score_erp(erp),
        "vix": score_vix(vix_pct),
        "hy": score_hy_oas(d["hy_oas"]),
        "term": score_term_structure(slope),
    }
    # —— 两层框架：价格(估值分位)锚定买卖方向(60%)，其余6指标为环境分调制力度(40%) ——
    # 保证"便宜→偏多 / 贵→偏谨慎"的方向与价位区间一致，宏观/趋势只调节买多卖少的力度。
    anchor = ind["valuation"]
    env_w = {k: w for k, w in WEIGHTS.items() if k != "valuation"}
    env = sum(env_w[k] * ind[k] for k in env_w) / sum(env_w.values())
    composite = 0.6 * anchor + 0.4 * env

    if composite >= 68:
        signal = "强烈买入 · 分批建仓"; color = "#16a34a"
        advice = "多项指标共振利多，纳指处于低估/回撤区。建议分批建仓，急跌至强支撑可加仓。"
    elif composite >= 55:
        signal = "条件合适 · 可小仓位建仓"; color = "#65a30d"
        advice = "环境偏友好但非极致。建议先小仓位(1/3)建仓，待 VIX 抬升或回撤至长期均线附近再加码。"
    elif composite >= 45:
        signal = "中性 · 观望"; color = "#d97706"
        advice = "估值偏高、波动率低、权益吸引力一般。可极少量定投，主仓等待 5-10% 回撤或恐慌放大。"
    elif composite >= 35:
        signal = "估值偏高 · 持有为主"; color = "#ea580c"
        advice = "纳指偏贵或动能转弱。持有现有仓位，逼近前高分批止盈，不宜追高。"
    else:
        signal = "高估过热/破位 · 建议减仓"; color = "#dc2626"
        advice = "估值高企且动能过热，或已跌破长期均线趋势转弱。建议减仓防守，等待企稳/回调。"

    ctx = dict(
        vix=vix, vix_pct=vix_pct, slope=slope, treasury=treasury, ey=ey, erp=erp,
        trend_ratio=trend_ratio, etf_pct=etf_pct, dist_high=dist_high,
        indicators=ind, composite=composite, env=env, env_fav=(env >= 50),
        signal=signal, color=color, advice=advice,
    )
    return ctx

# ----------------------------------------------------------------------------
# 5. 价位区间参考（中性位置描述，仅标示 ETF 价格相对 52 周区间的位置）
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

def reconcile_text(d, ctx, zone_name, etf_pct):
    """两层框架的桥梁：价格(估值分位)锚定买卖方向，其余指标(环境分)只调制力度。
    方向必与价位区间一致：便宜→偏多，贵→偏谨慎；环境好则加大力度，环境差则减小。"""
    comp = ctx["composite"]; sig = ctx["signal"]; ind = ctx["indicators"]; env = ctx["env"]
    cheap = etf_pct < 40
    pos = ("处于年内偏低位置、安全边际高" if cheap else
           "处于历史中枢附近" if etf_pct < 65 else
           "处于年内偏高位置、安全边际有限")
    bear = []
    if ctx["erp"] < 1.0: bear.append(f"股票风险溢价偏低(ERP {ctx['erp']:+.1f}%)")
    bull = []
    if ctx["trend_ratio"] > 1.05: bull.append(f"趋势站上长期均线({ctx['trend_ratio']:.2f}×) 顺势")
    if d["hy_oas"] < 320: bull.append(f"信用利差健康(HY OAS {d['hy_oas']:.0f} bps)")
    if ctx["slope"] > 1.05: bull.append(f"VIX 期限结构良性(斜率 {ctx['slope']:.2f})")
    if cheap:
        tail = ("；".join(bear) + "，故小仓位分批、不一次性满仓") if bear else "，可直接小仓位分批布局"
        return (f"当前 159696 现价 {d['etf_price']:.3f}，价位「{zone_name}」{pos}（52 周区间分位 {etf_pct:.0f}%）。"
                f"价格处低位→方向偏多；综合评分 {comp:.0f}/100 得出信号「{sig}」。本体系以价格(估值分位)锚定方向、"
                f"其余指标(环境分 {env:.0f})调制力度：虽便宜可逢低布局，但{tail}。")
    tail = ("；".join(bull) + "，但价格已偏高、安全边际有限，故以持有/观望为主、不追高") if bull else "，且价格偏高、安全边际有限，故以持有/观望为主、不追高"
    return (f"当前 159696 现价 {d['etf_price']:.3f}，价位「{zone_name}」{pos}（52 周区间分位 {etf_pct:.0f}%）。"
            f"价格偏高→方向偏谨慎；综合评分 {comp:.0f}/100 得出信号「{sig}」。本体系以价格锚定方向、"
            f"其余指标(环境分 {env:.0f})调制力度：{tail}。")

# ----------------------------------------------------------------------------
# 6. HTML 报告
# ----------------------------------------------------------------------------
def gauge(value, color, label, sub="", rt_tag=None):
    import html
    angle = 180 - (value / 100.0) * 180
    rad = math.radians(angle)
    x = 100 + 80 * math.cos(rad)
    y = 100 - 80 * math.sin(rad)
    arc_bg = f'<path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="#e5e7eb" stroke-width="14" stroke-linecap="round"/>'
    # rt_tag 用于标记"需要随实时价刷新的仪表盘"（综合/估值/趋势），供 JS 定位
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

def build_html(d, ctx, status):
    ind = ctx["indicators"]; comp = ctx["composite"]
    cz_name, cz_color, cz_desc = current_zone(d)
    rec = reconcile_text(d, ctx, cz_name, ctx["etf_pct"])

    def card(name, val, score, color, note, cid=None):
        cls = f'card rt-card-{cid}' if cid else 'card'
        return (f'<div class="{cls}"><div class="c-head"><span class="c-name">{name}</span>'
                f'<span class="c-score" style="color:{color}">{score:.0f}</span></div>'
                f'<div class="c-val">{val}</div>'
                f'<div class="bar"><div class="bar-fill" style="width:{score:.0f}%;background:{color}"></div></div>'
                f'<div class="c-note">{note}</div></div>')
    cards = [
        card("估值分位 (价格 vs 52周)", f"纳指位于年内 {ctx['etf_pct']:.0f}% 分位",
             ind["valuation"], "#b45309", "越接近年内低位越便宜", cid='valuation'),
        card("趋势 (价格/长期均线)", f"比值 {ctx['trend_ratio']:.2f}×",
             ind["trend"], "#16a34a", "站上均线顺势，破位防守", cid='trend'),
        card("动量 RSI(14)", f"RSI = {d['etf_rsi']:.0f}",
             ind["rsi"], "#db2777", "超卖(低)=更好买点"),
        card("股票风险溢价 (ERP)", f"ERP {ctx['erp']:+.1f}% (盈利率{ctx['ey']:.1f}%−10Y {ctx['treasury']:.1f}%)",
             ind["erp"], "#7c3aed", "股票相对债券的吸引力"),
        card("波动率环境 (VIX)", f"VIX {d['vix']:.1f} (分位{ctx['vix_pct']:.0f}%)",
             ind["vix"], "#2563eb", "恐慌高位常是长线买点"),
        card("高收益信用利差", f"HY OAS {d['hy_oas']:.0f} bps",
             ind["hy"], "#ea580c", "越低=风险偏好越健康"),
        card("VIX 期限结构", f"斜率 {ctx['slope']:.2f} (VIX3M/VIX)",
             ind["term"], "#0891b2", "Contango 健康 / Back 恐慌"),
    ]

    zone_rows = ""
    for name, lo, hi, desc, color in compute_zones(d):
        mark = "◀ 当前" if name == cz_name else ""
        zone_rows += (f"<tr><td><b style='color:{color}'>{name}</b></td>"
                      f"<td>{lo:.3f}</td><td>{hi:.3f}</td>"
                      f"<td style='text-align:left;font-size:12px;color:#475569'>{desc}</td>"
                      f"<td>{mark}</td></tr>")

    bull = [("恐慌抄底", f"VIX 飙升破 25-30（现 {d['vix']:.1f}）时分批买入，买在恐惧"),
            ("回撤到位", f"纳指回撤至长期均线({d['etf_ma']:.3f}) 或较高点 -10% 附近"),
            ("动量超卖", f"RSI 跌破 35（现 {d['etf_rsi']:.0f}）出现均值回归机会"),
            ("信用healthy", f"HY OAS 维持 300bps 下方（现 {d['hy_oas']:.0f}）风险偏好稳")]
    bear = [("逼近前高", f"纳指升至 {d['etf_52w_high']*0.98:.3f}+ 且 RSI>70 超买"),
            ("低波高估", f"VIX<13 且 估值分位>85%（现分位 {ctx['etf_pct']:.0f}%）追高风险"),
            ("趋势破位", f"纳指跌破长期均线({d['etf_ma']:.3f}) 且 RSI<40 转弱防守"),
            ("信用走阔", f"HY OAS 升破 500bps（现 {d['hy_oas']:.0f}）系统性风险上升")]

    bull_html = "".join(f'<div class="rule rule-bull"><span class="r-kind">加仓信号</span><b>{t}</b><br>{v}</div>' for t, v in bull)
    bear_html = "".join(f'<div class="rule rule-bear"><span class="r-kind">止盈信号</span><b>{t}</b><br>{v}</div>' for t, v in bear)

    st_lines = "".join(f"<li>{k}: {v}</li>" for k, v in status.items())

    html_doc = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>纳指交易助手 · 报告</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif;
 background:#f1f5f9;color:#0f172a;padding:24px}}
.wrap{{max-width:1080px;margin:0 auto}}
header{{background:linear-gradient(135deg,#1e293b,#334155);color:#fff;border-radius:16px;
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
.recon{{display:flex;gap:20px;align-items:stretch;background:linear-gradient(135deg,#eff6ff,#f8fafc);
 border:2px solid {cz_color};border-radius:14px;padding:18px;margin-bottom:20px;flex-wrap:wrap}}
.recon .r-col{{flex:1;min-width:200px}}
.recon .r-label{{font-size:12px;color:#64748b;margin-bottom:4px}}
.recon .r-val{{font-size:18px;font-weight:800}}
.recon .r-zone{{color:{cz_color}}}
.recon .r-text{{flex:2 1 100%;font-size:13px;line-height:1.7;color:#334155;margin-top:14px;
 border-top:1px dashed #c7d2fe;padding-top:12px}}
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
}}
</style></head><body><div class="wrap">
<header>
  <h1>📈 纳指交易助手</h1>
  <div class="meta">数据时间：{d.get('as_of','')} ｜ 标的：159696 纳指ETF易方达（跟踪纳斯达克100，A股可直接交易）｜ 框架：价格锚定方向+环境调制力度 + 价位区间 + 时间窗口（直接买入现货 ETF）</div>
  <div class="signal">{ctx['signal']}　综合评分 {comp:.0f}/100</div>
  <div class="advice">{ctx['advice']}</div>
  <div id="dataStamp" class="rt-stamp">⏳ 正在获取实时行情…</div>
</header>

<div class="comp-wrap">
  <div class="comp-gauge">{gauge(comp, ctx['color'], "综合评分", ctx['signal'], rt_tag='comp')}</div>
  <div style="flex:1">
    <h2 style="margin-bottom:10px;font-size:15px">指标仪表盘（分数越高 = 越适合买入/加仓纳指）</h2>
    <div class="cards" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">
      {''.join(gauge(ind[k], '#2563eb', n, '', rt_tag=('valuation' if k=='valuation' else ('trend' if k=='trend' else None))) for k,n in
        [('valuation','估值'),('trend','趋势'),('rsi','RSI'),('erp','ERP'),
         ('vix','VIX'),('hy','信用利差'),('term','期限结构')])}
    </div>
  </div>
</div>

<div class="recon">
  <div class="r-col">
    <div class="r-label">📍 技术面价位（价格相对 52 周区间）</div>
    <div class="r-val r-zone">{cz_name}</div>
    <div style="font-size:12px;color:#64748b;margin-top:4px" class="rt-recon-price">现价 {d['etf_price']:.3f} ｜ 52 周区间分位 {ctx['etf_pct']:.0f}%</div>
  </div>
  <div class="r-col">
    <div class="r-label">🎯 综合信号（全页唯一结论）</div>
    <div class="r-val rt-signal" style="color:{ctx['color']}">{ctx['signal']}</div>
    <div style="font-size:12px;color:#64748b;margin-top:4px" class="rt-comp-score">综合评分 {comp:.0f}/100</div>
  </div>
  <div class="r-text">{rec}</div>
</div>

<div class="section"><h2>① 七维指标明细</h2><div class="cards">{''.join(cards)}</div></div>

<div class="section"><h2>② 价位区间参考（仅标示价格相对 52 周区间的位置，安全边际参考）</h2>
  <table><thead><tr>
    <th>价位区间</th><th>下沿</th><th>上沿</th><th>位置描述</th><th>状态</th>
  </tr></thead><tbody>{zone_rows}</tbody></table>
  <p style="font-size:12px;color:#64748b;margin-top:10px">
  区间基于 159696 的 52 周高低({d['etf_52w_low']:.3f}–{d['etf_52w_high']:.3f}) 按百分比切分，颜色仅表示"低=绿 / 高=红"。
  此表为<b>安全边际参考</b>，不单独下买卖命令；具体买入/卖出操作一律以上方「综合信号」为准。
  纳指长期向上，减仓区宜"分批减/留底仓"，不必清仓。建议分批操作。</p>
</div>

<div class="section"><h2>③ 时间窗口与触发条件（何时加仓 / 何时止盈）</h2>
  <div class="rules">
    <div><div style="font-weight:700;margin-bottom:8px;color:#16a34a">加仓信号</div>{bull_html}</div>
    <div><div style="font-weight:700;margin-bottom:8px;color:#dc2626">止盈信号</div>{bear_html}</div>
  </div>
  <p style="font-size:12px;color:#64748b;margin-top:12px">
  核心逻辑：低估值+高恐慌+高 ERP 时"买在恐惧"；逼近前高+超买+低波动时"分批止盈"。
  纳指宜长线持有、定投为主，择时仅用于加减仓，避免频繁全进全出。</p>
</div>

<div class="section"><h2>④ 数据来源与新鲜度</h2>
  <ul class="status">{st_lines}</ul>
  <p style="font-size:12px;color:#94a3b8;margin-top:8px">
  ETF 价格/52周/均线/RSI：东方财富(159696, 不复权)，经 --manual 刷新；
  宏观驱动：Yahoo Finance (VIX/期限结构/10Y) + FRED (HY OAS)，实时抓取失败回退快照。
  若标注"回退-快照"表示实时抓取失败，请以实时数据为准。</p>
</div>

<footer>
  纳指交易助手 · 仅供研究参考，非投资建议<br>
  生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} ·
  <a href="{HOMEPAGE_URL}" style="color:#2563eb">返回工具箱首页</a> ·
  <a href="{FEEDBACK_URL}" style="color:#2563eb">反馈/建议</a>
</footer>
</div>'''
    # —— 浏览器端实时行情：拉取实时价并刷新价格驱动评分（环境分沿用快照）——
    rt_cfg = {
        "kind": "nasdaq",
        "funds": [{
            "scope": "", "secid": "0.159696", "gtimg": "sz159696", "name": "159696 纳指ETF易方达",
            "lo": round(float(d["etf_52w_low"]), 4), "hi": round(float(d["etf_52w_high"]), 4), "ma": round(float(d["etf_ma"]), 4),
            "env": {"trend": round(float(ind["trend"]), 1), "rsi": round(float(ind["rsi"]), 1), "erp": round(float(ind["erp"]), 1), "vix": round(float(ind["vix"]), 1), "hy": round(float(ind["hy"]), 1), "term": round(float(ind["term"]), 1)},
            "weights": {"valuation": 16, "trend": 16, "rsi": 16, "erp": 18, "vix": 14, "hy": 12, "term": 8},
            "anchors": {"val": [[0,95],[20,80],[40,60],[60,45],[80,28],[100,15]], "trend": [[0.85,15],[0.92,32],[0.98,55],[1.0,65],[1.05,80],[1.12,90]]},
            "cardVal": "valuation", "hdrSignal": ".signal"
        }]
    }
    rt_cfg_json = json.dumps(rt_cfg, ensure_ascii=False)
    html_doc = html_doc + '<script>var RT_CFG=' + rt_cfg_json + ';</script>\n<script>' + realtime_js.REALTIME_JS + '</script>\n</body></html>'
    return html_doc

# ----------------------------------------------------------------------------
# 7. 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="纳指交易助手 (Nasdaq Trading Assistant)")
    ap.add_argument("--offline", action="store_true", help="强制使用内置快照")
    ap.add_argument("--fred-key", default=None, help="FRED API Key")
    ap.add_argument("--manual", default=None, help="手动覆盖JSON(覆盖SNAPSHOT字段, 含 etf_*)")
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
    print("  纳指交易助手  ·  拉取市场数据 ...")
    print("=" * 60)
    d, status = fetch_market_data(fred_key=args.fred_key, offline=offline)

    ctx = analyze(d)
    cz_name, _, _ = current_zone(d)

    print(f"\n  数据时间      : {d.get('as_of','')}")
    print(f"  159696 现价   : {d['etf_price']:.3f}  (52周 {d['etf_52w_low']:.3f}-{d['etf_52w_high']:.3f}, 分位 {ctx['etf_pct']:.0f}%)")
    print(f"  长期均线      : {d['etf_ma']:.3f}  → 价格/MA = {ctx['trend_ratio']:.2f}×   RSI {d['etf_rsi']:.0f}")
    print(f"  VIX           : {ctx['vix']:.2f}  (1年分位 {ctx['vix_pct']:.0f}%)   期限斜率 {ctx['slope']:.2f}")
    print(f"  10Y 国债      : {ctx['treasury']:.2f}%   盈利收益率 {ctx['ey']:.2f}%  → ERP {ctx['erp']:+.2f}%   HY OAS {d['hy_oas']:.0f}bps")
    print(f"  当前区间      : {cz_name}")
    print("-" * 60)
    print("  指标评分(0-100, 越高越适合买入):")
    names = {"valuation":"估值分位","trend":"趋势","rsi":"RSI动量","erp":"股票风险溢价",
             "vix":"波动率VIX","hy":"信用利差","term":"VIX期限结构"}
    for k in WEIGHTS:
        print(f"    {names[k]:<12} {ctx['indicators'][k]:5.1f}   (权重{WEIGHTS[k]})")
    print("-" * 60)
    print(f"  ★ 综合评分    : {ctx['composite']:.1f} / 100")
    print(f"  ★ 行动信号    : {ctx['signal']}")
    print(f"  ★ 建议        : {ctx['advice']}")
    print("=" * 60)

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "nasdaq_trading_report.html")
    html_doc = build_html(d, ctx, status)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"  ✅ 报告已生成: {out}\n")

if __name__ == "__main__":
    main()
