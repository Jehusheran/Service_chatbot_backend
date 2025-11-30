-- messages
CREATE TABLE IF NOT EXISTS messages (
  id BIGSERIAL PRIMARY KEY,
  message_id TEXT UNIQUE,
  customer_id TEXT NOT NULL,
  agent_id TEXT NULL,
  sender TEXT NOT NULL,
  message TEXT NOT NULL,
  meta JSONB DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_customer_agent_time ON messages(customer_id, agent_id, created_at DESC);

-- agents
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  name TEXT,
  password_hash TEXT,
  created_at timestamptz DEFAULT now()
);

-- customers
CREATE TABLE IF NOT EXISTS customers (
  customer_id TEXT PRIMARY KEY,
  name TEXT,
  email TEXT,
  phone TEXT,
  created_at timestamptz DEFAULT now()
);

-- bookings
CREATE TABLE IF NOT EXISTS bookings (
  id BIGSERIAL PRIMARY KEY,
  booking_ref TEXT UNIQUE NOT NULL,
  idempotency_key TEXT,
  customer_id TEXT REFERENCES customers(customer_id),
  agent_id TEXT REFERENCES agents(agent_id),
  calendar_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  service_id TEXT NOT NULL,
  start timestamptz NOT NULL,
  "end" timestamptz NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('confirmed','cancelled','rescheduled','pending')),
  paid BOOLEAN DEFAULT FALSE,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bookings_customer ON bookings(customer_id);

-- otps
CREATE TABLE IF NOT EXISTS otps (
  id BIGSERIAL PRIMARY KEY,
  phone TEXT NOT NULL,
  code TEXT NOT NULL,
  valid_until timestamptz NOT NULL,
  used BOOLEAN DEFAULT FALSE,
  created_at timestamptz DEFAULT now()
);

-- summaries
CREATE TABLE IF NOT EXISTS summaries (
  id BIGSERIAL PRIMARY KEY,
  customer_id TEXT,
  agent_id TEXT NULL,
  range_start timestamptz,
  range_end timestamptz,
  sentences JSONB DEFAULT '[]'::jsonb,
  topics JSONB DEFAULT '[]'::jsonb,
  sentiment TEXT,
  message_count INT,
  model_meta JSONB DEFAULT '{}'::jsonb,
  generated_at timestamptz DEFAULT now(),
  cache_key TEXT UNIQUE,
  source_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_summaries_customer_range ON summaries(customer_id, range_start, range_end);

-- conversations
CREATE TABLE IF NOT EXISTS conversations (
  id BIGSERIAL PRIMARY KEY,
  customer_id TEXT NOT NULL,
  agent_id TEXT NULL,
  mode TEXT NOT NULL DEFAULT 'bot',
  bot_assist BOOLEAN DEFAULT FALSE,
  agent_online BOOLEAN DEFAULT FALSE,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  UNIQUE (customer_id, agent_id)
);
