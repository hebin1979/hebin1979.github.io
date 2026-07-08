#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQQ 卖 Put 交易助手 (Sell-Put Assistant for QQQ LEAPS)
========================================================
目的：综合多个市场指标，判断"现在是否适合开始卖一年期(LEAPS) QQQ Put"，
      并筛选最合适的行权价、给出获利了结与风控建议。

指标框架（7 个，权重合计 100）：
  1. 波动率环境 VIX       权重 22  —— 权利金丰厚程度（水平+1年分位）
  2. 股票风险溢价 ERP      权重 15  —— 股票相对债券的估值吸引力
  3. VIX 期限结构          权重 13  —— 波动率的期限结构(Contango/Back)
  4. QQQ 趋势(200MA)      权重 15  —— 不逆势卖 Put
  5. 高收益信用利差 HY OAS 权重 12  —— 系统性信用风险
  6. QQQ 动量 RSI(14)     权重 13  —— 超卖=更好的卖 Put 时点
  7. QQQ 估值分位          权重 10  —— 距离52周高点的缓冲垫

综合评分 -> 行动信号：
  >=70  强烈建议开仓（分批建仓卖 Put）
  58-69 条件合适，可小仓位开始
  45-57 中性，观望等待更好点位
  <45   暂不建议（波动太低 / 趋势恶化 / 信用走阔）

数据来源：Yahoo Finance (VIX/QQQ/VIX期限结构/10Y/期权链) + FRED (HY OAS)
         本机直连 Yahoo/FRED 通常可用；若抓取失败自动回退到缓存/快照并标注。

用法：
  python qqq_put_assistant.py                # 拉取实时数据并生成报告
  python qqq_put_assistant.py --offline      # 强制使用内置快照(演示/离线)
  python qqq_put_assistant.py --fred-key X  # 提供 FRED API Key(获取 HY OAS)
  python qqq_put_assistant.py --manual json # 手动覆盖关键数值(见 SNAPSHOT)
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
# 0. 内置快照 (真实数据校准, 截至 2026-07-07, 用于离线/抓取失败回退)
# ----------------------------------------------------------------------------
SNAPSHOT = {
    "as_of": "2026-07-07(快照/离线)",
    "vix": 16.13, "vix_52w_high": 35.30, "vix_52w_low": 13.38,
    "vix_1y_pct": 12.5,                       # VIX 在过去1年中的分位(手动校准)
    "qqq": 709.43, "qqq_52w_high": 748.65, "qqq_52w_low": 549.58,
    "qqq_200ma": 650.0,
    "qqq_rsi": 52.0,
    "qqq_trailing_eps": 21.5,                # 用于 ERP 计算
    "qqq_div_yield": 0.0043,                 # 用于 BS 模型的 q
    "vix3m": 17.5, "vix6m": 18.5,
    "tnx": 44.8,                             # CBOE 10Y 收益率指数(值=收益率*10)
    "hy_oas": 320.0,                         # 高收益债利差(bps)
    # ~1年到期 Put 候选(快照, 仅用于离线演示; 实时会替换为真实期权链)
    "options": [
        # strike, bid, ask, impliedVolatility
        (560, 6.80, 7.20, 0.245),
        (590, 9.10, 9.60, 0.240),
        (620, 12.30, 12.90, 0.235),
        (650, 16.50, 17.20, 0.230),
        (680, 22.10, 22.90, 0.225),
        (710, 29.50, 30.50, 0.222),
    ],
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
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
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

def _yf_options(sym, s=None):
    if s is None:
        s = _session()
    for host in ("query1", "query2"):
        try:
            r = s.get(f"https://{host}.finance.yahoo.com/v7/finance/options/{sym}", timeout=25)
            if r.status_code == 200:
                j = r.json()
                return j.get("optionChain", {}).get("result", [{}])[0]
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
        d["options_live"] = False
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
            closes = v["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            d["vix"] = m["regularMarketPrice"]
            d["vix_52w_high"] = m.get("fiftyTwoWeekHigh", max(closes))
            d["vix_52w_low"] = m.get("fiftyTwoWeekLow", min(closes))
            d["vix_1y_pct"] = pct_of(d["vix"], d["vix_52w_low"], d["vix_52w_high"])
            status["vix"] = "实时(Yahoo)"
        else:
            raise ValueError("empty")
    except Exception:
        d["vix"] = SNAPSHOT["vix"]; d["vix_52w_high"] = SNAPSHOT["vix_52w_high"]
        d["vix_52w_low"] = SNAPSHOT["vix_52w_low"]; d["vix_1y_pct"] = SNAPSHOT["vix_1y_pct"]
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
            d["qqq_div_yield"] = (m.get("trailingAnnualDividendYield") or SNAPSHOT["qqq_div_yield"])
            d["qqq_rsi"] = rsi(closes, 14)
            status["qqq"] = "实时(Yahoo)"
        else:
            raise ValueError("empty")
    except Exception:
        for k in ("qqq","qqq_52w_high","qqq_52w_low","qqq_200ma","qqq_trailing_eps","qqq_div_yield","qqq_rsi"):
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

    # ---- 期权链 (~1年 Put) ----
    d["options_live"] = False
    try:
        oc = _yf_options("QQQ", s)
        if oc:
            exps = oc.get("expirationDates", [])
            target = pick_1y_expiry(exps)
            chain = None
            for exp in (target, exps[-1]) if target else [exps[-1]]:
                rr = _yf_options_with_exp("QQQ", exp, s)
                if rr:
                    chain = rr; d["expiry"] = exp; break
            if chain is not None:
                puts = chain.get("puts", [])
                d["options"] = [(p["strike"], p["bid"], p["ask"], p.get("impliedVolatility", 0.22))
                                for p in puts if p["bid"] > 0]
                d["options_live"] = True
                status["options"] = "实时(Yahoo)"
            else:
                raise ValueError("no chain")
        else:
            raise ValueError("empty")
    except Exception:
        d["options"] = SNAPSHOT["options"]
        d["expiry"] = None
        status["options"] = "回退-快照"

    d["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # 缓存
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"data": d, "status": status,
                       "ts": time.time()}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return d, status

def _yf_options_with_exp(sym, exp, s):
    for host in ("query1", "query2"):
        try:
            r = s.get(f"https://{host}.finance.yahoo.com/v7/finance/options/{sym}",
                      params={"date": exp}, timeout=25)
            if r.status_code == 200:
                j = r.json()
                res = j.get("optionChain", {}).get("result", [{}])
                if res:
                    return res[0]
        except Exception:
            continue
    return None

def pick_1y_expiry(exps):
    if not exps:
        return None
    now = time.time()
    cand = [(abs(e - (now + 365*86400)), e) for e in exps if e > now]
    cand.sort()
    return cand[0][1] if cand else exps[-1]

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

# ----------------------------------------------------------------------------
# 3. 黑-斯科尔斯 (用于 Put Delta / 近似定价)
# ----------------------------------------------------------------------------
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def put_delta(S, K, T, r, q, sigma):
    if T <= 0 or sigma <= 0:
        return -1.0 if S < K else 0.0
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return -math.exp(-q * T) * norm_cdf(-d1)

def put_price(S, K, T, r, q, sigma):
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return (K * math.exp(-r * T) * norm_cdf(-d2)
            - S * math.exp(-q * T) * norm_cdf(-d1))

# ----------------------------------------------------------------------------
# 4. 指标打分 (每个返回 0-100, 越高越适合卖 Put)
# ----------------------------------------------------------------------------
def score_vix_env(vix, vix_pct):
    """波动率环境：水平(钟形,峰值~25) + 1年分位(线性)。"""
    # 水平钟形
    if vix <= 15:
        lvl = 25 + (vix - 12) / 3 * 5      # 12->25, 15->30
    elif vix <= 25:
        lvl = 30 + (vix - 15) / 10 * 60    # 15->30, 25->90
    elif vix <= 35:
        lvl = 90 - (vix - 25) / 10 * 25    # 25->90, 35->65
    else:
        lvl = max(20, 65 - (vix - 35) * 1.0)
    lvl = max(0, min(100, lvl))
    return 0.45 * lvl + 0.55 * vix_pct

def score_erp(erp):
    """股票风险溢价(百分比点)。越高越适合做多(卖Put)。"""
    if erp <= -2:
        return 15
    if erp >= 4:
        return 92
    # -2 ->15, 4 ->92
    return 15 + (erp + 2) / 6 * 77

def score_term_structure(slope):
    """VIX3M / VIX。Contango(>1)=健康; Backwardation(<1)=恐慌。"""
    if slope >= 1.15:
        return 85
    if slope >= 1.05:
        return 70
    if slope >= 1.00:
        return 55
    if slope >= 0.95:
        return 35
    return 20

def score_trend(ratio):
    """QQQ / 200MA。远离MA下方=趋势恶化。"""
    if ratio >= 1.10:
        return 90
    if ratio >= 1.00:
        return 80
    if ratio >= 0.95:
        return 60
    if ratio >= 0.85:
        return 35
    return 15

def score_hy_oas(oas):
    """高收益债利差(bps)。越低越风险偏好。"""
    if oas < 250:
        return 85
    if oas < 350:
        return 70
    if oas < 500:
        return 45
    if oas < 700:
        return 25
    return 10

def score_rsi(rsi):
    """RSI(14)。超卖=更好卖Put时点(更多权利金+均值回归)。"""
    if rsi < 30:
        return 90
    if rsi < 45:
        return 75
    if rsi < 60:
        return 60
    if rsi < 70:
        return 45
    return 30

def score_valuation(dist_high):
    """距离52周高点比例(price/52wHigh)。越低于高点=缓冲越多。"""
    if dist_high <= 0.85:
        return 85
    if dist_high <= 0.95:
        return 70
    if dist_high < 1.00:
        return 50
    return 35

# ----------------------------------------------------------------------------
# 5. 综合评分
# ----------------------------------------------------------------------------
WEIGHTS = {
    "vix_env": 22, "erp": 15, "term": 13, "trend": 15,
    "hy": 12, "rsi": 13, "valuation": 10,
}

def analyze(d):
    vix = d["vix"]; vix_pct = d["vix_1y_pct"]
    slope = d["vix3m"] / vix if vix > 0 else 1.0
    treasury = d["tnx"] / 10.0
    ey = (d["qqq_trailing_eps"] / d["qqq"]) * 100.0 if d["qqq"] > 0 else 0
    erp = ey - treasury
    trend_ratio = d["qqq"] / d["qqq_200ma"] if d["qqq_200ma"] > 0 else 1.0
    dist_high = d["qqq"] / d["qqq_52w_high"] if d["qqq_52w_high"] > 0 else 1.0

    ind = {
        "vix_env": score_vix_env(vix, vix_pct),
        "erp": score_erp(erp),
        "term": score_term_structure(slope),
        "trend": score_trend(trend_ratio),
        "hy": score_hy_oas(d["hy_oas"]),
        "rsi": score_rsi(d["qqq_rsi"]),
        "valuation": score_valuation(dist_high),
    }
    composite = sum(WEIGHTS[k] * ind[k] for k in WEIGHTS) / 100.0

    if composite >= 70:
        signal = "强烈建议开仓"
        color = "#16a34a"; advice = "波动率/估值环境有利，建议分批建仓卖出 1 年 LEAPS Put，优先选 15-25% OTM 行权价。"
    elif composite >= 58:
        signal = "条件合适·可小仓位开始"
        color = "#65a30d"; advice = "环境偏友好但不算极致，建议先小仓位(1/3)卖出，待 VIX 进一步抬升或回撤再加码。"
    elif composite >= 45:
        signal = "中性·观望"
        color = "#d97706"; advice = "当前波动偏低/估值偏高，权利金不厚。建议等待 VIX 升至 20+ 或 QQQ 出现 5-10% 回撤再动手。"
    else:
        signal = "暂不建议"
        color = "#dc2626"; advice = "环境不利（低波动+高估值/趋势恶化/信用走阔）。持有现金，等待更优风险报酬比。"

    ctx = dict(
        vix=vix, vix_pct=vix_pct, slope=slope, treasury=treasury, ey=ey, erp=erp,
        trend_ratio=trend_ratio, dist_high=dist_high,
        indicators=ind, composite=composite, signal=signal, color=color, advice=advice,
    )
    return ctx

# ----------------------------------------------------------------------------
# 6. 行权价筛选 (1年 LEAPS Put)
# ----------------------------------------------------------------------------
def _delta_fit(delta):
    """Put Delta 拟合度：甜区 0.20-0.30（对应 1 年 LEAPS 合理价外度），越偏离越低。"""
    if delta <= 0.05:
        return 20
    if delta < 0.20:
        return 20 + (delta - 0.05) / 0.15 * 60      # 0.05->20, 0.20->80
    if delta <= 0.30:
        return 80 + (0.30 - delta) / 0.10 * 15      # 0.20->95, 0.30->80 (峰值~0.22)
    if delta <= 0.45:
        return 80 - (delta - 0.30) / 0.15 * 55      # 0.30->80, 0.45->25
    return max(10, 25 - (delta - 0.45) * 40)

def select_strikes(d, ctx):
    S = d["qqq"]; r = ctx["treasury"] / 100.0; q = d["qqq_div_yield"]
    # 到期天数
    exp = d.get("expiry")
    if exp:
        T = max(0.1, (exp - time.time()) / 86400 / 365.0)
    else:
        T = 1.0  # 快照默认1年
    out = []
    for (K, bid, ask, iv) in d["options"]:
        if K >= S or iv <= 0:
            continue  # 只卖价外(OTM) Put
        otm = (S - K) / S * 100.0
        if otm < 5 or otm > 40:
            continue
        mid = (bid + ask) / 2.0
        prem = bid  # 用买价(保守成交)
        yield_k = prem / K * 100.0
        ann_yield = yield_k / T
        delta = abs(put_delta(S, K, T, r, q, iv))  # 0-1
        # 行权价质量分：综合环境 + 权利金收益率 + Delta甜区拟合
        yield_score = min(ann_yield / 6.0, 1.0) * 100.0   # 年化6%封顶
        delta_score = _delta_fit(delta)
        strike_score = (0.35 * ctx["composite"] + 0.35 * yield_score
                        + 0.30 * delta_score)
        out.append(dict(K=K, bid=bid, ask=ask, mid=mid, iv=iv*100.0, otm=otm,
                        prem=prem, yield_k=yield_k, ann_yield=ann_yield,
                        delta=delta, T=T, strike_score=strike_score))
    out.sort(key=lambda x: x["strike_score"], reverse=True)
    return out[:6]

# ----------------------------------------------------------------------------
# 7. 获利了结 / 风控建议 (针对推荐合约)
# ----------------------------------------------------------------------------
def exit_plan(ctx, strike):
    prem = strike["prem"]
    tp_price = prem * 0.50          # 获利50%了结
    rules = []
    rules.append(("获利了结 (50% 最大收益)",
                  f"当该 Put 市值跌至 ≤ ${tp_price:.2f}（权利金的 50%）时平仓获利，释放资金。",
                  "核心规则"))
    rules.append(("波动率回落 (Vol Crush)",
                  "若 VIX 较建仓时下跌 >30%，波动率溢价已兑现，可提前了结。",
                  "辅助规则"))
    rules.append(("滚动 (Roll) 触发",
                  f"若 QQQ 跌破行权价、Put Delta 升至 >0.40（当前 Δ≈{strike['delta']:.2f}），"
                  f"说明接货风险上升，应向下/向后滚动或准备现金接货。",
                  "风控"))
    rules.append(("到期处理",
                  f"若临近到期(≤30天)仍为价外，让其到期归零或平仓；若价内则履约接货或滚动。",
                  "收尾"))
    return rules

# ----------------------------------------------------------------------------
# 8. HTML 报告
# ----------------------------------------------------------------------------
def gauge(value, color, label, sub=""):
    """半环形仪表盘 SVG。value 0-100。"""
    import html
    angle = 180 - (value / 100.0) * 180  # 180->左, 0->右
    rad = math.radians(angle)
    x = 100 + 80 * math.cos(rad)
    y = 100 - 80 * math.sin(rad)
    large = 0 if value <= 50 else 0  # 半圆始终用小弧
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

def build_html(d, ctx, strikes, status):
    ind = ctx["indicators"]
    comp = ctx["composite"]
    # 指标卡片
    cards = []
    def card(name, val, score, color, note):
        return (f'<div class="card"><div class="c-head"><span class="c-name">{name}</span>'
                f'<span class="c-score" style="color:{color}">{score:.0f}</span></div>'
                f'<div class="c-val">{val}</div>'
                f'<div class="bar"><div class="bar-fill" style="width:{score:.0f}%;background:{color}"></div></div>'
                f'<div class="c-note">{note}</div></div>')
    cards.append(card("波动率环境 (VIX)", f"VIX {ctx['vix']:.1f} · 分位 {ctx['vix_pct']:.0f}%",
                      ind["vix_env"], "#2563eb", "权利金丰厚程度：水平+1年分位"))
    cards.append(card("股票风险溢价 (ERP)", f"ERP {ctx['erp']:+.1f}%  (盈利收益率{ctx['ey']:.1f}% − 10Y {ctx['treasury']:.1f}%)",
                      ind["erp"], "#7c3aed", "股票相对债券的估值吸引力"))
    cards.append(card("VIX 期限结构", f"斜率 {ctx['slope']:.2f} (VIX3M/VIX)",
                      ind["term"], "#0891b2", "Contango 健康 / Backwardation 恐慌"))
    cards.append(card("QQQ 趋势 (200MA)", f"价格/MA200 = {ctx['trend_ratio']:.2f}×",
                      ind["trend"], "#16a34a", "不逆势卖 Put"))
    cards.append(card("高收益信用利差", f"HY OAS {d['hy_oas']:.0f} bps",
                      ind["hy"], "#ea580c", "系统性信用风险温度"))
    cards.append(card("QQQ 动量 RSI(14)", f"RSI = {d['qqq_rsi']:.0f}",
                      ind["rsi"], "#db2777", "超卖=更好的卖 Put 时点"))
    cards.append(card("QQQ 估值分位", f"距52周高点 {ctx['dist_high']*100:.0f}%",
                      ind["valuation"], "#ca8a04", "缓冲垫厚度"))

    # 候选合约表
    rows = ""
    if strikes:
        for i, s in enumerate(strikes, 1):
            tag = "推荐" if i == 1 else (f"备选{i}" if i <= 3 else "观察")
            color = "#16a34a" if i == 1 else "#64748b"
            rows += (f"<tr><td><b style='color:{color}'>{tag}</b></td>"
                     f"<td>${s['K']:.0f}</td>"
                     f"<td>{s['otm']:.1f}%</td>"
                     f"<td>${s['prem']:.2f}</td>"
                     f"<td>{s['yield_k']:.1f}%</td>"
                     f"<td>{s['ann_yield']:.1f}%</td>"
                     f"<td>{s['iv']:.0f}%</td>"
                     f"<td>{s['delta']:.2f}</td>"
                     f"<td><b>{s['strike_score']:.0f}</b></td></tr>")
    else:
        rows = "<tr><td colspan='9' style='text-align:center;color:#dc2626'>无可用期权数据（请联网运行或检查数据源）</td></tr>"

    # 退出规则（取第一推荐）
    exit_html = ""
    trade_card = ""
    if strikes:
        top = strikes[0]
        capital = top["K"] * 100          # 现金担保每合约资金
        max_profit = top["prem"] * 100    # 最大收益=权利金
        breakeven = top["K"] - top["prem"]
        dte = int(top["T"] * 365)
        trade_card = f'''<div class="trade-card">
          <div class="tc-title">🎯 首选交易卡（以「{ctx['signal']}」环境为例）</div>
          <div class="tc-grid">
            <div><span>卖出 Put 行权价</span><b>${top['K']:.0f}</b></div>
            <div><span>到期天数</span><b>~{dte} 天 (LEAPS)</b></div>
            <div><span>收到权利金/股</span><b>${top['prem']:.2f}</b></div>
            <div><span>每合约最大收益</span><b style="color:#16a34a">${max_profit:,.0f}</b></div>
            <div><span>现金担保/合约</span><b>${capital:,.0f}</b></div>
            <div><span>盈亏平衡价</span><b style="color:#dc2626">${breakeven:.2f}</b></div>
            <div><span>价外幅度</span><b>{top['otm']:.1f}%</b></div>
            <div><span>Put Delta (接货概率≈)</span><b>{top['delta']:.2f}</b></div>
            <div><span>隐含波动率</span><b>{top['iv']:.0f}%</b></div>
          </div>
          <div class="tc-note">盈亏平衡 = 行权价 − 权利金；到期价 ≥ 行权价则权利金全收，
          到期价 &lt; 盈亏平衡价才开始亏损。现金担保卖出 Put 本质是"以折扣价建仓 QQQ"。</div>
        </div>'''
        for (t, desc, kind) in exit_plan(ctx, top):
            exit_html += f'<div class="rule"><span class="r-kind">{kind}</span><b>{t}</b><br>{desc}</div>'

    # 数据状态
    st_lines = "".join(f"<li>{k}: {v}</li>" for k, v in status.items())

    html_doc = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QQQ 卖 Put 交易助手 · 报告</title>
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
td:nth-child(2){{font-weight:700}}
.rules{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.rule{{border-left:4px solid {ctx['color']};background:#f8fafc;border-radius:8px;padding:12px;font-size:13px;line-height:1.6}}
.r-kind{{display:inline-block;font-size:11px;background:#e2e8f0;color:#475569;
 border-radius:4px;padding:1px 8px;margin-bottom:6px}}
.trade-card{{margin-top:16px;border:2px solid {ctx['color']};border-radius:12px;padding:16px;background:#fcfdfe}}
.tc-title{{font-size:14px;font-weight:700;margin-bottom:12px;color:#0f172a}}
.tc-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.tc-grid>div{{display:flex;flex-direction:column;gap:2px}}
.tc-grid span{{font-size:11px;color:#64748b}}
.tc-grid b{{font-size:16px;color:#0f172a}}
.tc-note{{font-size:12px;color:#64748b;margin-top:12px;line-height:1.6}}
.status{{font-size:12px;color:#64748b}}
.status li{{margin:2px 0}}
footer{{text-align:center;font-size:11px;color:#94a3b8;margin-top:10px}}
</style></head><body><div class="wrap">
<header>
  <h1>📉 QQQ 卖 Put 交易助手</h1>
  <div class="meta">数据时间：{d.get('as_of','')} ｜ 标的：QQQ(纳指100 ETF) ｜ 策略：卖出 1 年远期(LEAPS) 价外 Put</div>
  <div class="signal">{ctx['signal']}　综合评分 {comp:.0f}/100</div>
  <div class="advice">{ctx['advice']}</div>
</header>

<div class="comp-wrap">
  <div class="comp-gauge">{gauge(comp, ctx['color'], "综合评分", ctx['signal'])}</div>
  <div style="flex:1">
    <h2 style="margin-bottom:10px;font-size:15px">指标仪表盘（分数越高 = 越适合卖 Put）</h2>
    <div class="cards" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">
      {''.join(gauge(ind[k], '#2563eb', n, '') for k,n in
        [('vix_env','VIX环境'),('erp','ERP'),('term','期限结构'),('trend','趋势'),
         ('hy','信用利差'),('rsi','RSI'),('valuation','估值')])}
    </div>
  </div>
</div>

<div class="section"><h2>① 七维指标明细</h2><div class="cards">{''.join(cards)}</div></div>

<div class="section"><h2>② 推荐卖出的一年期(LEAPS) Put 候选</h2>
  <table><thead><tr>
    <th>评级</th><th>行权价</th><th>价外%</th><th>权利金(买价)</th>
    <th>权利金/行权价</th><th>年化收益</th><th>隐含波动率</th><th>Put Delta</th><th>合约评分</th>
  </tr></thead><tbody>{rows}</tbody></table>
  <p style="font-size:12px;color:#64748b;margin-top:10px">
  说明：年化收益 = 权利金 ÷ 行权价（现金担保，按 1 年计）；Put Delta 表示到期接货概率近似。
  推荐首选综合"环境+权利金+Delta甜区"最高的合约。</p>
  {trade_card}
</div>

<div class="section"><h2>③ 获利了结 / 滚动 / 风控规则（以首选合约为例）</h2>
  <div class="rules">{exit_html}</div>
</div>

<div class="section"><h2>④ 数据来源与新鲜度</h2>
  <ul class="status">{st_lines}</ul>
  <p style="font-size:12px;color:#94a3b8;margin-top:8px">
  数据源：Yahoo Finance (VIX/QQQ/期限结构/10Y/期权链) + FRED (HY OAS)。
  若标注"回退-快照"表示实时抓取失败，使用内置快照，请以实时数据为准。</p>
</div>

<footer>
  QQQ 卖 Put 交易助手 · 仅供研究参考，非投资建议<br>
  生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} ·
  <a href="{HOMEPAGE_URL}" style="color:#2563eb">返回工具箱首页</a> ·
  <a href="{FEEDBACK_URL}" style="color:#2563eb">反馈/建议</a>
</footer>
</div></body></html>'''
    return html_doc

# ----------------------------------------------------------------------------
# 9. 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="QQQ 卖 Put 交易助手")
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
    print("  QQQ 卖 Put 交易助手  ·  拉取市场数据 ...")
    print("=" * 60)
    d, status = fetch_market_data(fred_key=args.fred_key, offline=offline)

    ctx = analyze(d)
    strikes = select_strikes(d, ctx)

    # 控制台摘要
    print(f"\n  数据时间      : {d.get('as_of','')}")
    print(f"  QQQ 现价      : ${d['qqq']:.2f}  (52周 {d['qqq_52w_low']:.0f}-{d['qqq_52w_high']:.0f})")
    print(f"  VIX           : {ctx['vix']:.2f}  (1年分位 {ctx['vix_pct']:.0f}%)")
    print(f"  10Y 国债      : {ctx['treasury']:.2f}%   盈利收益率 {ctx['ey']:.2f}%  → ERP {ctx['erp']:+.2f}%")
    print(f"  VIX 期限结构  : 斜率 {ctx['slope']:.2f}")
    print(f"  QQQ/200MA     : {ctx['trend_ratio']:.2f}×   RSI {d['qqq_rsi']:.0f}   HY OAS {d['hy_oas']:.0f}bps")
    print("-" * 60)
    print("  指标评分(0-100, 越高越适合卖Put):")
    names = {"vix_env":"波动率环境","erp":"股票风险溢价","term":"VIX期限结构",
             "trend":"QQQ趋势","hy":"信用利差","rsi":"RSI动量","valuation":"估值分位"}
    for k in WEIGHTS:
        print(f"    {names[k]:<12} {ctx['indicators'][k]:5.1f}   (权重{WEIGHTS[k]})")
    print("-" * 60)
    print(f"  ★ 综合评分    : {ctx['composite']:.1f} / 100")
    print(f"  ★ 行动信号    : {ctx['signal']}")
    print(f"  ★ 建议        : {ctx['advice']}")
    if strikes:
        print("-" * 60)
        print("  推荐合约(按合约评分排序):")
        for i, s in enumerate(strikes[:4], 1):
            tag = "★推荐" if i == 1 else f"备选{i}"
            print(f"    {tag}  K=${s['K']:.0f}  OTM={s['otm']:.1f}%  权利金=${s['prem']:.2f}  "
                  f"年化={s['ann_yield']:.1f}%  Δ={s['delta']:.2f}  评分={s['strike_score']:.0f}")
    else:
        print("  (无可用期权数据)")
    print("=" * 60)

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "qqq_put_report.html")
    html_doc = build_html(d, ctx, strikes, status)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"  ✅ 报告已生成: {out}\n")

if __name__ == "__main__":
    main()
