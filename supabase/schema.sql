-- Backend-only bootstrap schema. Apply only to the dedicated bot project.
-- Supabase CLI was unavailable during authoring; this is NOT a fabricated migration.
-- Generate a migration with `supabase migration new bot_audit` before managed rollout.
begin;
create table if not exists public.trades (
  id uuid primary key,
  created_at timestamptz not null default now(),
  payload jsonb not null check (jsonb_typeof(payload) = 'object')
);
create table if not exists public.market_snapshots (
  id uuid primary key,
  created_at timestamptz not null default now(),
  payload jsonb not null check (jsonb_typeof(payload) = 'object')
);
create table if not exists public.model_checkpoints (
  id uuid primary key,
  created_at timestamptz not null default now(),
  payload jsonb not null check (jsonb_typeof(payload) = 'object')
);
create table if not exists public.risk_state (
  id uuid primary key,
  created_at timestamptz not null default now(),
  payload jsonb not null check (jsonb_typeof(payload) = 'object')
);
create table if not exists public.decision_logs (
  id uuid primary key,
  created_at timestamptz not null default now(),
  payload jsonb not null check (jsonb_typeof(payload) = 'object')
);

alter table public.trades enable row level security;
alter table public.market_snapshots enable row level security;
alter table public.model_checkpoints enable row level security;
alter table public.risk_state enable row level security;
alter table public.decision_logs enable row level security;
revoke all on public.trades, public.market_snapshots, public.model_checkpoints,
  public.risk_state, public.decision_logs from public, anon, authenticated;
grant select, insert on public.trades, public.market_snapshots, public.model_checkpoints,
  public.risk_state, public.decision_logs to service_role;
-- Immutable audit tables: replay uses INSERT ON CONFLICT DO NOTHING, never UPDATE.
revoke update, delete, truncate on public.trades, public.market_snapshots,
  public.model_checkpoints, public.risk_state, public.decision_logs from service_role;
create index if not exists trades_created_idx on public.trades (created_at desc);
create index if not exists snapshots_created_idx on public.market_snapshots (created_at desc);
create index if not exists checkpoints_created_idx on public.model_checkpoints (created_at desc);
create index if not exists risk_created_idx on public.risk_state (created_at desc);
create index if not exists decisions_created_idx on public.decision_logs (created_at desc);
create index if not exists decisions_version_idx on public.decision_logs ((payload->>'model_version'), created_at desc);
commit;
