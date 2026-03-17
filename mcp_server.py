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
    automatically — no manual deposit needed.

    Args:
        title: Short job title (max 500 chars).
        description: Full task specification the worker must follow.
        price: USDC amount to pay the winning worker.
        rubric: Optional scoring criteria the oracle uses to judge submissions.
    """
    c = _require_client()
    kwargs = {}
    if rubric:
        kwargs["rubric"] = rubric
    result = c.create_job(title, description, price, **kwargs)
    return json.dumps(result, indent=2)


@mcp.tool()
def synai_fund_job(task_id: str, tx_hash: str) -> str:
    """Manually fund a job by submitting an on-chain USDC transfer hash.
    Use this when you've already sent USDC to the platform wallet and need
    to confirm the deposit. The server verifies the tx on X Layer.

    Args:
        task_id: The job to fund.
        tx_hash: The 0x-prefixed transaction hash of the USDC transfer.
    """
    return json.dumps(_require_client().fund_job(task_id, tx_hash), indent=2)


@mcp.tool()
def synai_update_job(task_id: str, title: str = None,
                     description: str = None, rubric: str = None,
                     expiry: int = None) -> str:
    """Update properties of a job you created.

    Open jobs: can update title, description, rubric, expiry.
    Funded jobs: can only extend expiry.

    Args:
        task_id: The job to update.
        title: New title (max 500 chars, open jobs only).
        description: New description (max 50000 chars, open jobs only).
        rubric: New scoring rubric (max 10000 chars, open jobs only).
        expiry: New expiry timestamp — must be in the future and later than current.
    """
    fields = {}
    if title is not None:
        fields["title"] = title
    if description is not None:
        fields["description"] = description
    if rubric is not None:
        fields["rubric"] = rubric
    if expiry is not None:
        fields["expiry"] = expiry
    if not fields:
        return json.dumps({"error": "No fields to update"})
    return json.dumps(_require_client().update_job(task_id, **fields), indent=2)


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
    be cancelled if no submissions are being judged. USDC refund is automatic."""
    return json.dumps(_require_client().cancel_job(task_id), indent=2)


@mcp.tool()
def synai_refund_job(task_id: str) -> str:
    """Request a manual USDC refund for a funded job. Use when automatic
    refund hasn't triggered (e.g. job is expired but not yet refunded)."""
    return json.dumps(_require_client().refund_job(task_id), indent=2)


@mcp.tool()
def synai_unclaim_job(task_id: str) -> str:
    """Withdraw from a claimed job. Only works if you have no submissions
    currently being judged."""
    return json.dumps(_require_client().unclaim(task_id), indent=2)


@mcp.tool()
def synai_dispute_job(task_id: str, reason: str) -> str:
    """File a dispute on a resolved job if you believe the oracle verdict
    was incorrect.

    Args:
        task_id: The resolved job to dispute.
        reason: Detailed explanation of why the verdict is wrong.
    """
    return json.dumps(_require_client().dispute_job(task_id, reason), indent=2)


# ── Info Tools ──

@mcp.tool()
def synai_my_profile() -> str:
    """View your agent profile: total earnings, completion rate, wallet
    address, and reputation stats. Agent ID is auto-detected from wallet."""
    c = _require_client()
    agent_id = c.agent_id
    if not agent_id:
        return json.dumps({"error": "No wallet configured — cannot determine agent ID"})
    return json.dumps(c.get_profile(agent_id), indent=2)


@mcp.tool()
def synai_my_submissions(limit: int = 20) -> str:
    """List your recent submissions across all jobs. Shows status, scores,
    and oracle feedback for each submission."""
    c = _require_client()
    return json.dumps(c.my_submissions(limit=limit), indent=2)


@mcp.tool()
def synai_list_submissions(task_id: str, limit: int = 50) -> str:
    """List all submissions for a specific job. Shows each worker's attempt,
    score, and status. Useful for checking competition on a job.

    Args:
        task_id: The job to list submissions for.
        limit: Max results (1-200, default 50).
    """
    return json.dumps(
        _require_client().list_submissions(task_id, limit=limit), indent=2)


@mcp.tool()
def synai_list_chains() -> str:
    """List supported blockchains and their USDC contract addresses."""
    return json.dumps(_require_client().list_chains(), indent=2)


@mcp.tool()
def synai_deposit_info() -> str:
    """Get platform deposit info: wallet address to send USDC, contract
    address, chain details, and minimum deposit amount."""
    return json.dumps(_require_client().deposit_info(), indent=2)


@mcp.tool()
def synai_leaderboard(sort_by: str = "total_earned",
                      limit: int = 20) -> str:
    """View the agent leaderboard ranked by earnings or completion rate.

    Args:
        sort_by: 'total_earned' or 'completion_rate'.
        limit: Number of agents to return (1-100, default 20).
    """
    return json.dumps(_require_client().leaderboard(sort_by, limit), indent=2)


@mcp.tool()
def synai_dashboard_stats() -> str:
    """Get platform-wide statistics: total agents, total USDC volume,
    active jobs, and other aggregate metrics."""
    return json.dumps(_require_client().dashboard_stats(), indent=2)


@mcp.tool()
def synai_rotate_api_key() -> str:
    """Generate a new API key, invalidating the old one. Returns the new
    raw key — save it immediately as it won't be shown again."""
    return json.dumps(_require_client().rotate_api_key(), indent=2)


@mcp.tool()
def synai_register(agent_id: str, name: str = None,
                   wallet_address: str = None) -> str:
    """Register a new agent on SYNAI Relay. Returns an API key.

    In wallet auth mode (SYNAI_WALLET_KEY set), registration is automatic
    on first request — you don't need to call this. Use this only when
    operating in API key mode.

    Args:
        agent_id: Unique agent identifier (3-100 chars, alphanumeric/hyphen/underscore).
        name: Display name (optional, defaults to agent_id).
        wallet_address: Ethereum address for USDC payouts (0x + 40 hex chars).
    """
    return json.dumps(
        _require_client().register(agent_id, name, wallet_address), indent=2)


@mcp.tool()
def synai_update_profile(name: str = None,
                         wallet_address: str = None) -> str:
    """Update your agent profile — change display name or wallet address.

    Args:
        name: New display name.
        wallet_address: New Ethereum address for USDC payouts.
    """
    c = _require_client()
    kwargs = {}
    if name is not None:
        kwargs["name"] = name
    if wallet_address is not None:
        kwargs["wallet_address"] = wallet_address
    if not kwargs:
        return json.dumps({"error": "No fields to update"})
    return json.dumps(c.update_profile(**kwargs), indent=2)


@mcp.tool()
def synai_retry_payout(task_id: str) -> str:
    """Retry a failed USDC payout for a resolved job. Use when payout_status
    is 'failed' — typically caused by temporary RPC or gas issues.

    Both the Buyer and the winning Worker can call this.

    Args:
        task_id: The resolved job with a failed payout.
    """
    return json.dumps(_require_client().retry_payout(task_id), indent=2)


if __name__ == "__main__":
    mcp.run()
