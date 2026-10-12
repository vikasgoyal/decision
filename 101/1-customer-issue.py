r"""Call Microsoft-Decision-1 with the support request from the screenshot.

Requires Python 3.10+; no third-party packages are needed.
Uses the Decision-1 deployment and your Azure CLI sign-in. In PowerShell:

    az login
    py .\1-customer-issue.py

Configuration comes from the .env file next to this script (copy .env.example):
AZURE_ENDPOINT (HTTPS resource base URL, no path) and DEPLOYMENT_NAME. Real
environment variables take precedence over .env. Optionally set AZURE_ENTRA_TOKEN
to supply an access token instead of obtaining one through Azure CLI.

This resource has key-based authentication disabled, so only Entra ID is used.
An explicitly supplied token must be unexpired. Your signed-in identity needs
an inference role on the resource, such as Cognitive Services User.

Inspect the request without credentials or an endpoint:

    python .\1-customer-issue.py --dry-run

Use --state "Your customer message" to try another request.
The model field is your deployment name, not necessarily Microsoft-Decision-1.
The script prints the complete JSON response, including answers and usage.

A self-contained HTML report (Arial) with the input, questions, criteria, and
results is written next to this script as 1-customer-issue-report.html.
Use --output PATH to choose another location.

Reference:
https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision
"""

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


ENV_FILE = Path(__file__).with_name(".env")
DEFAULT_STATE = (
    "My account payment has failed twice today. "
    "I need help before the end of the day."
)
QUESTIONS = {
    "urgency": {
        "type": "noul",
        "instructions": "Does the customer need urgent help?",
    },
    "team": {
        "type": "choice",
        "instructions": "Which team should handle the request?",
        "criteria": {
            "billing": "Payment and invoice issues",
            "technical": "Software and integration issues",
        },
    },
    "priority": {
        "type": "score",
        "instructions": "How high is the priority of this request?",
        "criteria": [
            "Routine, no time pressure",
            "Important, some time pressure",
            "Urgent, immediate attention needed",
        ],
    },
}


DEFAULT_OUTPUT = Path(__file__).with_name("1-customer-issue-report.html")

STYLE = """
body { font-family: Arial, Helvetica, sans-serif; color: #1f2933; background: #f3f5f8;
       margin: 0; line-height: 1.5; }
header { background: #1b2a41; color: #fff; padding: 28px 48px; }
header h1 { margin: 0 0 4px; font-size: 24px; }
header p { margin: 0; color: #c9d3e0; font-size: 13px; }
main { max-width: 1000px; margin: 0 auto; padding: 24px 48px 48px; }
section { background: #fff; border: 1px solid #d9dee5; border-radius: 6px;
          padding: 20px 24px; margin-bottom: 20px; }
h2 { font-size: 17px; margin: 0 0 14px; color: #1b2a41;
     border-bottom: 2px solid #e4e8ee; padding-bottom: 8px; }
h3 { font-size: 14px; margin: 18px 0 6px; color: #3e4c59; }
.input { font-size: 13px; background: #f7f9fb; border: 1px solid #e4e8ee;
         border-radius: 4px; padding: 4px 18px 10px; }
.input h3 { margin: 16px 0 6px; color: #1b2a41; font-size: 13px; text-transform: uppercase;
            letter-spacing: .04em; }
.input p { margin: 10px 0 8px; }
.input ul { margin: 0 0 8px; padding-left: 20px; }
.input li { margin-bottom: 4px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #e4e8ee;
         vertical-align: top; }
th { background: #f7f9fb; color: #3e4c59; }
td ul { margin: 0; padding-left: 18px; }
td li { margin-bottom: 6px; }
section > ul { font-size: 13px; margin: 0; padding-left: 20px; }
section > ul li { margin-bottom: 4px; }
.cards { display: flex; gap: 16px; flex-wrap: wrap; }
.card { flex: 1 1 260px; border: 1px solid #d9dee5; border-radius: 6px; padding: 16px; }
.label { font-size: 12px; color: #616e7c; text-transform: uppercase; letter-spacing: .04em; }
.value { font-size: 22px; font-weight: bold; margin: 4px 0; color: #1b2a41; }
.sub { font-size: 12px; color: #616e7c; }
.bar { background: #e4e8ee; border-radius: 3px; height: 8px; margin-top: 4px; }
.fill { background: #2f6fb3; height: 8px; border-radius: 3px; }
.row { margin-top: 10px; font-size: 12px; }
.note { font-size: 12px; color: #52606d; background: #fff8e6; border: 1px solid #f0dca5;
        border-radius: 4px; padding: 12px; }
"""


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE lines without overriding existing environment variables."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def pct(value) -> str:
    return f"{value * 100:.2f}%" if is_number(value) else "n/a"


def number(value, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if is_number(value) else "n/a"


def input_html(text: str) -> str:
    """Render paragraphs, headings, and bullets from the plain-text state."""
    parts: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if paragraph:
            parts.append(f"<p>{esc(' '.join(paragraph))}</p>")
            paragraph.clear()
        if bullets:
            items = "".join(f"<li>{esc(item)}</li>" for item in bullets)
            parts.append(f"<ul>{items}</ul>")
            bullets.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
        elif line.endswith(":") and line.upper() == line and len(line) < 80:
            flush()
            parts.append(f"<h3>{esc(line.rstrip(':').title())}</h3>")
        elif line.startswith("- "):
            if paragraph:
                flush()
            bullets.append(line[2:].strip())
        elif bullets and raw[:1].isspace():
            bullets[-1] += " " + line
        else:
            if bullets:
                flush()
            paragraph.append(line)
    flush()
    return "".join(parts)


def criteria_html(criteria) -> str:
    if isinstance(criteria, dict):
        items = [f"<li><strong>{esc(k)}:</strong> {esc(v)}</li>" for k, v in criteria.items()]
    elif isinstance(criteria, list):
        items = [f"<li>{esc(v)}</li>" for v in criteria]
    else:
        return "<em>None supplied</em>"
    return f"<ul>{''.join(items)}</ul>"


def prob_rows(probabilities, labels=None) -> str:
    if not isinstance(probabilities, dict):
        return ""
    rows = []
    for key, value in probabilities.items():
        label = labels.get(key, key) if isinstance(labels, dict) else key
        width = max(0.0, min(100.0, value * 100)) if is_number(value) else 0.0
        rows.append(
            f'<div class="row"><div>{esc(label)} &mdash; {pct(value)}</div>'
            f'<div class="bar"><div class="fill" style="width:{width:.2f}%"></div></div></div>'
        )
    return "".join(rows)


def render_report(state: str, deployment: str, result: dict) -> str:
    answers = result["answers"]
    urgency = answers["urgency"]
    team = answers["team"]
    priority = answers["priority"]
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}

    levels = QUESTIONS["priority"]["criteria"]
    legend = priority.get("legend") if isinstance(priority.get("legend"), dict) else {}
    score = priority.get("score")
    level_name = ""
    if is_number(score):
        nearest = str(min(max(round(score), 0), len(levels) - 1))
        level_name = str(legend.get(nearest, levels[int(nearest)]))
    team_name = str(team.get("choice", "n/a"))
    team_desc = QUESTIONS["team"]["criteria"].get(team_name, "")

    question_rows = "".join(
        f"<tr><td>{esc(name.title())}</td><td>{esc(q['type'])}</td>"
        f"<td>{esc(q['instructions'])}</td><td>{criteria_html(q.get('criteria'))}</td></tr>"
        for name, q in QUESTIONS.items()
    )
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    level_text = f" &mdash; {esc(level_name.split(',')[0])}" if level_name else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Customer Issue Triage Report</title>
<style>{STYLE}</style>
</head>
<body>
<header>
<h1>Customer Issue Triage Report</h1>
<p>Generated {esc(generated)} &bull; Deployment: {esc(deployment)} &bull; Model: {esc(result.get("model", "n/a"))}</p>
</header>
<main>
<section>
<h2>1. Input</h2>
<div class="input">{input_html(state)}</div>
</section>
<section>
<h2>2. Questions Asked</h2>
<table>
<tr><th>Decision</th><th>Type</th><th>Instructions</th><th>Criteria</th></tr>
{question_rows}
</table>
</section>
<section>
<h2>3. Results</h2>
<div class="cards">
<div class="card">
<div class="label">Urgent help needed</div>
<div class="value">{pct(urgency.get("noul"))}</div>
<div class="sub">Probability that the customer needs urgent help.</div>
</div>
<div class="card">
<div class="label">Recommended team</div>
<div class="value">{esc(team_name.title())}</div>
<div class="sub">Confidence {pct(team.get("confidence"))}. {esc(team_desc)}</div>
</div>
<div class="card">
<div class="label">Priority</div>
<div class="value">{number(score)} / {len(levels) - 1}{level_text}</div>
<div class="sub">Confidence {pct(priority.get("confidence"))}.</div>
</div>
</div>
<h3>Team probabilities</h3>
{prob_rows(team.get("probabilities"))}
<h3>Priority probabilities</h3>
{prob_rows(priority.get("probabilities"), legend)}
</section>
<section>
<h2>4. Usage</h2>
<table>
<tr><th>Input tokens</th><th>Output tokens</th></tr>
<tr><td>{esc(usage.get("input_tokens", "n/a"))}</td><td>{esc(usage.get("output_tokens", "n/a"))}</td></tr>
</table>
</section>
<section>
<h2>5. Understanding the Decision Types</h2>
<p class="sub">All three types take a question and criteria. They differ in the shape of the answer.</p>
<table>
<tr><th>Type</th><th>Question it answers</th><th>Output</th><th>Best used for</th><th>In this report</th></tr>
<tr><td><strong>noul</strong></td><td>Is this true?</td>
<td>One probability from 0 to 1. There are no options.</td>
<td>Gates and filters. Your code sets the cutoff, for example above 0.9 is urgent.</td>
<td>Urgent help needed</td></tr>
<tr><td><strong>choice</strong></td><td>Which one is it?</td>
<td>One selected label, plus a probability for every option.</td>
<td>Routing and classification into unordered categories.</td>
<td>Recommended team</td></tr>
<tr><td><strong>score</strong></td><td>How much?</td>
<td>A value on an ordered scale, plus a probability for each level.</td>
<td>Ranking and prioritizing, where levels have an order and you sort or threshold.</td>
<td>Priority</td></tr>
</table>
<h3>How to choose</h3>
<ul>
<li>Use <strong>noul</strong> when the answer is yes or no.</li>
<li>Use <strong>choice</strong> when you are picking a bucket and the options are alternatives, not a scale.</li>
<li>Use <strong>score</strong> when the levels are ordered. A split between two adjacent levels yields a score between them, which a single label cannot express.</li>
</ul>
</section>
<div class="note"><strong>Important:</strong> This report is an automated triage
recommendation. Review low-confidence results before acting, and validate thresholds
on labeled examples before automating routing.</div>
</main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=DEFAULT_STATE, help="Customer message.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the request without sending it."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="HTML report path."
    )
    args = parser.parse_args()
    if not args.state.strip():
        parser.error("--state must not be empty.")

    load_env_file()
    deployment = os.environ.get("DEPLOYMENT_NAME", "").strip()
    if not deployment:
        if not args.dry_run:
            parser.error("Set DEPLOYMENT_NAME in .env (copy .env.example).")
        deployment = "<DEPLOYMENT_NAME>"
    payload = {
        "model": deployment,
        "state": args.state,
        "questions": QUESTIONS,
    }
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    endpoint = os.environ.get("AZURE_ENDPOINT", "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        parser.error("Set AZURE_ENDPOINT in .env to the HTTPS resource endpoint (no path).")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = os.environ.get("AZURE_ENTRA_TOKEN", "").strip()
    if not token:
        az = shutil.which("az")
        if not az:
            parser.error("Install Azure CLI and run az login, or set AZURE_ENTRA_TOKEN.")
        try:
            token_result = subprocess.run(
                [
                    az, "account", "get-access-token",
                    "--resource", "https://cognitiveservices.azure.com",
                    "--query", "accessToken", "-o", "tsv",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print(
                f"Azure CLI token acquisition failed. Run az login.\n{exc.stderr}",
                file=sys.stderr,
            )
            return 1
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"Could not run Azure CLI: {exc}", file=sys.stderr)
            return 1
        token = token_result.stdout.strip()
        if not token:
            print("Azure CLI returned an empty access token.", file=sys.stderr)
            return 1
    headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        f"{endpoint}/providers/microsoft/v1/systemone",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"Decision API returned HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Could not reach the Decision API: {exc}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"Decision API returned invalid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        print("Decision API response is missing an answers object.", file=sys.stderr)
        return 1
    missing = QUESTIONS.keys() - result["answers"].keys()
    if missing:
        print(
            f"Decision API response is missing answers: {', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2))
    try:
        args.output.write_text(
            render_report(args.state, deployment, result), encoding="utf-8"
        )
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        print(f"Could not write the HTML report: {exc}", file=sys.stderr)
        return 1
    print(f"HTML report written to {args.output.resolve()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
