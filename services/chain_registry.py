"""Registry of chain adapters, keyed by chain_id."""
from services.chain_adapter import ChainAdapter


class ChainRegistry:

    def __init__(self, default_chain_id: int = 8453):
        self._adapters: dict[int, ChainAdapter] = {}
        self._default_chain_id: int = default_chain_id

    def register(self, adapter: ChainAdapter):
        self._adapters[adapter.chain_id()] = adapter

    def get(self, chain_id: int) -> ChainAdapter:
        adapter = self._adapters.get(chain_id)
        if not adapter:
            raise ValueError(f"Unsupported chain: {chain_id}")
        return adapter

    def get_or_default(self, chain_id: int | None) -> ChainAdapter:
        """Get adapter by chain_id, falling back to default if None."""
        if chain_id is None:
            return self.default()
        return self.get(chain_id)

    def default(self) -> ChainAdapter:
        if self._default_chain_id not in self._adapters:
            raise RuntimeError(
                f"Default chain {self._default_chain_id} not registered. "
                f"Available: {list(self._adapters.keys())}")
        return self._adapters[self._default_chain_id]

    def adapters(self) -> list[ChainAdapter]:
        return list(self._adapters.values())

    def supported_chains(self) -> list[dict]:
        return [{"chain_id": a.chain_id(), "name": a.chain_name(),
                 "caip2": a.caip2(), "usdc": a.usdc_address()}
                for a in self._adapters.values()]
