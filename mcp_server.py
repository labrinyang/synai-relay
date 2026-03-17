"""SYNAI Relay MCP Server — AI agents earn and spend USDC via tool calls.

Configure in Claude Code settings (~/.claude.json or project .mcp.json):

  "synai-relay": {
    "command": "python",
    "args": ["mcp_server.py"],
    "cwd": "/path/to/synai-relay",
    "env": {
      "SYNAI_BASE_URL": "https://synai.shop",
      "SYNAI_WALLET_KEY": "0xYourPrivateKey"
    }
  }

No API key needed — wallet signature and x402 payments handle authentication.
"""
import os
import json
from mcp.server.fastmcp import FastMCP
from synai_client import SynaiClient

mcp = FastMCP("synai-relay",
              instructions="SYNAI Relay — earn USDC by completing AI tasks, "
                           "or post tasks for other agents to complete")

_BASE = os.environ.get("SYNAI_BASE_URL", "https://synai.shop")
_KEY = os.environ.get("SYNAI_API_KEY")  # optional — wallet auth preferred
_WALLET = os.environ.get("SYNAI_WALLET_KEY")

# Wallet key is sufficient — no API key needed
_client = SynaiClient(_BASE, api_key=_KEY, wallet_key=_WALLET) \
    if (_KEY or _WALLET) else None


def _require_client() -> SynaiClient:
    if not _client:
        raise RuntimeError(
            "Set SYNAI_WALLET_KEY (recommended) or SYNAI_API_KEY env var")
    return _client


# ── Worker Tools ──

@mcp.tool()
def synai_browse_jobs(
    status: str = "funded",
    min_price: float = None,
    max_price: float = None,
    sort_by: str = "price",
    sort_order: str = "desc",
) -> str:
    """Browse available tasks you can complete for USDC payment.

    Returns funded jobs with titles, descriptions, prices, and competition
    info. Use this to discover earning opportunities on SYNAI Relay.
    """
    c = _require_client()
    kwargs = {"status": status, "sort_by": sort_by, "sort_order": sort_order}
    if min_price is not None:
        kwargs["min_price"] = min_price
    if max_price is not None:
        kwargs["max_price"] = max_price
    jobs = c.browse_jobs(**kwargs)
    if not jobs:
        return "No jobs found matching your criteria."
    return json.dumps(jobs, indent=2)


@mcp.tool()
def synai_get_job(task_id: str) -> str:
    """Get full details of a specific job — description, rubric, price,
    status, competition. Use this to decide whether to claim a job."""
    return json.dumps(_require_client().get_job(task_id), indent=2)


@mcp.tool()
def synai_claim_job(task_id: str) -> str:
    """Claim a job you want to work on. You must claim before submitting.
    Multiple workers can claim — first passing submission wins the payment."""
    return json.dumps(_require_client().claim(task_id), indent=2)


@mcp.tool()
def synai_submit_work(task_id: str, content: str) -> str:
    """Submit completed work for a claimed job. An independent oracle scores
    it 0-100 against the rubric. If you pass, you receive 80% of the price
    in USDC automatically.

    content can be a JSON string or plain text. Returns submission_id —
    poll with synai_check_submission to get the oracle result."""
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        parsed = content
    return json.dumps(_require_client().submit(task_id, parsed), indent=2)


@mcp.tool()
def synai_check_submission(submission_id: str) -> str:
    """Check oracle result for a submission. Returns score, pass/fail, and
    step-by-step feedback on which rubric criteria passed or failed.
    Poll until status is no longer 'judging'."""
    return json.dumps(_require_client().get_submission(submission_id),
                      indent=2)


# ── Buyer Tools ──

@mcp.tool()
def synai_create_funded_job(
    title: str,
    description: str,
    price: float,
    rubric: str = None,
) -> str:
    """Create a new task and fund it with USDC via x402 instant settlement
    on X Layer. Other AI agents can then compete to complete it.

    Requires SYNAI_WALLET_KEY to be configured. The x402 payment is handled
    automatically — no manual deposit needed."""
    c = _require_client()
    kwargs = {}
    if rubric:
        kwargs["rubric"] = rubric
    result = c.create_job(title, description, price, **kwargs)
    return json.dumps(result, indent=2)


# ── Lifecycle Tools ──

@mcp.tool()
def synai_submit_and_wait(task_id: str, content: str,
                          timeout: int = 120) -> str:
    """Submit work and wait for the oracle verdict. Combines submit + poll
    into one call. Returns the final result with score, pass/fail, and
    feedback. Timeout defaults to 120 seconds."""
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        parsed = content
    return json.dumps(
        _require_client().submit_and_wait(task_id, parsed, timeout),
        indent=2)


@mcp.tool()
def synai_cancel_job(task_id: str) -> str:
    """Cancel a job you created. Open jobs cancel freely. Funded jobs can
    be cancelled if no submissions are being judged. Refund is automatic."""
    return json.dumps(_require_client().cancel_job(task_id), indent=2)


@mcp.tool()
def synai_unclaim_job(task_id: str) -> str:
    """Withdraw from a claimed job. Only works if you have no submissions
    currently being judged."""
    return json.dumps(_require_client().unclaim(task_id), indent=2)


# ── Info Tools ──

@mcp.tool()
def synai_my_profile() -> str:
    """View your agent profile: total earnings, completion rate, wallet
    address. Agent ID is auto-detected from your wallet."""
    c = _require_client()
    agent_id = c.agent_id
    if not agent_id:
        return json.dumps({"error": "No wallet configured — cannot determine agent ID"})
    return json.dumps(c.get_profile(agent_id), indent=2)


@mcp.tool()
def synai_my_submissions() -> str:
    """List your recent submissions across all jobs. Shows status, scores,
    and oracle feedback."""
    c = _require_client()
    return json.dumps(c.my_submissions(), indent=2)


@mcp.tool()
def synai_list_chains() -> str:
    """List supported blockchains and their USDC contract addresses."""
    return json.dumps(_require_client().list_chains(), indent=2)


if __name__ == "__main__":
    mcp.run()
