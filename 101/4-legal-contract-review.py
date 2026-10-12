r"""Triage a vendor contract for legal review with Microsoft-Decision-1.

Requires Python 3.10+; no third-party packages are needed.
Uses the Decision-1 deployment and your Azure CLI sign-in. In PowerShell:

    az login
    py .\4-legal-contract-review.py

Preview the request without authentication or an API call:

    py .\4-legal-contract-review.py --dry-run

Use --state "Contract summary, playbook, and verified facts" for another case.
Replace the entire state, including playbook context, when testing another case.
The playbook and facts below are illustrative, not real legal rules or advice.

Configuration comes from the .env file next to this script (copy .env.example):
AZURE_ENDPOINT (HTTPS resource base URL, no path) and DEPLOYMENT_NAME. Real
environment variables take precedence over .env. Optionally set AZURE_ENTRA_TOKEN
(an unexpired Entra ID access token); otherwise a token is obtained from your
existing Azure CLI sign-in.
Your identity needs inference permissions, such as Cognitive Services User.
API keys are not used.

Outputs: attorney-review probability, recommended review route, and contract
risk level on a 0-3 scale, with probabilities and token usage.
The sample only prints triage recommendations. It does not give legal advice,
interpret contract terms, approve a contract, or replace attorney review.
In production, extract clause values and apply mandatory playbook rules
deterministically, then supply the verified results as model context.
Validate model results on labeled cases and keep attorneys in control of decisions.

A self-contained HTML report (Arial) with the input, questions, and results is
written next to this script as 4-legal-contract-review-report.html. Use
--output PATH to choose another location. The JSON response is also printed.

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
DEFAULT_STATE = """
CONTRACT SUMMARY:
A business team wants to sign a three-year, $450,000 SaaS agreement with a new
vendor that will process customer personal data. The vendor's draft limits its
liability to one month of fees, excludes liability for data breaches, allows
the vendor to use customer data to train its own products, and renews
automatically for three years unless cancelled 180 days before expiration. The
business team wants signature by Friday to meet a product launch date.

ILLUSTRATIVE CONTRACT PLAYBOOK:
- Contracts above $250,000 require commercial legal review.
- Processing customer personal data requires privacy counsel review.
- Liability caps below twelve months of fees require senior counsel approval.
- Vendor use of customer data for its own purposes requires privacy review
  and cannot be accepted without written approval.
- Cases combining a liability-cap deviation with data-protection concerns
  require senior counsel escalation as the primary route.
- Business deadlines do not waive playbook requirements.

VERIFIED FACTS FOR THIS EXAMPLE:
- Contract value exceeds the $250,000 threshold.
- The contract involves customer personal data.
- The liability cap is below twelve months of fees.
- The draft allows vendor use of customer data for product training.

UNCONFIRMED CLAIMS:
The business team says the launch depends on this vendor. No deviation has been
approved, and no alternative vendor has been evaluated.
""".strip()

QUESTIONS = {
    "attorney_review_required": {
        "type": "noul",
        "instructions": (
            "Does this contract require attorney review under the supplied "
            "playbook? Use verified facts; business deadlines do not waive "
            "playbook requirements."
        ),
        "criteria": {
            "true": (
                "At least one supplied playbook trigger requires legal review, "
                "such as contract value, personal data, or liability deviations."
            ),
            "false": (
                "The supplied facts establish that the contract is a standard, "
                "pre-approved form with no playbook trigger."
            ),
        },
    },
    "review_route": {
        "type": "choice",
        "instructions": (
            "Recommend the primary legal review route using the supplied playbook "
            "and facts. Apply the senior counsel escalation rule when its "
            "conditions are met. Do not approve the contract, interpret legal "
            "enforceability, or invent missing facts."
        ),
        "criteria": {
            "standard_template_approval": (
                "Pre-approved template or routine terms with no playbook trigger "
                "requiring attorney review."
            ),
            "commercial_legal_review": (
                "Value, term, renewal, or commercial terms need attorney review, "
                "without personal-data or liability-cap concerns requiring escalation."
            ),
            "privacy_counsel_review": (
                "Personal data or data-use terms are the primary issue, without "
                "the combination requiring senior counsel escalation."
            ),
            "senior_counsel_escalation": (
                "A liability-cap deviation and data-protection concerns coexist, "
                "or terms materially deviate from the playbook in multiple areas."
            ),
        },
    },
    "contract_risk": {
        "type": "score",
        "instructions": (
            "How much legal and commercial risk does this contract present "
            "relative to the supplied playbook? Weigh the deviations and data "
            "exposure. Do not treat a business deadline as reducing risk."
        ),
        "criteria": [
            "Low: Standard terms with no playbook deviation.",
            "Moderate: Limited deviation or review trigger with manageable exposure.",
            "High: Material deviations or sensitive data requiring counsel attention.",
            "Critical: Multiple material deviations combining data exposure, limited "
            "liability, and long-term commitment.",
        ],
    },
}


DEFAULT_OUTPUT = Path(__file__).with_name("4-legal-contract-review-report.html")

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
pre { font-family: Arial, Helvetica, sans-serif; font-size: 13px; background: #f7f9fb;
      border: 1px solid #e4e8ee; border-radius: 4px; padding: 14px; margin: 0;
      white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #e4e8ee;
         vertical-align: top; }
th { background: #f7f9fb; color: #3e4c59; }
.input { font-size: 13px; background: #f7f9fb; border: 1px solid #e4e8ee;
         border-radius: 4px; padding: 4px 18px 10px; }
.input h3 { margin: 16px 0 6px; color: #1b2a41; font-size: 13px; text-transform: uppercase;
            letter-spacing: .04em; }
.input p { margin: 0 0 8px; }
.input ul { margin: 0 0 8px; padding-left: 20px; }
.input li { margin-bottom: 4px; }
td ul { margin: 0; padding-left: 18px; }
section > ul { font-size: 13px; margin: 0; padding-left: 20px; }
section > ul li { margin-bottom: 4px; }
td li { margin-bottom: 6px; }
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
.badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 12px;
         font-weight: bold; color: #fff; }
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


def pct(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "n/a"
    return f"{value * 100:.2f}%"


def number(value, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "n/a"
    return f"{value:.{digits}f}"


def input_html(text: str) -> str:
    """Render headings, bullets, and wrapped paragraphs from the plain-text state."""
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
        return ""
    return f"<ul>{''.join(items)}</ul>"


def prob_rows(probabilities, labels=None) -> str:
    if not isinstance(probabilities, dict):
        return ""
    rows = []
    for key, value in probabilities.items():
        label = labels.get(key, key) if isinstance(labels, dict) else key
        width = (
            max(0.0, min(100.0, value * 100))
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else 0.0
        )
        rows.append(
            f'<div class="row"><div>{esc(label)} &mdash; {pct(value)}</div>'
            f'<div class="bar"><div class="fill" style="width:{width:.2f}%"></div></div></div>'
        )
    return "".join(rows)


def render_report(state: str, deployment: str, result: dict) -> str:
    answers = result["answers"]
    review = answers["attorney_review_required"]
    route = answers["review_route"]
    risk = answers["contract_risk"]
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}

    risk_labels = risk.get("legend") if isinstance(risk.get("legend"), dict) else {}
    risk_level = ""
    score = risk.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        nearest = str(min(max(round(score), 0), len(QUESTIONS["contract_risk"]["criteria"]) - 1))
        risk_level = str(risk_labels.get(nearest, "")).split(":")[0]
    route_name = str(route.get("choice", "n/a"))
    route_desc = QUESTIONS["review_route"]["criteria"].get(route_name, "")

    question_rows = "".join(
        f"<tr><td>{esc(name.replace('_', ' ').title())}</td><td>{esc(q['type'])}</td>"
        f"<td>{esc(q['instructions'])}</td><td>{criteria_html(q.get('criteria'))}</td></tr>"
        for name, q in QUESTIONS.items()
    )
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contract Review Triage Report</title>
<style>{STYLE}</style>
</head>
<body>
<header>
<h1>Contract Review Triage Report</h1>
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
<div class="label">Attorney review required</div>
<div class="value">{pct(review.get("noul"))}</div>
<div class="sub">Probability that the playbook requires attorney review.</div>
</div>
<div class="card">
<div class="label">Recommended review route</div>
<div class="value">{esc(route_name.replace("_", " ").title())}</div>
<div class="sub">Confidence {pct(route.get("confidence"))}. {esc(route_desc)}</div>
</div>
<div class="card">
<div class="label">Contract risk</div>
<div class="value">{number(score)} / {len(QUESTIONS["contract_risk"]["criteria"]) - 1}{" &mdash; " + esc(risk_level) if risk_level else ""}</div>
<div class="sub">Confidence {pct(risk.get("confidence"))}.</div>
</div>
</div>
<h3>Review route probabilities</h3>
{prob_rows(route.get("probabilities"))}
<h3>Contract risk probabilities</h3>
{prob_rows(risk.get("probabilities"), risk_labels)}
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
<td>Gates and filters. Your code sets the cutoff, for example above 0.9 goes to legal.</td>
<td>Attorney review required</td></tr>
<tr><td><strong>choice</strong></td><td>Which one is it?</td>
<td>One selected label, plus a probability for every option.</td>
<td>Routing and classification into unordered categories.</td>
<td>Review route</td></tr>
<tr><td><strong>score</strong></td><td>How much?</td>
<td>A value on an ordered scale, plus a probability for each level.</td>
<td>Ranking and prioritizing, where levels have an order and you sort or threshold.</td>
<td>Contract risk</td></tr>
</table>
<h3>How to choose</h3>
<ul>
<li>Use <strong>noul</strong> when the answer is yes or no.</li>
<li>Use <strong>choice</strong> when you are picking a bucket and the options are alternatives, not a scale.</li>
<li>Use <strong>score</strong> when the levels are ordered. A split between High and Critical yields a score between them, which a single label cannot express.</li>
</ul>
</section>
<div class="note"><strong>Important:</strong> This report is an automated triage
recommendation, not legal advice. The playbook and facts are illustrative. It does
not interpret contract terms, approve a contract, or replace attorney review.</div>
</main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", default=DEFAULT_STATE, help="Contract summary with playbook and facts."
    )
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
    payload = {"model": deployment, "state": args.state, "questions": QUESTIONS}
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
