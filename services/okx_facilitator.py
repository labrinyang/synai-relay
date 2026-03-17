"""OKX x402 facilitator adapter — translates OKX API format to x402 SDK types.

OKX uses non-standard field names compared to x402 spec:
  - "maxAmountRequired" instead of "amount"
  - "chainIndex" instead of "network" (CAIP-2)
  - "description" (extra field in verify, not in x402 spec)
  - "payTo" / "asset" / "scheme" match x402 standard

These mappings were verified against OKX API v6 as of 2026-03.
If OKX updates their API toward the x402 standard, update the dicts below.
"""
import logging

# Verified import path: x402.facilitator re-exports from x402.schemas.responses
from x402.facilitator import VerifyResponse, SettleResponse
from x402.schemas import SupportedResponse, SupportedKind

from services.onchainos_client import OnchainOSClient
from services.x402_service import parse_chain_id

logger = logging.getLogger('relay.okx_facilitator')


class OKXFacilitatorClient:
    """Adapts OKX's x402 API to the x402 SDK's FacilitatorClientSync protocol."""

    def __init__(self, *, onchainos_client: OnchainOSClient):
        self._client = onchainos_client

    @staticmethod
    def _okx_payload(payload) -> dict:
        """Transform x402 SDK PaymentPayload into OKX's expected format.

        OKX expects: {x402Version, scheme, payload: {signature, authorization}}
        SDK produces: {x402Version, payload, accepted, resource, extensions}

        Note: resource and extensions are stripped — OKX does not use them.
        """
        dumped = payload.model_dump(by_alias=True)
        return {
            "x402Version": dumped.get("x402Version", 2),
            "scheme": payload.accepted.scheme,
            "payload": dumped["payload"],
        }

    def _build_request_body(self, payload, requirements, **extra_req_fields) -> dict:
        """Build OKX x402 API request body (shared by verify and settle)."""
        reqs = {
            "scheme": requirements.scheme,
            "maxAmountRequired": requirements.amount,  # OKX field name (std: amount)
            "payTo": requirements.pay_to,
            "asset": requirements.asset,
            **extra_req_fields,
        }
        return {
            "x402Version": 1,  # OKX API version (not x402 protocol version)
            "chainIndex": str(parse_chain_id(requirements.network)),
            "paymentPayload": self._okx_payload(payload),
            "paymentRequirements": reqs,
        }

    def verify(self, payload, requirements) -> VerifyResponse:
        req_body = self._build_request_body(payload, requirements,
                                            description="")
        resp = self._client.post("/api/v6/x402/verify", req_body)
        logger.debug("OKX verify response: %s", resp)
        data = resp["data"][0]
        return VerifyResponse(
            is_valid=data.get("isValid", False),
            payer=data.get("payer"),
            invalid_reason=data.get("invalidReason"),
            invalid_message=data.get("invalidReason"),  # OKX docs only define invalidReason
        )

    def settle(self, payload, requirements) -> SettleResponse:
        req_body = self._build_request_body(payload, requirements)
        resp = self._client.post("/api/v6/x402/settle", req_body)
        logger.debug("OKX settle response: %s", resp)
        data = resp["data"][0]
        # txHash can be None if settlement failed — default to empty string.
        # Official docs: error field is "errorReason" (string).
        # Also check legacy field names for backward compatibility.
        return SettleResponse(
            success=data.get("success", False),
            transaction=data.get("txHash") or "",
            network=f"eip155:{data.get('chainIndex', str(parse_chain_id(requirements.network)))}",
            payer=data.get("payer"),
            error_reason=data.get("errorReason") or data.get("failReason") or "",
            error_message=data.get("errorReason") or "",
        )

    def get_supported(self) -> SupportedResponse:
        resp = self._client.get("/api/v6/x402/supported")
        logger.debug("OKX get_supported response: %s", resp)
        # OKX returns each supported kind as a flat item in data[],
        # e.g. [{"x402Version":"1","chainIndex":"196","scheme":"exact"}]
        kinds = []
        for item in resp.get("data", []):
            try:
                kinds.append(SupportedKind(
                    x402_version=int(item.get("x402Version", 1)),
                    scheme=item["scheme"],
                    network=f"eip155:{item['chainIndex']}",
                    extra=item.get("extra"),
                ))
            except (KeyError, ValueError) as e:
                logger.warning("OKX get_supported: skipping malformed item %s: %s", item, e)
        return SupportedResponse(kinds=kinds)

    def close(self):
        pass
