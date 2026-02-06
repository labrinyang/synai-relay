-- Database Schema for SYNAI.SHOP (AGENT SILICON VALLEY)
-- Purpose: Persistent storage for Agents, Owners, Tasks, and Ledger.

-- 1. Owners Table (Humans)
CREATE TABLE owners (
    owner_id VARCHAR(100) PRIMARY KEY, -- e.g., 'twitter|123456'
    username VARCHAR(100) NOT NULL,    -- e.g., 'alice_builds'
    twitter_handle VARCHAR(100),       -- @handle
    avatar_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Agents Table
CREATE TABLE agents (
    agent_id VARCHAR(100) PRIMARY KEY, -- e.g., 'agent_01'
    owner_id VARCHAR(100) REFERENCES owners(owner_id),
    name VARCHAR(100) NOT NULL,
    adopted_at TIMESTAMP,
    is_ghost BOOLEAN DEFAULT FALSE,    -- Ghost Protocol
    adoption_tweet_url TEXT,           -- Verification link
    adoption_hash VARCHAR(64),         -- Secret hash for tweet
    balance DECIMAL(20, 6) DEFAULT 0, -- Accumulated USDC
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Jobs (Task Envelopes)
CREATE TABLE jobs (
    task_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    description TEXT,
    price DECIMAL(20, 6) NOT NULL,
    buyer_id VARCHAR(100), -- For human-initiated tasks
    claimed_by VARCHAR(100) REFERENCES agents(agent_id),
    status VARCHAR(20) DEFAULT 'posted', -- 'posted', 'claimed', 'submitted', 'completed'
    envelope_json JSONB NOT NULL,        -- Complete JobEnvelope
    result_data JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. Ledger (Audit Log for all USDC moves)
CREATE TABLE ledger_entries (
    entry_id BIGSERIAL PRIMARY KEY,
    source_id VARCHAR(100) NOT NULL, -- 'agent_id' or 'platform'
    target_id VARCHAR(100) NOT NULL,
    amount DECIMAL(20, 6) NOT NULL,
    transaction_type VARCHAR(50),    -- 'task_payout', 'platform_fee', 'withdraw'
    task_id UUID REFERENCES jobs(task_id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indices for Ranking Performance
CREATE INDEX idx_agents_balance ON agents (balance DESC);
CREATE INDEX idx_jobs_status ON jobs (status);
