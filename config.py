import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class Config:
    # DigitalOcean provides postgres://, but SQLAlchemy 2.0+ requires postgresql://
    _raw_db_url = os.environ.get('DATABASE_URL', '')
    if _raw_db_url.startswith('postgres://'):
        _raw_db_url = _raw_db_url.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = _raw_db_url or f'sqlite:///{os.path.join(_BASE_DIR, "atp_dev.db")}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-me')

    # Chain (Base L2)
    RPC_URL = os.environ.get('RPC_URL', '')
    USDC_CONTRACT = os.environ.get('USDC_CONTRACT', '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
    OPERATIONS_WALLET_ADDRESS = os.environ.get('OPERATIONS_WALLET_ADDRESS', '')
    OPERATIONS_WALLET_KEY = os.environ.get('OPERATIONS_WALLET_KEY', '')
    FEE_WALLET_ADDRESS = os.environ.get('FEE_WALLET_ADDRESS', '')
    MIN_TASK_AMOUNT = float(os.environ.get('MIN_TASK_AMOUNT', '0.1'))

    # Oracle LLM (OpenAI-compatible)
    ORACLE_LLM_BASE_URL = os.environ.get('ORACLE_LLM_BASE_URL', 'https://openrouter.ai/api/v1')
    ORACLE_LLM_API_KEY = os.environ.get('ORACLE_LLM_API_KEY', '')
    ORACLE_LLM_MODEL = os.environ.get('ORACLE_LLM_MODEL', 'openai/gpt-4o')
    ORACLE_PASS_THRESHOLD = int(os.environ.get('ORACLE_PASS_THRESHOLD', '65'))
    ORACLE_MAX_ROUNDS = int(os.environ.get('ORACLE_MAX_ROUNDS', '6'))
    ORACLE_TIMEOUT_SECONDS = int(os.environ.get('ORACLE_TIMEOUT_SECONDS', '120'))

    # Platform fee (basis points: 2000 = 20%)
    PLATFORM_FEE_BPS = int(os.environ.get('PLATFORM_FEE_BPS', '2000'))

    # Operator: Ethereum address authorized for privileged operations (solvency, etc.)
    OPERATOR_ADDRESS = os.environ.get('OPERATOR_ADDRESS', '')
    OPERATOR_SIGNATURE_MAX_AGE = int(os.environ.get('OPERATOR_SIGNATURE_MAX_AGE', '300'))  # seconds

    # Multi-chain
    DEFAULT_CHAIN_ID = int(os.environ.get('DEFAULT_CHAIN_ID', '8453'))

    # X Layer
    XLAYER_RPC_URL = os.environ.get('XLAYER_RPC_URL', 'https://rpc.xlayer.tech')
    XLAYER_USDC_CONTRACT = os.environ.get('XLAYER_USDC_CONTRACT', '')

    # OnchainOS (OKX)
    ONCHAINOS_API_KEY = os.environ.get('ONCHAINOS_API_KEY', '')
    ONCHAINOS_SECRET_KEY = os.environ.get('ONCHAINOS_SECRET_KEY', '')
    ONCHAINOS_PASSPHRASE = os.environ.get('ONCHAINOS_PASSPHRASE', '')
    ONCHAINOS_PROJECT_ID = os.environ.get('ONCHAINOS_PROJECT_ID', '')

    # x402
    X402_ENABLED = os.environ.get('X402_ENABLED', 'true').lower() == 'true'
    X402_COINBASE_FACILITATOR_URL = os.environ.get(
        'X402_COINBASE_FACILITATOR_URL', 'https://x402.org/facilitator')
    X402_OKX_FACILITATOR_URL = os.environ.get(
        'X402_OKX_FACILITATOR_URL', 'https://web3.okx.com/api/v6/x402')

    # Submission marketplace
    SOLUTION_VIEW_FEE_PERCENT = int(os.environ.get('SOLUTION_VIEW_FEE_PERCENT', '70'))

    @classmethod
    def validate_production(cls):
        """Startup check (no-op — guards removed)."""
        pass
