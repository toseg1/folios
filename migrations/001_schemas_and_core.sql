CREATE SCHEMA core;
CREATE SCHEMA marts;
CREATE SCHEMA staging;

CREATE TABLE core.schema_migrations (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- One table replaces the eleven reference tables of the parent project.
-- Codes are identical to 04.3 so Folios doubles as a taxonomy proving ground.
CREATE TABLE core.dimensions (
    dimension   text NOT NULL,     -- 'asset_class','region','fiscal_envelope',…
    code        text NOT NULL,
    label_fr    text NOT NULL,
    label_en    text NOT NULL,
    sort_order  int  NOT NULL DEFAULT 0,
    PRIMARY KEY (dimension, code)
);

CREATE TABLE core.accounts (
    account_id       text PRIMARY KEY,
    broker           text NOT NULL,
    institution      text,
    account_type     text NOT NULL,
    fiscal_envelope  text,          -- PEA, PEA_PME, CTO, AV, PER, LIVRET_A,
                                    -- LDDS, LEP, PEL, CAT, CURRENT
    -- CUSTODY LAYER 1: who holds YOUR units, and how.
    custody_type      text,         -- BROKER, BANK, INSURER, EXCHANGE,
                                    -- SELF_CUSTODY, DIRECT_REGISTRAR
    holding_structure text NOT NULL DEFAULT 'UNKNOWN',
                                    -- DIRECT_REGISTERED (nominatif pur),
                                    -- NOMINEE_SEGREGATED, OMNIBUS, UNKNOWN.
                                    -- This is what decides what actually happens
                                    -- if the intermediary fails.
    -- CUSTODY LAYER 2b: how your crypto keys are stored. NULL unless
    -- custody_type = 'SELF_CUSTODY' — you cannot answer this about an exchange.
    key_storage       text,         -- COLD_HARDWARE, COLD_PAPER, MULTISIG,
                                    -- MPC, HOT_SOFTWARE
    contractual_rate  numeric(9,6), -- PEL, CAT. NEVER for administered rates
                                    -- (Livret A, LDDS) — those change by decree
                                    -- and are not a property of the account
    maturity_date     date,         -- protected-and-locked vs protected-and-available
    deposit_ceiling   numeric(20,4),-- Livret A 22950, LDDS 12000 → headroom card
    guarantee_scheme  text,         -- FGDR, FGAP, NONE
    base_currency     char(3) NOT NULL,
    opened_on         date,
    closed_on         date,
    is_active         boolean NOT NULL DEFAULT true,

    CONSTRAINT key_storage_only_when_self_custody CHECK (
        key_storage IS NULL OR custody_type = 'SELF_CUSTODY')
);

CREATE TABLE core.instruments (
    instrument_id     text PRIMARY KEY,
    isin              text UNIQUE,
    yf_symbol         text,
    name              text NOT NULL,
    asset_class       text NOT NULL,   -- validated against core.dimensions by the
                                       -- seeder, NOT by a CHECK: a dimension must
                                       -- be extensible without a migration
                                       -- EQUITY | ETP | FUND | BOND | CRYPTO
    instrument_type   text,         -- ETF | ETC | ETN | OPCVM | SICAV | SCPI
                                    -- | SHARE | BOND | TOKEN
                                    -- ETP is the umbrella; the wrapper is the
                                    -- instrument_type. See legal_structure below —
                                    -- an ETN is NOT a fund and owns nothing.
    currency          char(3) NOT NULL,
    region            text,
    sector            text,
    issuer            text,
    domicile_country  char(2),
    -- no is_etf boolean: it conflated ETF with ETP and could not express ETC/ETN.
    -- Derive from instrument_type instead.
    protection_type   text,         -- GUARANTEED | MARKET_EXPOSED |
                                    -- PARTIALLY_PROTECTED | UNKNOWN
    is_pea_eligible   boolean,      -- NULL means unknown, displayed as unknown
    price_source      text NOT NULL DEFAULT 'yfinance'
        CHECK (price_source IN ('yfinance','manual')),
    is_active         boolean NOT NULL DEFAULT true
);
-- NOTE: 'CASH' is deliberately NOT an asset class. Cash is derived from
-- SUM(net_amount). Two representations of cash would double-count in
-- v_allocation.
--
-- DIMENSIONS ARE DATA, TRANSACTION TYPES ARE CODE. asset_class, region,
-- sector, instrument_type, protection_type, fiscal_envelope, custody_type
-- and account_type carry no CHECK: they are extended by adding a row to
-- config/dimensions.csv. txn_type keeps its CHECK because a new type
-- changes how numbers are computed, not merely how they are grouped.

-- SUBTYPE TABLES (ADR-030). Type-specific attributes do not belong on the base
-- table: coupon frequency means nothing for a share, replication method means
-- nothing for a bond. One wide table would be ~60% null and could carry no
-- constraints. All values here are TYPED BY HAND in config — never fetched.
-- Equities need no subtype: the base table already covers them.

CREATE TABLE core.instrument_fund (
    instrument_id       text PRIMARY KEY REFERENCES core.instruments,
    -- THE FIELD THAT DECIDES EVERYTHING DOWNSTREAM: whether you own a share of
    -- ring-fenced assets, or merely hold a claim on an issuer.
    legal_structure     text NOT NULL,
        -- UCITS_FUND        — ETF/OPCVM/SICAV. Ring-fenced assets, a depositary,
        --                     real holdings. Look-through applies.
        -- NON_UCITS_FUND    — a fund, but outside UCITS protection.
        -- COLLATERALISED_NOTE — most ETCs. A debt security backed by collateral
        --                     (physical metal, or a swap). Not a fund.
        -- UNSECURED_NOTE    — ETNs. UNSECURED DEBT OF THE ISSUER. No ring-fenced
        --                     assets, no depositary, no holdings. If the issuer
        --                     fails you are a creditor, not an owner.
        -- SCPI              — French property vehicle.
    is_ucits            boolean,
    rhp_years           int,
    distribution_policy text,          -- ACCUMULATING | DISTRIBUTING
    ongoing_charges     numeric(9,6),  -- DECIMAL FRACTION: 0.20% is 0.0020
    sri                 int CHECK (sri BETWEEN 1 AND 7),
                                       -- PRIIPs KID figure, stored AS PUBLISHED.
                                       -- Ordinal scale: never averaged.
    replication_method  text,          -- PHYSICAL_FULL | PHYSICAL_SAMPLED | SYNTHETIC
    swap_counterparty   text,          -- synthetic only
    uses_sec_lending    boolean,
    depositary          text,          -- CUSTODY LAYER 2a: who holds the FUND's assets
    sfdr_article        int CHECK (sfdr_article IN (6,8,9)),
    benchmark_index     text,          -- overlap detection without look-through
    justetf_id          text,
    -- SCPI only
    subscription_price  numeric(20,4),
    withdrawal_price    numeric(20,4), -- value the position at THIS one
    management_company  text,
    property_sector     text,
    occupancy_rate      numeric(9,6),
    distribution_rate   numeric(9,6),

    -- An unsecured note has no fund assets, so it can have no depositary.
    -- The CHECK exists to teach the distinction, not merely to enforce it.
    CONSTRAINT note_has_no_depositary CHECK (
        legal_structure <> 'UNSECURED_NOTE' OR depositary IS NULL)
);

-- ETP look-through. AGGREGATE grain, not one row per constituent: justETF
-- publishes full sector and country breakdowns but only top-10 holdings, and
-- the aggregate view is what fixes "a world ETF's region is Global", which is
-- a shrug rather than an allocation.
--
-- Roughly 30 rows per ETP instead of 1,500. Snapshots are APPENDED, never
-- updated: a 2024 allocation computed from 2026 constituents is wrong, and
-- wrong invisibly.
CREATE TABLE core.etp_exposure (
    instrument_id  text NOT NULL REFERENCES core.instruments,
    as_of_date     date NOT NULL,
    dimension      text NOT NULL,   -- country | sector | region | holding
    code           text NOT NULL,   -- mapped to core.dimensions where possible
    label          text NOT NULL,   -- as published; kept even when unmapped,
                                    -- so an unmapped row is never lost
    weight         numeric(9,6) NOT NULL
                   CHECK (weight >= 0 AND weight <= 1),   -- DECIMAL FRACTION
    source         text NOT NULL,   -- justetf | issuer_file | manual
    PRIMARY KEY (instrument_id, as_of_date, dimension, code)
);
CREATE INDEX ON core.etp_exposure (instrument_id, as_of_date);

CREATE TABLE core.instrument_bond (
    instrument_id     text PRIMARY KEY REFERENCES core.instruments,
    coupon_rate       numeric(9,6),    -- decimal fraction
    coupon_frequency  text,            -- ANNUAL | SEMI_ANNUAL | QUARTERLY | ZERO
    maturity_date     date,
    face_value        numeric(20,4),
    issuer_type       text,            -- SOVEREIGN | CORPORATE | FINANCIAL | SUPRANATIONAL
    credit_rating     text,
    seniority         text,            -- SENIOR_SECURED | SENIOR_UNSECURED | SUBORDINATED
    is_callable       boolean NOT NULL DEFAULT false
);

CREATE TABLE core.instrument_crypto (
    instrument_id     text PRIMARY KEY REFERENCES core.instruments,
    chain             text NOT NULL,   -- USDC on Ethereum and USDC on Solana are
    contract_address  text,            -- TWO instruments, not one: different
    token_standard    text,            -- contract, bridge exposure, failure mode
    consensus         text,            -- POW | POS
    is_stablecoin     boolean NOT NULL DEFAULT false,
    peg_currency      char(3)
);

CREATE TABLE core.instrument_aliases (
    alias          text NOT NULL,
    source         text NOT NULL DEFAULT 'manual',
    instrument_id  text NOT NULL REFERENCES core.instruments,
    PRIMARY KEY (alias, source)
);

CREATE TABLE core.transactions (
    entry_id        text PRIMARY KEY,   -- STABLE key, see §4
    txn_hash        text NOT NULL,      -- content hash, for change detection only
    account_id      text NOT NULL REFERENCES core.accounts,
    trade_date      date NOT NULL,
    settle_date     date,
    txn_type        text NOT NULL CHECK (txn_type IN (
                        'BUY','SELL','DIVIDEND','INTEREST','STAKING',
                        'FEE','TAX','DEPOSIT','WITHDRAWAL',
                        'TRANSFER_IN','TRANSFER_OUT','SPLIT','OPENING_BALANCE')),
    instrument_id   text REFERENCES core.instruments,
    quantity        numeric(38,18),
    price           numeric(20,8),
    gross_amount    numeric(20,4),
    fee             numeric(20,4) NOT NULL DEFAULT 0,
    tax             numeric(20,4) NOT NULL DEFAULT 0,
    net_amount      numeric(20,4) NOT NULL,
    currency        char(3) NOT NULL,
    fx_rate_to_base numeric(18,10) NOT NULL DEFAULT 1,
    fx_rate_date    date,
    fx_rate_source  text NOT NULL DEFAULT 'ecb',
    is_estimated    boolean NOT NULL DEFAULT false,
    note            text,
    source          text NOT NULL DEFAULT 'manual',
    source_file     text,
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT trade_needs_instrument CHECK (
        txn_type NOT IN ('BUY','SELL') OR
        (instrument_id IS NOT NULL AND quantity IS NOT NULL AND price IS NOT NULL)),

    -- The sign convention is the load-bearing assumption behind every view.
    -- Enforce it here so one hand-written INSERT cannot break it silently.
    CONSTRAINT sign_convention CHECK (
        CASE txn_type
          WHEN 'BUY'             THEN quantity > 0 AND net_amount < 0
          WHEN 'SELL'            THEN quantity < 0 AND net_amount > 0
          WHEN 'DIVIDEND'        THEN net_amount > 0
          WHEN 'INTEREST'        THEN net_amount > 0
          WHEN 'STAKING'         THEN quantity > 0 AND net_amount = 0
          WHEN 'FEE'             THEN net_amount < 0
          WHEN 'TAX'             THEN net_amount < 0
          WHEN 'DEPOSIT'         THEN net_amount > 0
          WHEN 'WITHDRAWAL'      THEN net_amount < 0
          WHEN 'TRANSFER_IN'     THEN quantity > 0 AND net_amount = 0
          WHEN 'TRANSFER_OUT'    THEN quantity < 0 AND net_amount = 0
          WHEN 'SPLIT'           THEN quantity <> 0 AND net_amount = 0
          WHEN 'OPENING_BALANCE' THEN quantity > 0
          ELSE true
        END),

    -- A standalone FEE/TAX row carries its amount in net_amount only.
    -- Without this, v_costs counts the same fee twice.
    CONSTRAINT standalone_cost_rows_have_no_columns CHECK (
        txn_type NOT IN ('FEE','TAX') OR (fee = 0 AND tax = 0))
);

CREATE INDEX ON core.transactions (account_id, trade_date);
CREATE INDEX ON core.transactions (instrument_id, trade_date);
CREATE INDEX ON core.transactions (account_id, txn_type, trade_date);

CREATE TABLE core.prices (
    instrument_id  text NOT NULL REFERENCES core.instruments,
    price_date     date NOT NULL,
    close_price    numeric(20,8) NOT NULL,   -- for valuation
    adj_close      numeric(20,8),            -- for return series
    currency       char(3) NOT NULL,
    source         text NOT NULL DEFAULT 'yfinance',
    PRIMARY KEY (instrument_id, price_date)
);

CREATE TABLE core.fx_rates (
    rate_date  date NOT NULL,
    base_ccy   char(3) NOT NULL,
    quote_ccy  char(3) NOT NULL,
    rate       numeric(20,10) NOT NULL CHECK (rate > 0),
    source     text NOT NULL DEFAULT 'ecb',
    PRIMARY KEY (rate_date, base_ccy, quote_ccy)
);

CREATE TABLE core.statement_positions (
    account_id     text NOT NULL REFERENCES core.accounts,
    as_of          date NOT NULL,
    instrument_id  text NOT NULL REFERENCES core.instruments,
    quantity       numeric(38,18) NOT NULL,
    market_value   numeric(20,4),
    currency       char(3),
    PRIMARY KEY (account_id, as_of, instrument_id)
);

-- Cash is where manual-entry errors actually land: a mistyped fee,
-- a dividend never entered.
CREATE TABLE core.statement_cash (
    account_id  text NOT NULL REFERENCES core.accounts,
    as_of       date NOT NULL,
    currency    char(3) NOT NULL,
    balance     numeric(20,4) NOT NULL,
    PRIMARY KEY (account_id, as_of, currency)
);

CREATE TABLE core.load_runs (
    run_id         bigserial PRIMARY KEY,
    source         text NOT NULL,
    source_file    text,
    file_sha256    text,
    rows_read      int NOT NULL DEFAULT 0,
    rows_inserted  int NOT NULL DEFAULT 0,
    rows_updated   int NOT NULL DEFAULT 0,
    rows_skipped   int NOT NULL DEFAULT 0,
    status         text NOT NULL,
    message        text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz
);
