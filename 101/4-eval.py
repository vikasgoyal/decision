r"""Measure run-to-run variance of use case 4 (legal contract review).

Sends the exact same request to Microsoft-Decision-1 repeatedly (default: 20
runs, 3 seconds apart), analyzes the variation in the answers, and writes a
self-contained HTML report (Arial, inline SVG charts) to 4-eval-report.html.

Requires Python 3.10+; no third-party packages are needed.
Reuses the input, questions, and configuration from 4-legal-contract-review.py,
so keep both scripts in the same folder. Configure .env as described there and
sign in with `az login`. In PowerShell:

    py .\4-eval.py
    py .\4-eval.py --runs 5 --delay 1     # quick trial
    py .\4-eval.py --output C:\temp\eval.html

The request body is built once and reused byte-for-byte, so every run receives
identical input. The report shows its SHA-256 hash. The sleep is applied between
runs; the script does not wait after the final run. A failed run is recorded and
the experiment continues.

The results describe repeatability for this input, deployment, and time window.
They do not prove determinism for other inputs or after model updates.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean, median, stdev

SOURCE = Path(__file__).with_name("4-legal-contract-review.py")
DEFAULT_OUTPUT = Path(__file__).with_name("4-eval-report.html")
DEFAULT_RUNS = 20
DEFAULT_DELAY = 3.0

spec = importlib.util.spec_from_file_location("legal_review", SOURCE)
if spec is None or spec.loader is None:
    sys.exit(f"Cannot load {SOURCE}. Keep 4-legal-contract-review.py beside this script.")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

QUESTIONS = base.QUESTIONS
STATE = base.DEFAULT_STATE
ROUTE_OPTIONS = list(QUESTIONS["review_route"]["criteria"])
RISK_LEVELS = list(range(len(QUESTIONS["contract_risk"]["criteria"])))
RISK_NAMES = {
    i: text.split(":")[0] for i, text in enumerate(QUESTIONS["contract_risk"]["criteria"])
}

T_CRIT = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 25: 2.060,
    30: 2.042,
}


def t_crit(df: int) -> float:
    """Two-sided 95% Student t critical value; conservative between tabulated df."""
    if df < 1:
        return float("nan")
    if df > 30:
        return 1.96
    return T_CRIT[max(k for k in T_CRIT if k <= df)]


def get_token(parser: argparse.ArgumentParser) -> str:
    token = os.environ.get("AZURE_ENTRA_TOKEN", "").strip()
    if token:
        return token
    az = shutil.which("az")
    if not az:
        parser.error("Install Azure CLI and run az login, or set AZURE_ENTRA_TOKEN.")
    try:
        completed = subprocess.run(
            [
                az, "account", "get-access-token",
                "--resource", "https://cognitiveservices.azure.com",
                "--query", "accessToken", "-o", "tsv",
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )
    except subprocess.CalledProcessError as exc:
        parser.error(f"Azure CLI token acquisition failed. Run az login.\n{exc.stderr}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        parser.error(f"Could not run Azure CLI: {exc}")
    token = completed.stdout.strip()
    if not token:
        parser.error("Azure CLI returned an empty access token.")
    return token


def parse_run(result) -> dict:
    """Extract the numbers we analyze; raise ValueError on any unexpected shape."""
    try:
        answers = result["answers"]
        review = float(answers["attorney_review_required"]["noul"])
        route = answers["review_route"]
        risk = answers["contract_risk"]
        route_probs = {k: float(route["probabilities"][k]) for k in ROUTE_OPTIONS}
        risk_probs = {i: float(risk["probabilities"][str(i)]) for i in RISK_LEVELS}
        usage = result.get("usage") or {}
        return {
            "review": review,
            "route_choice": str(route["choice"]),
            "route_conf": float(route["confidence"]),
            "route_probs": route_probs,
            "risk_score": float(risk["score"]),
            "risk_conf": float(risk["confidence"]),
            "risk_probs": risk_probs,
            "in_tokens": usage.get("input_tokens"),
            "out_tokens": usage.get("output_tokens"),
        }
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"unexpected response shape ({exc!r})") from exc


def call_once(url: str, body: bytes, token: str) -> tuple[dict, float]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)
    return result, (time.perf_counter() - started) * 1000


# ----------------------------- statistics -----------------------------------

def describe(values: list[float]) -> dict:
    n = len(values)
    m = mean(values)
    sd = stdev(values) if n > 1 else 0.0
    half = t_crit(n - 1) * sd / math.sqrt(n) if n > 1 else float("nan")
    return {
        "n": n, "mean": m, "sd": sd, "median": median(values),
        "min": min(values), "max": max(values), "range": max(values) - min(values),
        "cv": sd / abs(m) if m else float("nan"),
        "ci_lo": m - half, "ci_hi": m + half,
    }


def logit(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


def logit_sd(values: list[float]) -> float:
    return stdev([logit(v) for v in values]) if len(values) > 1 else 0.0


def entropy(probs: list[float], normalize: bool = True) -> float:
    h = -sum(p * math.log(p) for p in probs if p > 0)
    return h / math.log(len(probs)) if normalize and len(probs) > 1 else h


def tv_distance(p: list[float], q: list[float]) -> float:
    return 0.5 * sum(abs(a - b) for a, b in zip(p, q))


def trend(values: list[float]) -> tuple[float, float]:
    """Ordinary least squares slope per run and Pearson r against run index."""
    n = len(values)
    if n < 3:
        return float("nan"), float("nan")
    xs = list(range(1, n + 1))
    mx, my = mean(xs), mean(values)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in values)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    slope = sxy / sxx
    r = sxy / math.sqrt(sxx * syy) if syy > 0 else 0.0
    return slope, r


def r_critical(n: int) -> float:
    t = t_crit(n - 2)
    return t / math.sqrt(t * t + (n - 2)) if n > 2 else float("nan")


# ------------------------------- rendering ----------------------------------

def esc(value) -> str:
    return base.esc(value)


def fmt_value(kind: str, v: float) -> str:
    if v != v:
        return "n/a"
    return {"prob": f"{v * 100:.3f}%", "score": f"{v:.4f}", "ms": f"{v:.0f} ms"}[kind]


def fmt_spread(kind: str, v: float) -> str:
    if v != v:
        return "n/a"
    return {"prob": f"{v * 100:.4f} pp", "score": f"{v:.4f}", "ms": f"{v:.0f} ms"}[kind]


def line_chart(title: str, values: list[float], kind: str) -> str:
    n = len(values)
    m = mean(values)
    sd = stdev(values) if n > 1 else 0.0
    width, height, left, right, top, bottom = 460, 240, 64, 14, 30, 36
    lo = min(min(values), m - sd)
    hi = max(max(values), m + sd)
    pad = (hi - lo) * 0.12 if hi - lo > 1e-12 else max(abs(hi) * 0.001, 1e-3)
    lo, hi = lo - pad, hi + pad
    scale = 100 if kind == "prob" else 1
    suffix = {"prob": "%", "score": "", "ms": " ms"}[kind]
    span = (hi - lo) * scale
    decimals = 0 if kind == "ms" else (
        2 if span >= 1 else min(6, int(-math.floor(math.log10(span))) + 2)
    )

    def x_of(i: int) -> float:
        return left + (width - left - right) * (i / (n - 1) if n > 1 else 0.5)

    def y_of(v: float) -> float:
        return top + (height - top - bottom) * (1 - (v - lo) / (hi - lo))

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}" '
        f'class="chart"><text x="{left}" y="16" class="ct">{esc(title)}</text>'
    ]
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = y_of(v)
        parts.append(
            f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{left - 6}" y="{y + 3:.1f}" text-anchor="end" class="ax">'
            f"{v * scale:.{decimals}f}{suffix}</text>"
        )
    band_top, band_bottom = y_of(m + sd), y_of(m - sd)
    parts.append(
        f'<rect x="{left}" y="{band_top:.1f}" width="{width - left - right}" '
        f'height="{max(band_bottom - band_top, 0.5):.1f}" class="band"/>'
        f'<line x1="{left}" x2="{width - right}" y1="{y_of(m):.1f}" y2="{y_of(m):.1f}" class="mean"/>'
    )
    points = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in enumerate(values))
    parts.append(f'<polyline points="{points}" class="line"/>')
    for i, v in enumerate(values):
        parts.append(
            f'<circle cx="{x_of(i):.1f}" cy="{y_of(v):.1f}" r="3.5" class="dot">'
            f"<title>Run {i + 1}: {fmt_value(kind, v)}</title></circle>"
        )
    for i in range(n):
        if i == 0 or i == n - 1 or (i + 1) % 5 == 0:
            parts.append(
                f'<text x="{x_of(i):.1f}" y="{height - 18}" text-anchor="middle" class="ax">{i + 1}</text>'
            )
    parts.append(
        f'<text x="{(left + width - right) / 2:.0f}" y="{height - 4}" text-anchor="middle" '
        f'class="ax">Run number</text></svg>'
    )
    return "".join(parts)


def heatmap(columns: list[str], rows: list[list[float]], labels: list[str]) -> str:
    """Rows = runs. Color shows deviation from that column's mean (blue above, orange below)."""
    means = [mean(r[c] for r in rows) for c in range(len(columns))]
    maxdev = [max(abs(r[c] - means[c]) for r in rows) for c in range(len(columns))]
    head = "".join(f"<th>{esc(label)}</th>" for label in labels)
    body = []
    for i, row in enumerate(rows):
        cells = []
        for c, v in enumerate(row):
            dev = v - means[c]
            intensity = abs(dev) / maxdev[c] if maxdev[c] > 0 else 0
            color = "47,111,179" if dev >= 0 else "217,119,6"
            alpha = 0.12 + 0.6 * intensity if maxdev[c] > 0 else 0
            cells.append(
                f'<td style="background:rgba({color},{alpha:.2f})">{v * 100:.3f}%'
                f'<br><span class="sub">{dev * 100:+.4f} pp</span></td>'
            )
        body.append(f"<tr><th>{i + 1}</th>{''.join(cells)}</tr>")
    return f'<table class="heat"><tr><th>Run</th>{head}</tr>{"".join(body)}</table>'


def stat_table(metrics: list[tuple[str, list[float], str]]) -> str:
    head = (
        "<tr><th>Metric</th><th>n</th><th>Mean</th><th>SD</th><th>Median</th><th>Min</th>"
        "<th>Max</th><th>Range</th><th>CV</th><th>95% CI of mean</th><th>Logit SD</th></tr>"
    )
    rows = []
    for name, values, kind in metrics:
        d = describe(values)
        ci = (
            "n/a" if d["ci_lo"] != d["ci_lo"]
            else f"{fmt_value(kind, d['ci_lo'])} to {fmt_value(kind, d['ci_hi'])}"
        )
        cv = "n/a" if d["cv"] != d["cv"] else f"{d['cv'] * 100:.4f}%"
        lsd = f"{logit_sd(values):.4f}" if kind == "prob" else "&mdash;"
        rows.append(
            f"<tr><td>{esc(name)}</td><td>{d['n']}</td><td>{fmt_value(kind, d['mean'])}</td>"
            f"<td>{fmt_spread(kind, d['sd'])}</td><td>{fmt_value(kind, d['median'])}</td>"
            f"<td>{fmt_value(kind, d['min'])}</td><td>{fmt_value(kind, d['max'])}</td>"
            f"<td>{fmt_spread(kind, d['range'])}</td><td>{cv}</td><td>{ci}</td><td>{lsd}</td></tr>"
        )
    return f"<table>{head}{''.join(rows)}</table>"


EXTRA_STYLE = """
.charts { display: flex; flex-wrap: wrap; gap: 16px; }
.charts > div { flex: 1 1 440px; border: 1px solid #e4e8ee; border-radius: 6px; padding: 6px; }
.chart { width: 100%; height: auto; font-family: Arial, Helvetica, sans-serif; }
.ct { font-size: 13px; font-weight: bold; fill: #1b2a41; }
.ax { font-size: 10px; fill: #616e7c; }
.grid { stroke: #e4e8ee; stroke-width: 1; }
.band { fill: rgba(47,111,179,.14); }
.mean { stroke: #2f6fb3; stroke-width: 1.5; stroke-dasharray: 5 4; }
.line { fill: none; stroke: #9db7d5; stroke-width: 1.2; }
.dot { fill: #1b2a41; }
.legend { font-size: 12px; color: #616e7c; margin: 8px 0 0; }
.heat td { text-align: center; font-size: 12px; white-space: nowrap; }
.heat th { text-align: center; }
.verdict { font-size: 15px; font-weight: bold; padding: 12px 16px; border-radius: 6px;
           border: 1px solid #b7dfc3; background: #eaf7ee; color: #1e5631; margin-bottom: 16px; }
.verdict.warn { border-color: #f0dca5; background: #fff8e6; color: #7a5a00; }
.verdict.bad { border-color: #f0b8b8; background: #fdeeee; color: #8a1f1f; }
.bars .row { margin-top: 8px; font-size: 13px; }
.mono { font-family: Consolas, "Courier New", monospace; font-size: 12px; word-break: break-all; }
section table + h3, section .charts + h3 { margin-top: 22px; }
td.num { text-align: right; }
"""


def render_report(ctx: dict) -> str:
    runs = ctx["ok"]
    n = len(runs)
    review = [r["review"] for r in runs]
    risk = [r["risk_score"] for r in runs]
    route_conf = [r["route_conf"] for r in runs]
    risk_conf = [r["risk_conf"] for r in runs]
    latency = [r["latency"] for r in runs]
    counts = Counter(r["route_choice"] for r in runs)
    modal, modal_count = counts.most_common(1)[0]
    agreement = modal_count / n
    modal_probs = [r["route_probs"][modal] for r in runs]
    top_risk_level = max(RISK_LEVELS)
    top_risk_probs = [r["risk_probs"][top_risk_level] for r in runs]
    rounded_levels = Counter(min(max(round(v), 0), top_risk_level) for v in risk)
    level_mode, level_mode_count = rounded_levels.most_common(1)[0]
    level_agreement = level_mode_count / n

    route_vectors = [[r["route_probs"][o] for o in ROUTE_OPTIONS] for r in runs]
    risk_vectors = [[r["risk_probs"][i] for i in RISK_LEVELS] for r in runs]
    route_mean = [mean(v[c] for v in route_vectors) for c in range(len(ROUTE_OPTIONS))]
    risk_mean = [mean(v[c] for v in risk_vectors) for c in range(len(RISK_LEVELS))]
    route_tv = [tv_distance(v, route_mean) for v in route_vectors]
    risk_tv = [tv_distance(v, risk_mean) for v in risk_vectors]
    route_entropy = [entropy(v) for v in route_vectors]
    risk_entropy = [entropy(v) for v in risk_vectors]
    margins = []
    for v in route_vectors:
        top_two = sorted(v, reverse=True)[:2]
        margins.append(top_two[0] - top_two[1])
    ev_gaps = [
        abs(r["risk_score"] - sum(i * r["risk_probs"][i] for i in RISK_LEVELS)) for r in runs
    ]
    risk_slope, risk_r = trend(risk)
    review_slope, review_r = trend(review)
    r_crit = r_critical(n)

    risk_sd = stdev(risk) if n > 1 else 0.0
    if n < 2:
        verdict, vclass = "Not enough successful runs to assess variance.", "bad"
    elif agreement == 1 and level_agreement == 1 and risk_sd < 0.05:
        verdict, vclass = "Highly repeatable: same route and risk level in every run.", ""
    elif agreement >= 0.9 and level_agreement >= 0.9:
        verdict, vclass = "Mostly repeatable: decisions matched in at least 90% of runs.", "warn"
    else:
        verdict, vclass = "Variable: decisions changed across identical runs.", "bad"

    metrics = [
        ("Attorney review probability", review, "prob"),
        (f"Route probability: {modal}", modal_probs, "prob"),
        ("Route confidence", route_conf, "prob"),
        ("Contract risk score (0-3)", risk, "score"),
        ("Risk confidence", risk_conf, "prob"),
        (f"P(risk level {top_risk_level}: {RISK_NAMES[top_risk_level]})", top_risk_probs, "prob"),
        ("Request latency", latency, "ms"),
    ]

    charts = "".join(
        f"<div>{line_chart(title, values, kind)}</div>"
        for title, values, kind in [
            ("Attorney review probability", review, "prob"),
            (f"Route probability: {modal}", modal_probs, "prob"),
            ("Contract risk score (0-3)", risk, "score"),
            ("Request latency", latency, "ms"),
        ]
    )

    bars = "".join(
        f'<div class="row">{esc(option)} &mdash; {counts.get(option, 0)} of {n} runs '
        f"({counts.get(option, 0) / n * 100:.0f}%)"
        f'<div class="bar"><div class="fill" style="width:{counts.get(option, 0) / n * 100:.1f}%"></div></div></div>'
        for option in ROUTE_OPTIONS
    )

    route_heat = heatmap(ROUTE_OPTIONS, route_vectors, ROUTE_OPTIONS)
    risk_heat = heatmap(
        [str(i) for i in RISK_LEVELS], risk_vectors,
        [f"{i} {RISK_NAMES[i]}" for i in RISK_LEVELS],
    )

    def dist_stat(label: str, values: list[float], kind: str) -> str:
        d = describe(values)
        return (
            f"<tr><td>{esc(label)}</td><td>{fmt_spread(kind, d['mean'])}</td>"
            f"<td>{fmt_spread(kind, d['max'])}</td></tr>"
        )

    dist_rows = "".join([
        dist_stat("Route: total variation distance of each run from the mean distribution", route_tv, "prob"),
        dist_stat("Risk: total variation distance of each run from the mean distribution", risk_tv, "prob"),
        f"<tr><td>Route: normalized Shannon entropy (0 = certain, 1 = uniform)</td>"
        f"<td>{mean(route_entropy):.4f}</td><td>{max(route_entropy):.4f}</td></tr>",
        f"<tr><td>Risk: normalized Shannon entropy (0 = certain, 1 = uniform)</td>"
        f"<td>{mean(risk_entropy):.4f}</td><td>{max(risk_entropy):.4f}</td></tr>",
        f"<tr><td>Route: margin between top two options</td>"
        f"<td>{mean(margins) * 100:.3f}%</td><td>min {min(margins) * 100:.3f}%</td></tr>",
        f"<tr><td>Risk: |reported score &minus; expected value &Sigma;(level &times; probability)|</td>"
        f"<td>{mean(ev_gaps):.6f}</td><td>{max(ev_gaps):.6f}</td></tr>",
    ])

    def drift_text(name: str, slope: float, r: float, kind: str) -> str:
        if slope != slope:
            return f"<tr><td>{esc(name)}</td><td>n/a</td><td>n/a</td><td>n/a</td></tr>"
        unit = f"{slope:.6f}" if kind == "score" else f"{slope * 100:.5f} pp"
        verdict_text = "possible drift" if abs(r) > r_crit else "no significant trend"
        return (
            f"<tr><td>{esc(name)}</td><td>{unit} per run</td><td>{r:+.3f}</td>"
            f"<td>{verdict_text}</td></tr>"
        )

    drift_rows = drift_text("Attorney review probability", review_slope, review_r, "prob") + \
        drift_text("Contract risk score", risk_slope, risk_r, "score")

    run_rows = "".join(
        f"<tr><td>{r['index']}</td><td>{esc(r['time'])}</td><td class='num'>{r['latency']:.0f} ms</td>"
        f"<td class='num'>{r['review'] * 100:.3f}%</td><td>{esc(r['route_choice'])}</td>"
        f"<td class='num'>{r['route_conf'] * 100:.3f}%</td><td class='num'>{r['risk_score']:.4f}</td>"
        f"<td class='num'>{r['risk_conf'] * 100:.3f}%</td>"
        f"<td class='num'>{esc(r['in_tokens'])}/{esc(r['out_tokens'])}</td></tr>"
        for r in runs
    )
    failure_html = ""
    if ctx["failed"]:
        items = "".join(
            f"<li>Run {f['index']}: {esc(f['error'])}</li>" for f in ctx["failed"]
        )
        failure_html = (
            f'<section><h2>Failed runs</h2><p class="sub">Excluded from all statistics.</p>'
            f"<ul>{items}</ul></section>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Decision Model Repeatability Report</title>
<style>{base.STYLE}{EXTRA_STYLE}</style>
</head>
<body>
<header>
<h1>Decision Model Repeatability Report</h1>
<p>Use case 4: contract review triage &bull; Deployment: {esc(ctx['deployment'])} &bull; Model: {esc(ctx['model'])}
&bull; Started {esc(ctx['started'])}</p>
</header>
<main>
<section>
<h2>1. Summary</h2>
<div class="verdict {vclass}">{esc(verdict)}</div>
<div class="cards">
<div class="card"><div class="label">Successful runs</div>
<div class="value">{n} / {ctx['requested']}</div>
<div class="sub">{len(ctx['failed'])} failed. Delay between runs: {ctx['delay']:g} s.</div></div>
<div class="card"><div class="label">Route agreement</div>
<div class="value">{agreement * 100:.0f}%</div>
<div class="sub">{esc(modal)} in {modal_count} of {n} runs.</div></div>
<div class="card"><div class="label">Risk level agreement</div>
<div class="value">{level_agreement * 100:.0f}%</div>
<div class="sub">Score rounded to the nearest level ({esc(RISK_NAMES[level_mode])} in {level_mode_count} runs).</div></div>
<div class="card"><div class="label">Attorney review probability</div>
<div class="value">{mean(review) * 100:.2f}%</div>
<div class="sub">SD {(stdev(review) if n > 1 else 0) * 100:.4f} pp, range {(max(review) - min(review)) * 100:.4f} pp.</div></div>
<div class="card"><div class="label">Contract risk score</div>
<div class="value">{mean(risk):.4f}</div>
<div class="sub">SD {risk_sd:.4f}, range {max(risk) - min(risk):.4f} on a 0-3 scale.</div></div>
<div class="card"><div class="label">Mean latency</div>
<div class="value">{mean(latency):.0f} ms</div>
<div class="sub">SD {(stdev(latency) if n > 1 else 0):.0f} ms.</div></div>
</div>
</section>
<section>
<h2>2. Experiment Setup</h2>
<table>
<tr><th>Item</th><th>Value</th></tr>
<tr><td>Runs requested / completed</td><td>{ctx['requested']} / {n}</td></tr>
<tr><td>Delay between runs</td><td>{ctx['delay']:g} seconds (no wait after the final run)</td></tr>
<tr><td>Started / finished</td><td>{esc(ctx['started'])} / {esc(ctx['finished'])}</td></tr>
<tr><td>Request body SHA-256 (identical for every run)</td><td class="mono">{esc(ctx['sha256'])}</td></tr>
<tr><td>Request body size</td><td>{ctx['body_bytes']} bytes</td></tr>
</table>
<h3>Input sent in every run</h3>
<div class="input">{base.input_html(STATE)}</div>
</section>
<section>
<h2>3. Variation Across Runs</h2>
<p class="sub">Each chart is zoomed to its own data range, so small differences look larger than
they are. Read the axis labels. Dashed line = mean; shaded band = &plusmn;1 sample SD; hover a point for its value.</p>
<div class="charts">{charts}</div>
<h3>Route decision frequency</h3>
<div class="bars">{bars}</div>
<h3>Route probabilities by run: deviation from each option's mean</h3>
<p class="sub">Cell colour: blue above the option's mean, orange below; intensity is relative to that
column's largest deviation. Grey means no variation.</p>
{route_heat}
<h3>Risk level probabilities by run: deviation from each level's mean</h3>
{risk_heat}
</section>
<section>
<h2>4. Descriptive Statistics</h2>
<p class="sub">SD = sample standard deviation (n&minus;1). CV = SD / |mean|. CI = Student t interval for
the mean. pp = percentage points. Logit SD measures spread of a probability on the log-odds
scale, which is more informative than raw SD when probabilities sit near 0 or 1.</p>
{stat_table(metrics)}
</section>
<section>
<h2>5. Distribution-Level Metrics</h2>
<table>
<tr><th>Metric</th><th>Mean across runs</th><th>Worst run</th></tr>
{dist_rows}
</table>
<h3>Trend across run order</h3>
<p class="sub">Least-squares slope and Pearson r against run number. With n = {n}, |r| above
{r_crit:.3f} is significant at the 5% level (two-sided).</p>
<table>
<tr><th>Metric</th><th>Slope</th><th>Pearson r</th><th>Interpretation</th></tr>
{drift_rows}
</table>
</section>
<section>
<h2>6. Per-Run Results</h2>
<table>
<tr><th>Run</th><th>Time</th><th>Latency</th><th>Review prob.</th><th>Route</th><th>Route conf.</th>
<th>Risk score</th><th>Risk conf.</th><th>Tokens in/out</th></tr>
{run_rows}
</table>
</section>
{failure_html}
<section>
<h2>7. How to Read and Limits</h2>
<ul>
<li>Every run used the same request body (same hash), the same deployment, and ran within one session.</li>
<li>Decisions were stable if route agreement and risk level agreement are 100%. Verdict thresholds
(100% agreement and score SD below 0.05 for "highly repeatable"; at least 90% for "mostly") are
illustrative, not a standard.</li>
<li>Small probability differences between runs show the numbers are not bit-for-bit identical. Whether
that matters depends on your decision thresholds; compare the spread with the margin between your
threshold and the observed values.</li>
<li>With {n} runs, a rare flip may not appear. Confidence intervals describe the mean, not the chance of
an unusual run.</li>
<li>These results do not cover other inputs, rephrased criteria, or later model updates. Repeat the
experiment after changing any of them, and on borderline cases where the top options are close.</li>
</ul>
</section>
</main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="Number of runs.")
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY, help="Seconds to sleep between runs."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="HTML report path.")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1.")
    if args.delay < 0:
        parser.error("--delay must not be negative.")

    base.load_env_file()
    deployment = os.environ.get("DEPLOYMENT_NAME", "").strip()
    if not deployment:
        parser.error("Set DEPLOYMENT_NAME in .env (copy .env.example).")
    endpoint = os.environ.get("AZURE_ENDPOINT", "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
    ):
        parser.error("Set AZURE_ENDPOINT in .env to the HTTPS resource endpoint (no path).")
    token = get_token(parser)

    body = json.dumps(
        {"model": deployment, "state": STATE, "questions": QUESTIONS}
    ).encode("utf-8")
    sha256 = hashlib.sha256(body).hexdigest()
    url = f"{endpoint}/providers/microsoft/v1/systemone"

    ok: list[dict] = []
    failed: list[dict] = []
    model_name = "n/a"
    started = datetime.now().astimezone()
    print(f"Request SHA-256: {sha256}", file=sys.stderr)
    for index in range(1, args.runs + 1):
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        try:
            result, latency = call_once(url, body, token)
            parsed_run = parse_run(result)
            model_name = str(result.get("model", model_name))
            ok.append({"index": index, "time": stamp, "latency": latency, **parsed_run})
            print(
                f"Run {index}/{args.runs}: review={parsed_run['review']:.4f} "
                f"route={parsed_run['route_choice']} risk={parsed_run['risk_score']:.4f} "
                f"({latency:.0f} ms)",
                file=sys.stderr,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            failed.append({"index": index, "error": f"HTTP {exc.code}: {detail}"})
            print(f"Run {index}/{args.runs}: HTTP {exc.code}", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            failed.append({"index": index, "error": str(exc)})
            print(f"Run {index}/{args.runs}: {exc}", file=sys.stderr)
        if index < args.runs and args.delay > 0:
            time.sleep(args.delay)
    finished = datetime.now().astimezone()

    if not ok:
        print("All runs failed; no report written.", file=sys.stderr)
        return 1

    report = render_report(
        {
            "ok": ok, "failed": failed, "requested": args.runs, "delay": args.delay,
            "deployment": deployment, "model": model_name, "sha256": sha256,
            "body_bytes": len(body),
            "started": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "finished": finished.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }
    )
    try:
        args.output.write_text(report, encoding="utf-8")
    except OSError as exc:
        print(f"Could not write the report: {exc}", file=sys.stderr)
        return 1
    print(f"HTML report written to {args.output.resolve()}", file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
