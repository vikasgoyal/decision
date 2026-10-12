r"""Qualify and route a sales inquiry with Microsoft-Decision-1.

Requires Python 3.10+; no third-party packages are needed.
Uses the Decision-1 deployment and your Azure CLI sign-in. In PowerShell:

    az login
    py .\2-sales-lead-qualify.py

Preview the request without authentication or an API call:

    py .\2-sales-lead-qualify.py --dry-run

Try a different inquiry:

    py .\2-sales-lead-qualify.py --state "We are researching inventory software."

Configuration comes from the .env file next to this script (copy .env.example):
AZURE_ENDPOINT (HTTPS resource base URL, no path) and DEPLOYMENT_NAME. Real
environment variables take precedence over .env. Optionally set AZURE_ENTRA_TOKEN
(an unexpired Entra ID access token); otherwise a token is obtained from your
existing Azure CLI sign-in.
Your identity needs inference permissions, such as Cognitive Services User.
API keys are not used.

The response includes purchase readiness probability, a recommended sales
route, and follow-up priority on a 0-3 scale, along with probabilities and usage.
Routing criteria are illustrative business rules; adapt them to your process.
Use deterministic CRM rules for territory and existing account ownership.
These outputs are recommendations, not automatic CRM updates. Review uncertain
leads and validate thresholds on labeled examples before automating actions.

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
DEFAULT_STATE = (
    "We're a retail company with 120 stores looking for an inventory management "
    "solution. Budget is approved, and we want to launch within three months. "
    "Please arrange a demo with our procurement director."
)
QUESTIONS = {
    "purchase_readiness": {
        "type": "noul",
        "instructions": (
            "Does this inquiry indicate an active buying initiative rather than "
            "general research? Evaluate only the evidence in the inquiry."
        ),
        "criteria": {
            "true": (
                "A concrete business need with a buying signal such as approved "
                "budget, a planned rollout, or a request to evaluate vendors."
            ),
            "false": (
                "General research, educational interest, or curiosity without "
                "evidence of an active buying initiative."
            ),
        },
    },
    "sales_route": {
        "type": "choice",
        "instructions": (
            "Recommend exactly one sales route. Route explicit reseller or "
            "implementation-partner inquiries to partner_channel first. Otherwise "
            "route exploratory inquiries without active purchase intent to nurture. "
            "For active direct buyers, choose enterprise_sales or smb_sales based "
            "on the stated scale and complexity; do not invent company facts."
        ),
        "criteria": {
            "enterprise_sales": (
                "Active direct buyer with 50 or more stores/sites, or an explicitly "
                "complex organization-wide rollout or enterprise procurement process."
            ),
            "smb_sales": (
                "Active direct buyer seeking a standard solution for a smaller "
                "or single-site business, without enterprise-scale requirements."
            ),
            "partner_channel": (
                "Reseller, consultant, or implementation partner seeking to sell "
                "or deliver the solution to its own clients."
            ),
            "nurture": (
                "Exploratory interest or early research without evidence of an "
                "active buying initiative."
            ),
        },
    },
    "follow_up_priority": {
        "type": "score",
        "instructions": (
            "How promptly should sales follow up? Use the stated business need, "
            "budget, timeline, and vendor-evaluation activity. Do not assume "
            "missing budget approval or deadlines."
        ),
        "criteria": [
            "Low: Exploratory research without a concrete buying project.",
            "Medium: Defined business need, but funding or timeline is unclear.",
            "High: Funded project with a stated implementation timeline.",
            "Highest: Active vendor evaluation with a demo, proposal, or purchasing "
            "discussion requested for a funded, time-bound project.",
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
    parser.add_argument("--state", default=DEFAULT_STATE, help="Sales inquiry text.")
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
