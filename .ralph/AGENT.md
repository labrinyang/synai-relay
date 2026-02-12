# Ralph Agent Configuration

## Tech Stack
- **Language**: Python 3.12.8
- **Framework**: Flask 3.0.2
- **ORM**: Flask-SQLAlchemy 3.1.1 / SQLAlchemy 2.0+
- **Database**: SQLite (dev), configurable via DATABASE_URL
- **Web3**: web3.py 6+, eth-account 0.11
- **WSGI**: gunicorn 21.2.0
- **Test Framework**: pytest
- **Package Manager**: pip (requirements.txt)
- **Deployment**: Heroku-style (Procfile + runtime.txt)

## Build Instructions

```bash
# Install dependencies (use a virtualenv)
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Test Instructions

```bash
# Run all tests
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_oracle_guard.py -v

# Run with coverage (install pytest-cov first)
python -m pytest tests/ --cov=. --cov-report=term-missing
```

## Run Instructions

```bash
# Development server (port 5005)
python server.py

# Production (gunicorn, PORT from env)
gunicorn --bind 0.0.0.0:$PORT server:app

# With DEV_MODE (skip on-chain verification)
DEV_MODE=true python server.py
```

## Key Environment Variables
| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | DB connection string | `sqlite:///atp_dev.db` |
| `DEV_MODE` | Skip chain verification | `false` |
| `RPC_URL` | Base L2 RPC endpoint | (empty) |
| `USDC_CONTRACT` | USDC token address | Base mainnet USDC |
| `OPERATIONS_WALLET_ADDRESS` | Ops wallet for deposits | (empty) |
| `OPERATIONS_WALLET_KEY` | Ops wallet private key | (empty) |
| `FEE_WALLET_ADDRESS` | Fee collection wallet | (empty) |
| `ORACLE_LLM_API_KEY` | OpenRouter/LLM API key | (empty) |
| `ORACLE_LLM_MODEL` | Oracle evaluation model | `openai/gpt-4o` |

## Project Structure
```
synai-relay/
├── server.py              # Flask app + all route handlers
├── config.py              # Configuration (env vars)
├── models.py              # SQLAlchemy models (Owner, Agent, Job, Submission)
├── requirements.txt       # Python dependencies
├── Procfile               # Heroku-style process definition
├── runtime.txt            # Python version (3.12.8)
├── services/
│   ├── agent_service.py   # Agent registration + reputation
│   ├── job_service.py     # Job CRUD + listing
│   ├── oracle_guard.py    # Submission content safety check
│   ├── oracle_prompts.py  # LLM prompt templates
│   ├── oracle_service.py  # 6-step oracle evaluation pipeline
│   └── wallet_service.py  # On-chain USDC deposit/payout/refund
├── tests/
│   ├── conftest.py
│   ├── test_oracle_guard.py
│   ├── test_oracle_service.py
│   └── test_wallet_service.py
├── templates/             # HTML UI assets
├── scripts/demo/          # E2E demo scripts
└── docs/                  # Design docs (gitignored)
```

## Notes
- 9 tests currently passing (oracle guard, oracle service, wallet service)
- No E2E/integration tests in pytest yet — demo scripts in scripts/demo/ are standalone
- Oracle runs in background threads (not Celery/RQ)
- DB tables auto-created on startup via db.create_all()
