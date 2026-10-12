r"""Triage a complex purchase request with Microsoft-Decision-1.

Requires Python 3.10+; no third-party packages are needed.
Uses the Decision-1 deployment and your Azure CLI sign-in. In PowerShell:

    az login
    py .\3-purchase-request.py

Preview the request without authentication or an API call:

    py .\3-purchase-request.py --dry-run

Use --state "Request, applicable policy, and verified facts" for another case.
Replace the entire state, including policy context, when testing another policy.
The policy and facts below are illustrative, not authoritative company rules.

Configuration comes from the .env file next to this script (copy .env.example):
AZURE_ENDPOINT (HTTPS resource base URL, no path) and DEPLOYMENT_NAME. Real
environment variables take precedence over .env. Optionally set AZURE_ENTRA_TOKEN
(an unexpired Entra ID access token); otherwise a token is obtained from your
existing Azure CLI sign-in.
Your identity needs inference permissions, such as Cognitive Services User.
API keys are not used.

Outputs: exception-review probability, recommended primary review route, and
review urgency on a 0-3 scale, with probabilities and token usage.
The sample only prints recommendations; it does not approve or execute purchases.
Selecting a primary route must not bypass mandatory parallel reviews.
In production, compute monetary limits, date comparisons, and mandatory policy
triggers deterministically. Supply those verified results as model context.
Human reviewers retain approval authority; validate model results on labeled cases.

Reference:
https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ENV_FILE = Path(__file__).with_name(".env")
DEFAULT_STATE = """
PURCHASE REQUEST:
Operations requests a $180,000 renewal with its existing logistics provider.
The supplier proposes an 18% price increase. Switching vendors would take
eight weeks, but the contract expires in ten days. The supplier's insurance
certificate has expired. Operations requests a sole-source exception because
a service interruption could delay customer deliveries.

ILLUSTRATIVE PROCUREMENT POLICY:
- Renewal price increases above 5% require commercial negotiation review.
- Expired supplier insurance requires supplier-risk review before commitment.
- Sole-source renewals require documented justification and exception approval.
- Cases combining a policy exception, supplier risk, and material continuity
  concerns require a cross-functional exception committee as the primary route.
- The primary route coordinates but does not replace commercial and risk reviews.
- Urgency does not waive insurance requirements or authorize purchase approval.

VERIFIED FACTS FOR THIS EXAMPLE:
- Proposed increase exceeds the 5% limit; commercial review is mandatory.
- Insurance is expired; supplier-risk review is mandatory.
- Sole-source exception approval is required.
- Replacement lead time exceeds the time remaining before contract expiration.

UNCONFIRMED CLAIMS:
Operations reports potential delivery delays. Their scope and likelihood have
not been independently verified. No exception or insurance waiver is approved.
""".strip()

QUESTIONS = {
    "exception_review_required": {
        "type": "noul",
        "instructions": (
            "Does this purchase request require policy-exception or mandatory "
            "nonstandard review under the supplied policy? Use verified facts; "
            "do not treat the requester's urgency as permission to waive policy."
        ),
        "criteria": {
            "true": (
                "At least one supplied policy trigger requires exception approval "
                "or mandatory commercial or supplier-risk review."
            ),
            "false": (
                "The supplied facts establish that the request satisfies standard "
                "policy with no exception or mandatory nonstandard review."
            ),
        },
    },
    "primary_review_route": {
        "type": "choice",
        "instructions": (
            "Recommend the primary coordinating review route using the supplied "
            "policy and evidence. Apply the cross-functional rule when its conditions "
            "are met. A primary route never replaces required parallel reviews. "
            "Do not approve the purchase, waive policy, or invent missing facts."
        ),
        "criteria": {
            "standard_procurement": (
                "Routine compliant request with no policy exception, supplier-risk "
                "trigger, or material continuity concern requiring escalation."
            ),
            "commercial_negotiation": (
                "Pricing or terms require review, without the combination of "
                "exception, supplier risk, and continuity concerns requiring a committee."
            ),
            "supplier_risk_review": (
                "Supplier compliance or insurance is the primary issue, without "
                "the combination requiring a cross-functional committee."
            ),
            "cross_functional_exception_committee": (
                "A policy exception, supplier risk, and material business-continuity "
                "concerns coexist; procurement, risk, and operations must coordinate."
            ),
        },
    },
    "review_urgency": {
        "type": "score",
        "instructions": (
            "How urgently must reviewers assess this request to preserve compliant "
            "business-continuity options? Weigh remaining time, replacement lead "
            "time, and evidence of business impact. Distinguish reported risk from "
            "a verified active interruption. Urgency is not purchase approval."
        ),
        "criteria": [
            "Routine: No near-term deadline or material continuity concern.",
            "Time-sensitive: A deadline exists, with enough time for normal review "
            "and alternatives.",
            "Urgent: A near-term deadline and constrained alternatives create a "
            "credible continuity risk requiring expedited coordinated review.",
            "Immediate business-continuity concern: A verified active interruption "
            "or imminent unavoidable disruption requires immediate coordination.",
        ],
    },
}


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", default=DEFAULT_STATE, help="Purchase request with policy and facts."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the request without sending it."
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
