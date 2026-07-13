#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纳指交易助手 (Nasdaq / QQQ Trading Assistant)
========================================================
目的：综合多个市场指标，判断"现在是否适合买入纳指(QQQ)"，并给出
      合适的买入/卖出价位区间与最佳时间窗口（直接买入现货 ETF，非期权）。

指标框架（7 个，权重合计 100）—— 分数越高 = 越适合"买入/加仓"纳指：
  1. 估值分位 (价格 vs 52周区间)  权重 16  —— 越接近年内低位越便宜
  2. 趋势 (价格/200MA)            权重 16  —— 站上 200 日线=顺势，破位=防守
  3. 动量 RSI(14)                 权重 16  —— 超卖(低) = 更好的买点
  4. 股票风险溢价 ERP             权重 18  —— 盈利收益率−10Y，越高越有吸引力
  5. 波动率环境 VIX               权重 14  —— 恐慌(高)常是长线买点(买在恐惧)
  6. 高收益信用利差 HY OAS        权重 12  —— 越低=风险偏好健康
  7. VIX 期限结构                 权重 8   —— Contango 健康 / Backwardation 恐慌

综合评分 -> 行动信号（双向）：
  >=68  强烈买入（分批建仓/加仓）
  55-67 条件合适·可小仓位建仓
  45-54 中性·观望（可极少量试仓）
  35-44 估值偏高·持有为主，考虑部分止盈
  <35   高估过热/趋势破位·建议减仓

数据来源：Yahoo Finance (VIX/QQQ/VIX期限结构/10Y) + FRED (HY OAS)
         本机直连 Yahoo/FRED 通常可用；若抓取失败自动回退到缓存/快照并标注。

用法：
  python qqq_put_assistant.py                # 拉取实时数据并生成报告
  python qqq_put_assistant.py --offline      # 强制使用内置快照(演示/离线)
  python qqq_put_assistant.py --fred-key X  # 提供 FRED API Key(获取 HY OAS)
  python qqq_put_assistant.py --manual json # 手动覆盖关键数值(见 SNAPSHOT)
（注：本工具以纳指100 ETF QQQ 为标的，直接买入现货 ETF）
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
# 0. 内置快照 (真实数据校准, 截至 2026-07-13, 用于离线/抓取失败回退)
# ----------------------------------------------------------------------------
SNAPSHOT = {
    "as_of": "2026-07-13(快照/离线)",
    "vix": 16.25, "vix_52w_high": 35.30, "vix_52w_low": 13.38,
    "vix_1y_pct": 13.1,                      # VIX 在过去1年中的分位(手动校准)
    "qqq": 725.51, "qqq_52w_high": 748.65, "qqq_52w_low": 551.56,
    "qqq_200ma": 665.0,
    "qqq_rsi": 55.0,
    "qqq_trailing_eps": 21.76,               # 用于 ERP 计算
    "vix3m": 17.5, "vix6m": 18.5,
    "tnx": 45.73,                            # CBOE 10Y 收益率指数(值=收益率*10)
    "hy_oas": 270.0,                         # 高收益债利差(bps)
}

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qqq_cache.json")

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
        s.get("https://fc.yahoo.com", timeout=10)  # 取 consent cookie
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
    # 无 key 时尝试 CSV 直链
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
    """返回 (data_dict, status_dict)。status 标注每个字段数据来源/新鲜度。"""
    status = {}
    if offline or requests is None:
        d = dict(SNAPSHOT)
        for k in d:
            status[k] = "快照/离线"
        return d, status

    s = _session()
    d = {}
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
            d[k] = SNAPSHOT[k]
        status["vix"] = "回退-快照"

    # ---- QQQ ----
    try:
        q = _yf_chart("QQQ", "1y", "1d", s)
        if q:
            m = q["meta"]; closes = [c for c in q["indicators"]["quote"][0]["close"] if c is not None]
            d["qqq"] = m["regularMarketPrice"]
            d["qqq_52w_high"] = m.get("fiftyTwoWeekHigh", max(closes))
            d["qqq_52w_low"] = m.get("fiftyTwoWeekLow", min(closes))
            d["qqq_200ma"] = m.get("twoHundredDayAverage", sma(closes, 200))
            d["qqq_trailing_eps"] = m.get("trailingEPS") or SNAPSHOT["qqq_trailing_eps"]
            d["qqq_rsi"] = rsi(closes, 14)
            status["qqq"] = "实时(Yahoo)"
        else:
            raise ValueError("empty")
    except Exception:
        for k in ("qqq","qqq_52w_high","qqq_52w_low","qqq_200ma","qqq_trailing_eps","qqq_rsi"):
            d[k] = SNAPSHOT[k]
        status["qqq"] = "回退-快照"

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
    # 缓存
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"data": d, "status": status,
                       "ts": time.time()}, f, ensure_ascii=False, indent=2)
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
# 3. 指标打分 (每个返回 0-100, 越高越适合买入/加仓纳指)
# ----------------------------------------------------------------------------
def score_valuation(qqq_pct):
    """价格在 52周区间中的分位：越低(越便宜)分越高。"""
    return lerp([(0,95),(20,80),(40,60),(60,45),(80,28),(100,15)], qqq_pct)

def score_trend(ratio):
    """价格/200MA：站上均线=顺势健康；深跌破均线=趋势恶化。"""
    return lerp([(0.85,15),(0.92,32),(0.98,55),(1.0,65),(1.05,80),(1.12,90)], ratio)

def score_rsi(rsi):
    """RSI(14)：超卖(低)=更好买点；超买(高)=追高风险。"""
    return lerp([(20,95),(30,88),(45,72),(55,58),(65,42),(70,28),(80,15)], rsi)

def score_erp(erp):
    """股票风险溢价(百分点)=盈利收益率−10Y。越高越有吸引力。"""
    return lerp([(-2,15),(-1,30),(0,45),(1,58),(2,72),(3,82),(4,92)], erp)

def score_vix(vix_pct):
    """VIX 分位：恐慌(高分位)常是长线买点(买在恐惧)。"""
    return lerp([(0,32),(20,40),(50,60),(80,82),(100,92)], vix_pct)

def score_hy_oas(oas):
    """高收益债利差(bps)：越低=风险偏好越健康。"""
    return lerp([(250,88),(350,70),(500,45),(700,25),(1000,10)], oas)

def score_term_structure(slope):
    """VIX3M / VIX。Contango(>1)=健康; Backwardation(<1)=恐慌。"""
    return lerp([(0.90,18),(0.95,35),(1.0,55),(1.05,70),(1.15,88)], slope)

# ----------------------------------------------------------------------------
# 4. 综合评分
# ----------------------------------------------------------------------------
WEIGHTS = {
    "valuation": 16, "trend": 16, "rsi": 16, "erp": 18,
    "vix": 14, "hy": 12, "term": 8,
}

def analyze(d):
    vix = d["vix"]; vix_pct = d["vix_1y_pct"]
    slope = d["vix3m"] / vix if vix > 0 else 1.0
    treasury = d["tnx"] / 10.0
    ey = (d["qqq_trailing_eps"] / d["qqq"]) * 100.0 if d["qqq"] > 0 else 0
    erp = ey - treasury
    trend_ratio = d["qqq"] / d["qqq_200ma"] if d["qqq_200ma"] > 0 else 1.0
    qqq_pct = pct_of(d["qqq"], d["qqq_52w_low"], d["qqq_52w_high"])
    dist_high = d["qqq"] / d["qqq_52w_high"] if d["qqq_52w_high"] > 0 else 1.0

    ind = {
        "valuation": score_valuation(qqq_pct),
        "trend": score_trend(trend_ratio),
        "rsi": score_rsi(d["qqq_rsi"]),
        "erp": score_erp(erp),
        "vix": score_vix(vix_pct),
        "hy": score_hy_oas(d["hy_oas"]),
        "term": score_term_structure(slope),
    }
    composite = sum(WEIGHTS[k] * ind[k] for k in WEIGHTS) / 100.0

    if composite >= 68:
        signal = "强烈买入 · 分批建仓"
        color = "#16a34a"
        advice = "多项指标共振利多，纳指处于低估/回撤区。建议分批建仓，急跌至强支撑可加仓。"
    elif composite >= 55:
        signal = "条件合适 · 可小仓位建仓"
        color = "#65a30d"
        advice = "环境偏友好但非极致。建议先小仓位(1/3)建仓，待 VIX 抬升或回撤至 200 日线附近再加码。"
    elif composite >= 45:
        signal = "中性 · 观望"
        color = "#d97706"
        advice = "估值偏高、波动率低、权益吸引力一般。可极少量定投，主仓等待 5-10% 回撤或恐慌放大。"
    elif composite >= 35:
        signal = "估值偏高 · 持有为主"
        color = "#ea580c"
        advice = "纳指偏贵或动能转弱。持有现有仓位，逼近前高分批止盈，不宜追高。"
    else:
        signal = "高估过热/破位 · 建议减仓"
        color = "#dc2626"
        advice = "估值高企且动能过热，或已跌破 200 日线趋势转弱。建议减仓防守，等待企稳/回调。"

    ctx = dict(
        vix=vix, vix_pct=vix_pct, slope=slope, treasury=treasury, ey=ey, erp=erp,
        trend_ratio=trend_ratio, qqq_pct=qqq_pct, dist_high=dist_high,
        indicators=ind, composite=composite, signal=signal, color=color, advice=advice,
    )
    return ctx

# ----------------------------------------------------------------------------
# 5. 买入/卖出价位区间
# ----------------------------------------------------------------------------
def compute_zones(d):
    low, high, ma = d["qqq_52w_low"], d["qqq_52w_high"], d["qqq_200ma"]
    zones = [
        ("强力买入区", low + (high - low) * 0.05, ma * 0.92,
         "接近/跌破 52 周低位，长期价值凸显，可重仓分批", "#16a34a"),
        ("分批建仓区", max(low * 1.02, ma * 0.92), ma * 1.04,
         "围绕 200 日线上下，逢低累积的主战场", "#65a30d"),
        ("持有观望区", ma * 1.04, high * 0.90,
         "估值合理偏高，持有不追高、不加仓", "#d97706"),
        ("分批止盈区", high * 0.90, high * 0.98,
         "接近历史高位，开始分批减仓锁定利润", "#ea580c"),
        ("强力减仓区", high * 0.98, high * 1.02,
         "刷新/逼近历史高位且超买，仅留底仓", "#dc2626"),
    ]
    return zones

def current_zone(d):
    p = d["qqq"]
    zs = compute_zones(d)
    for name, lo, hi, desc, color in zs:
        if lo <= p <= hi:
            return name, color, desc
    if p < zs[0][1]:
        z = zs[0]; return z[0], z[4], z[3]
    z = zs[-1]; return z[0], z[4], z[3]

# ----------------------------------------------------------------------------
# 6. HTML 报告
# ----------------------------------------------------------------------------
def gauge(value, color, label, sub=""):
    """半环形仪表盘 SVG。value 0-100。"""
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
    ind = ctx["indicators"]
    comp = ctx["composite"]
    cz_name, cz_color, cz_desc = current_zone(d)

    cards = []
    def card(name, val, score, color, note):
        return (f'<div class="card"><div class="c-head"><span class="c-name">{name}</span>'
                f'<span class="c-score" style="color:{color}">{score:.0f}</span></div>'
                f'<div class="c-val">{val}</div>'
                f'<div class="bar"><div class="bar-fill" style="width:{score:.0f}%;background:{color}"></div></div>'
                f'<div class="c-note">{note}</div></div>')
    cards.append(card("估值分位 (价格 vs 52周)", f"纳指位于年内 {ctx['qqq_pct']:.0f}% 分位",
                      ind["valuation"], "#b45309", "越接近年内低位越便宜"))
    cards.append(card("趋势 (价格/200MA)", f"比值 {ctx['trend_ratio']:.2f}×",
                      ind["trend"], "#16a34a", "站上均线顺势，破位防守"))
    cards.append(card("动量 RSI(14)", f"RSI = {d['qqq_rsi']:.0f}",
                      ind["rsi"], "#db2777", "超卖(低)=更好买点"))
    cards.append(card("股票风险溢价 (ERP)", f"ERP {ctx['erp']:+.1f}% (盈利率{ctx['ey']:.1f}%−10Y {ctx['treasury']:.1f}%)",
                      ind["erp"], "#7c3aed", "股票相对债券的吸引力"))
    cards.append(card("波动率环境 (VIX)", f"VIX {d['vix']:.1f} (分位{ctx['vix_pct']:.0f}%)",
                      ind["vix"], "#2563eb", "恐慌高位常是长线买点"))
    cards.append(card("高收益信用利差", f"HY OAS {d['hy_oas']:.0f} bps",
                      ind["hy"], "#ea580c", "越低=风险偏好越健康"))
    cards.append(card("VIX 期限结构", f"斜率 {ctx['slope']:.2f} (VIX3M/VIX)",
                      ind["term"], "#0891b2", "Contango 健康 / Back 恐慌"))

    # 价位区间表
    zone_rows = ""
    for name, lo, hi, desc, color in compute_zones(d):
        mark = "◀ 当前" if name == cz_name else ""
        zone_rows += (f"<tr><td><b style='color:{color}'>{name}</b></td>"
                      f"<td>${lo:,.0f}</td><td>${hi:,.0f}</td>"
                      f"<td style='text-align:left;font-size:12px;color:#475569'>{desc}</td>"
                      f"<td>{mark}</td></tr>")

    # 时间窗口 / 触发条件
    bull = [("恐慌抄底", f"VIX 飙升破 25-30（现 {d['vix']:.1f}）时分批买入，买在恐惧"),
            ("回撤到位", f"纳指回撤至 200 日线(${d['qqq_200ma']:,.0f}) 或较高点 -10% 附近"),
            ("动量超卖", f"RSI 跌破 35（现 {d['qqq_rsi']:.0f}）出现均值回归机会"),
            ("信用healthy", f"HY OAS 维持 300bps 下方（现 {d['hy_oas']:.0f}）风险偏好稳")]
    bear = [("逼近前高", f"纳指升至 ${d['qqq_52w_high']*0.98:,.0f}+ 且 RSI>70 超买"),
            ("低波高估", f"VIX<13 且 估值分位>85%（现分位 {ctx['qqq_pct']:.0f}%）追高风险"),
            ("趋势破位", f"纳指跌破 200 日线(${d['qqq_200ma']:,.0f}) 且 RSI<40 转弱防守"),
            ("信用走阔", f"HY OAS 升破 500bps（现 {d['hy_oas']:.0f}）系统性风险上升")]

    bull_html = "".join(f'<div class="rule rule-bull"><span class="r-kind">买入</span><b>{t}</b><br>{v}</div>' for t, v in bull)
    bear_html = "".join(f'<div class="rule rule-bear"><span class="r-kind">止盈</span><b>{t}</b><br>{v}</div>' for t, v in bear)

    st_lines = "".join(f"<li>{k}: {v}</li>" for k, v in status.items())

    html_doc = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
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
 padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.comp-gauge{{flex:0 0 220px}}
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
.now-card{{border:2px solid {cz_color};border-radius:12px;padding:16px;background:#fbfdff;margin-bottom:14px}}
.now-card .nc-title{{font-size:14px;font-weight:700;margin-bottom:8px}}
.now-card .nc-zone{{font-size:20px;font-weight:800;color:{cz_color}}}
.now-card .nc-desc{{font-size:12px;color:#64748b;margin-top:6px}}
.status{{font-size:12px;color:#64748b}}
.status li{{margin:2px 0}}
footer{{text-align:center;font-size:11px;color:#94a3b8;margin-top:10px}}
</style></head><body><div class="wrap">
<header>
  <h1>📈 纳指交易助手</h1>
  <div class="meta">数据时间：{d.get('as_of','')} ｜ 标的：纳指100(QQQ ETF) ｜ 框架：7 指标加权打分 + 买卖价位区间 + 时间窗口（直接买入现货 ETF）</div>
  <div class="signal">{ctx['signal']}　综合评分 {comp:.0f}/100</div>
  <div class="advice">{ctx['advice']}</div>
</header>

<div class="comp-wrap">
  <div class="comp-gauge">{gauge(comp, ctx['color'], "综合评分", ctx['signal'])}</div>
  <div style="flex:1">
    <h2 style="margin-bottom:10px;font-size:15px">指标仪表盘（分数越高 = 越适合买入/加仓纳指）</h2>
    <div class="cards" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">
      {''.join(gauge(ind[k], '#2563eb', n, '') for k,n in
        [('valuation','估值'),('trend','趋势'),('rsi','RSI'),('erp','ERP'),
         ('vix','VIX'),('hy','信用利差'),('term','期限结构')])}
    </div>
  </div>
</div>

<div class="now-card">
  <div class="nc-title">📍 当前纳指 QQQ ${d['qqq']:,.0f} 所处区间</div>
  <div class="nc-zone">{cz_name}</div>
  <div class="nc-desc">{cz_desc}</div>
</div>

<div class="section"><h2>① 七维指标明细</h2><div class="cards">{''.join(cards)}</div></div>

<div class="section"><h2>② 买入 / 卖出价位区间（随实时数据动态计算）</h2>
  <table><thead><tr>
    <th>区间</th><th>下沿</th><th>上沿</th><th>策略含义</th><th>状态</th>
  </tr></thead><tbody>{zone_rows}</tbody></table>
  <p style="font-size:12px;color:#64748b;margin-top:10px">
  区间基于 52 周高低(${d['qqq_52w_low']:,.0f}–${d['qqq_52w_high']:,.0f}) 与 200 日线(${d['qqq_200ma']:,.0f}) 动态生成；
  绿区越跌越买，红区越涨越卖。纳指长期向上，减仓区宜"分批减/留底仓"，不必清仓。建议分批操作。</p>
</div>

<div class="section"><h2>③ 时间窗口与触发条件</h2>
  <div class="rules">
    <div><div style="font-weight:700;margin-bottom:8px;color:#16a34a">买入 / 加仓触发条件</div>{bull_html}</div>
    <div><div style="font-weight:700;margin-bottom:8px;color:#dc2626">止盈 / 减仓触发条件</div>{bear_html}</div>
  </div>
  <p style="font-size:12px;color:#64748b;margin-top:12px">
  核心逻辑：低估值+高恐慌+高 ERP 时"买在恐惧"；逼近前高+超买+低波动时"分批止盈"。
  纳指宜长线持有、定投为主，择时仅用于加减仓，避免频繁全进全出。</p>
</div>

<div class="section"><h2>④ 数据来源与新鲜度</h2>
  <ul class="status">{st_lines}</ul>
  <p style="font-size:12px;color:#94a3b8;margin-top:8px">
  数据源：Yahoo Finance (VIX/QQQ/期限结构/10Y) + FRED (HY OAS)。
  若标注"回退-快照"表示实时抓取失败，使用内置快照，请以实时数据为准。</p>
</div>

<footer>
  纳指交易助手 · 仅供研究参考，非投资建议<br>
  生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} ·
  <a href="{HOMEPAGE_URL}" style="color:#2563eb">返回工具箱首页</a> ·
  <a href="{FEEDBACK_URL}" style="color:#2563eb">反馈/建议</a>
</footer>
</div></body></html>'''
    return html_doc

# ----------------------------------------------------------------------------
# 7. 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="纳指交易助手 (Nasdaq/QQQ Trading Assistant)")
    ap.add_argument("--offline", action="store_true", help="强制使用内置快照")
    ap.add_argument("--fred-key", default=None, help="FRED API Key")
    ap.add_argument("--manual", default=None, help="手动覆盖JSON(覆盖SNAPSHOT字段)")
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

    # 控制台摘要
    print(f"\n  数据时间      : {d.get('as_of','')}")
    print(f"  QQQ 现价      : ${d['qqq']:,.2f}  (52周 {d['qqq_52w_low']:,.0f}-{d['qqq_52w_high']:,.0f}, 分位 {ctx['qqq_pct']:.0f}%)")
    print(f"  200MA         : ${d['qqq_200ma']:,.0f}  → 价格/MA = {ctx['trend_ratio']:.2f}×   RSI {d['qqq_rsi']:.0f}")
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
