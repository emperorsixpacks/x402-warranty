"""Configuration loaded from environment."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", env_file_encoding="utf-8")

    x402_network: str = "eip155:84532"
    facilitator_url: str = "https://x402.org/facilitator"
    rpc_url: str = "https://sepolia.base.org"

    treasury_private_key: str = ""
    upstream_float_private_key: str = ""
    treasury_address: str = ""
    upstream_float_address: str = ""

    # Tier max upstream coverage (atomic USDC)
    tier1_max_atomic: int = 50000
    tier2_max_atomic: int = 250000
    tier3_max_atomic: int = 1000000

    # Micro-insurance pricing
    premium_floor_bps: int = 500  # 5% minimum premium
    service_fee_bps: int = 50  # 0.5% service fee
    prior_refund_rate: float = 0.15  # Bayesian prior for new hosts
    prior_weight: int = 20  # calls treated as prior observations
    min_calls_for_warranty: int = 20  # inspect-only until this many calls
    max_refund_rate_for_warranty: float = 0.25  # disable warranty above this rate
    tier1_host_max_calls: int = 100  # tier1 only for hosts under this many calls

    # Exposure limits
    per_host_daily_payout_cap_atomic: int = 5_000_000  # $5 per host per day
    per_payer_hourly_refund_cap_atomic: int = 2_000_000  # $2 per payer per hour
    treasury_floor_atomic: int = 2_000_000  # stop warranty if below

    # Optional: Anthropic API key for LLM judge (cut first)
    anthropic_api_key: str = ""

    host: str = "0.0.0.0"
    port: int = 8000
    database_url: str = "sqlite:///./warranty.db"
    allow_local_upstream: bool = False
    public_url: str = ""

    @property
    def usdc_contract(self) -> str:
        if self.x402_network == "eip155:84532":
            return "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
        return "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    @property
    def chain_id(self) -> int:
        if self.x402_network == "eip155:84532":
            return 84532
        return 8453


settings = Settings()
