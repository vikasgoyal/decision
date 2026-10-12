# Microsoft-Decision-1 samples

Four standalone Python samples for [Microsoft-Decision-1](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision) in Microsoft Foundry. Each sample sends one piece of text and several typed questions to the decision API, then prints the JSON response. Samples 1 and 4 also write an HTML report.

| Script | Scenario | Decisions |
|---|---|---|
| `1-customer-issue.py` | Support request triage | urgency (`noul`), team (`choice`), priority (`score`) |
| `2-sales-lead-qualify.py` | Sales lead qualification | purchase readiness, sales route, follow-up priority |
| `3-purchase-request.py` | Purchase exception triage | exception review, primary review route, review urgency |
| `4-legal-contract-review.py` | Contract review triage | attorney review, review route, contract risk |

The samples are illustrative. The policies and playbooks inside them are invented examples. They produce triage recommendations, not approvals or legal advice.

## Decision types

| Type | Answers | Output |
|---|---|---|
| `noul` | Is this true? | One probability from 0 to 1 |
| `choice` | Which option is it? | One selected option plus a probability for each option |
| `score` | How much? | A value on an ordered scale plus a probability for each level |

## Prerequisites

- Python 3.10 or later. No third-party packages are needed.
- A Microsoft-Decision-1 deployment in Microsoft Foundry.
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), signed in with `az login`.
- An identity with inference permissions on the resource, such as **Cognitive Services User**.

Authentication uses Microsoft Entra ID only. API keys are not used.

## Setup

1. Copy the example configuration:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Edit `.env`:

   | Variable | Description |
   |---|---|
   | `AZURE_ENDPOINT` | Foundry resource HTTPS base URL, with no path |
   | `DEPLOYMENT_NAME` | Your deployment name, which may differ from the model name |
   | `AZURE_ENTRA_TOKEN` | Optional access token. If unset, one is obtained from Azure CLI |

   Environment variables already set in your shell take precedence over `.env`.

3. Sign in:

   ```powershell
   az login
   ```

## Run

```powershell
py .\1-customer-issue.py
py .\2-sales-lead-qualify.py
py .\3-purchase-request.py
py .\4-legal-contract-review.py
```

Common options:

| Option | Effect |
|---|---|
| `--state "text"` | Use your own input instead of the default example |
| `--dry-run` | Print the request without authenticating or calling the API |
| `--output PATH` | Choose the HTML report path (samples 1 and 4) |

Samples 3 and 4 expect the state to contain the request, applicable policy and verified facts. Replace the whole state when testing another case.

## Output

Each run prints the full JSON response. Samples 1 and 4 also write a self-contained HTML report next to the script (`1-customer-issue-report.html`, `4-legal-contract-review-report.html`). The report is an output file; open it in a browser rather than running it:

```powershell
start .\1-customer-issue-report.html
```

The report shows the input, the questions with their criteria, the results with probability bars, token usage, and an explanation of the decision types.

## Security

- `.env` is listed in `.gitignore`; commit only `.env.example`.
- Never commit access tokens.
- Generated reports contain your input text and are also ignored by git.
- Compute limits, dates and mandatory policy rules deterministically in production, and keep people in charge of final decisions.
