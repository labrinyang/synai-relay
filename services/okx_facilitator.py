"""OKX x402 facilitator adapter — translates OKX API format to x402 SDK types."""
import logging

# Verified import path: x402.facilitator re-exports from x402.schemas.responses
from x402.facilitator import VerifyResponse, SettleResponse

from services.onchainos_client import OnchainOSClient

logger = logging.getLogger('relay.okx_facilitator')


def _network_to_chain_index(network: str) -> str:
    """Extract chain index from CAIP-2 network. 'eip155:196' -> '196'."""
    return network.split(":")[-1]


class OKXFacilitatorClient:
    """Adapts OKX's x402 API to the x402 SDK's FacilitatorClientSync protocol."""

    def __init__(self, api_key: str, secret_key: str, passphrase: str,
                 project_id: str = ""):
        self._client = OnchainOSClient(api_key, secret_key, passphrase, project_id)

    def verify(self, payload, requirements) -> VerifyResponse:
        resp = self._client.post("/api/v6/x402/verify", {
            "x402Version": "1",
            "chainIndex": _network_to_chain_index(requirements.network),
            "paymentPayload": payload.model_dump(),
            "paymentRequirements": {
                "scheme": requirements.scheme,
                "maxAmountRequired": requirements.amount,
                "payTo": requirements.pay_to,
                "asset": requirements.asset,
                "description": "",
            },
        })
        data = resp["data"][0]
        return VerifyResponse(
            is_valid=data.get("isValid", False),
            payer=data.get("payer"),
            invalid_reason=data.get("invalidReason"),
            invalid_message=data.get("invalidMessage"),
        )

    def settle(self, payload, requirements) -> SettleResponse:
        resp = self._client.post("/api/v6/x402/settle", {
            "x402Version": "1",
            "chainIndex": _network_to_chain_index(requirements.network),
            "paymentPayload": payload.model_dump(),
            "paymentRequirements": {
                "scheme": requirements.scheme,
                "maxAmountRequired": requirements.amount,
                "payTo": requirements.pay_to,
                "asset": requirements.asset,
            },
        })
        data = resp["data"][0]
        # OKX settle response uses "errorMsg" not "errorReason"
        return SettleResponse(
            success=data.get("success", False),
            transaction=data.get("txHash", ""),
            network=f"eip155:{data.get('chainIndex', _network_to_chain_index(requirements.network))}",
            payer=data.get("payer"),
            error_reason=data.get("errorMsg"),
            error_message=data.get("errorMsg"),
        )

    def get_supported(self):
        return self._client.get("/api/v6/x402/supported")

    def close(self):
        pass
