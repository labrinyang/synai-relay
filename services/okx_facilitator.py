"""OKX x402 facilitator adapter — translates OKX API format to x402 SDK types."""
import logging

# Verified import path: x402.facilitator re-exports from x402.schemas.responses
from x402.facilitator import VerifyResponse, SettleResponse
from x402.schemas import SupportedResponse, SupportedKind

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

    @staticmethod
    def _okx_payload(payload) -> dict:
        """Transform x402 SDK PaymentPayload into OKX's expected format.

        OKX expects: {x402Version, scheme, payload: {signature, authorization}}
        SDK produces: {x402Version, payload, accepted, resource, extensions}
        """
        dumped = payload.model_dump(by_alias=True)
        return {
            "x402Version": dumped.get("x402Version", 1),
            "scheme": payload.accepted.scheme,
            "payload": dumped["payload"],
        }

    def verify(self, payload, requirements) -> VerifyResponse:
        resp = self._client.post("/api/v6/x402/verify", {
            "x402Version": 1,
            "chainIndex": _network_to_chain_index(requirements.network),
            "paymentPayload": self._okx_payload(payload),
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
            "x402Version": 1,
            "chainIndex": _network_to_chain_index(requirements.network),
            "paymentPayload": self._okx_payload(payload),
            "paymentRequirements": {
                "scheme": requirements.scheme,
                "maxAmountRequired": requirements.amount,
                "payTo": requirements.pay_to,
                "asset": requirements.asset,
            },
        })
        data = resp["data"][0]
        # OKX settle response uses "errorCode"/"failReason" for machine-readable
        # codes and "errorMsg" for human-readable text.
        # txHash can be None if settlement failed — default to empty string.
        return SettleResponse(
            success=data.get("success", False),
            transaction=data.get("txHash") or "",
            network=f"eip155:{data.get('chainIndex', _network_to_chain_index(requirements.network))}",
            payer=data.get("payer"),
            error_reason=data.get("errorCode") or data.get("failReason") or "",
            error_message=data.get("errorMsg") or "",
        )

    def get_supported(self) -> SupportedResponse:
        resp = self._client.get("/api/v6/x402/supported")
        # OKX returns each supported kind as a flat item in data[],
        # e.g. [{"x402Version":"1","chainIndex":"196","scheme":"exact"}]
        kinds = [
            SupportedKind(
                x402_version=int(item.get("x402Version", 1)),
                scheme=item["scheme"],
                network=f"eip155:{item['chainIndex']}",
                extra=item.get("extra"),
            )
            for item in resp.get("data", [])
        ]
        return SupportedResponse(kinds=kinds)

    def close(self):
        pass
