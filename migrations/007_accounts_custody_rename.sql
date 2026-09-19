-- Custody/instrument redesign from the data-model-review feedback, done as
-- one migration rather than several: the column changes below (accounts,
-- instrument_fund, instruments) touch a chain of dependent views —
-- v_transactions -> v_ledger_walk -> v_realised_pnl/v_income_yield ->
-- v_pnl_summary, plus v_custody_exposure and v_allocation — and Postgres
-- refuses a plain ALTER TABLE RENAME/DROP COLUMN when a view depends on
-- that column ("cannot change name of view column" / "other objects
-- depend on it"). Splitting this across several migrations would mean
-- rebuilding the same view chain two or three times for no reason; instead
-- every dependent view is dropped together in one DROP VIEW statement
-- (Postgres resolves cross-dependencies within a single multi-object DROP
-- without needing CASCADE) and recreated once, at the end, in dependency
-- order.
--
-- v_positions, v_fee_drag, v_risk_profile, v_wrapper_risk,
-- v_lookthrough_coverage and v_income are untouched: none of them select
-- institution/depositary/region.
DROP VIEW marts.v_pnl_summary;
DROP VIEW marts.v_income_yield;
DROP VIEW marts.v_realised_pnl;
DROP VIEW marts.v_ledger_walk;
DROP VIEW marts.v_transactions;
DROP VIEW marts.v_custody_exposure;
DROP VIEW marts.v_allocation;

-- Accounts: "institution" is renamed to "ultimate_parent" — the legal
-- entity actually bearing the risk, the sense already used in the user's
-- own reporting (e.g. Boursorama SA, the parent behind the BoursoBank
-- brand). New "custodian" column: the second custody layer, optional, who
-- actually holds it day to day. Examples: custody_type=BROKER — Trade
-- Republic's custodian is CACEIS; custody_type=SELF_CUSTODY — the wallet
-- app is MetaMask; custody_type=INSURER — BoursoBank's AV underwriter is
-- Generali. Not restricted to those custody_types, stays free text,
-- nullable. holding_structure and key_storage are untouched — custodian is
-- additive, not a replacement for either.
ALTER TABLE core.accounts RENAME COLUMN institution TO ultimate_parent;
ALTER TABLE core.accounts ADD COLUMN custodian text;

-- Fund-level custody vocabulary, matching the account-level rename above:
-- "depositary" (layer 2a, who holds the FUND's assets) becomes
-- "custodian" — same word as core.accounts.custodian, different table, no
-- collision (layer 2a fund-level custodian vs layer 1b/2 account-level
-- custodian).
ALTER TABLE core.instrument_fund RENAME COLUMN depositary TO custodian;
ALTER TABLE core.instrument_fund DROP CONSTRAINT note_has_no_depositary;
ALTER TABLE core.instrument_fund ADD CONSTRAINT note_has_no_custodian CHECK (
    legal_structure <> 'UNSECURED_NOTE' OR custodian IS NULL);

-- New instrument_fund fields sourced from justETF (stockdex), ETP only in
-- practice — a non-listed FUND (OPCVM/SICAV/SCPI) isn't on justETF, so
-- these stay NULL there. fund_currency/fund_domicile/fund_provider are
-- deliberately NOT new columns: core.instruments.currency/domicile_country
-- /issuer already carry that meaning for an ETP/FUND row (issuer =
-- management company/provider, e.g. iShares — distinct from
-- instrument_fund's own management_company, which stays SCPI-specific,
-- see FIELDS.md).
ALTER TABLE core.instrument_fund
    ADD COLUMN investment_focus       text,
    ADD COLUMN fund_size              numeric(20,4),
    ADD COLUMN strategy_risk          text,
    ADD COLUMN sustainability         text,
    ADD COLUMN currency_risk          text,
    ADD COLUMN volatility_1y_eur      numeric(9,6),
    ADD COLUMN inception_date         date,
    ADD COLUMN distribution_frequency text;  -- ANNUAL | SEMI_ANNUAL | QUARTERLY |
                                              -- MONTHLY, unchecked free text —
                                              -- same pattern as sibling
                                              -- distribution_policy

-- ticker: the plain/display ticker as quoted on its exchange, distinct
-- from yf_symbol (the exact string yfinance needs — often the same,
-- sometimes with an exchange suffix like .PA/.DE, or a different format
-- for crypto/ETNs). Both manual, both stored, never derived from one
-- another.
--
-- industry / description: sector's siblings — now sourced via API +
-- config/exposure_mapping.csv the same way sector already is, rather than
-- typed by hand. industry is dimension-checked like sector; description is
-- free text (a cached paragraph from yfinance .info['longBusinessSummary']
-- or justETF's own fund description), not a dimension.
--
-- region is dropped entirely, not replaced by a rollup: it was a manual,
-- no-source, 3-code bucket — with real country-level data now coming from
-- the API via domicile_country, a separate region field is redundant.
ALTER TABLE core.instruments
    ADD COLUMN ticker      text,
    ADD COLUMN industry    text,
    ADD COLUMN description text,
    DROP COLUMN region;

CREATE VIEW marts.v_transactions AS
SELECT
    t.entry_id, t.txn_hash, t.account_id, a.broker, a.ultimate_parent, a.custodian,
    a.fiscal_envelope, a.account_type,
    t.trade_date, t.settle_date, t.txn_type,
    t.instrument_id, i.name AS instrument_name, i.isin,
    i.asset_class, i.instrument_type,
    t.quantity, t.price, t.gross_amount, t.fee, t.tax, t.net_amount,
    t.currency, t.fx_rate_to_base, t.fx_rate_date, t.fx_rate_source,
    t.is_estimated, t.note, t.source, t.source_file, t.created_at
FROM core.transactions t
JOIN core.accounts a ON a.account_id = t.account_id
LEFT JOIN core.instruments i ON i.instrument_id = t.instrument_id;

CREATE VIEW marts.v_allocation AS
SELECT
    vp.account_id, a.fiscal_envelope, vp.instrument_id,
    vp.asset_class, vp.instrument_type, i.sector, vp.currency,
    i.protection_type, vp.market_value_base AS value_base
FROM marts.v_positions vp
JOIN core.accounts a ON a.account_id = vp.account_id
JOIN core.instruments i ON i.instrument_id = vp.instrument_id

UNION ALL

SELECT
    cb.account_id, a.fiscal_envelope, NULL AS instrument_id,
    'CASH' AS asset_class, NULL AS instrument_type,
    NULL AS sector, cb.currency, NULL AS protection_type,
    cb.balance AS value_base
FROM marts.v_cash_balances cb
JOIN core.accounts a ON a.account_id = cb.account_id;

-- Verbatim from 004_pnl.sql — recreated unchanged, it never selected
-- institution/depositary/region itself, it just depends on v_transactions.
CREATE VIEW marts.v_ledger_walk AS
WITH RECURSIVE ordered AS (
    SELECT
        account_id, instrument_id, trade_date, txn_type, entry_id,
        quantity, net_amount, fx_rate_to_base,
        net_amount * fx_rate_to_base AS net_amount_base,
        ROW_NUMBER() OVER (
            PARTITION BY account_id, instrument_id
            ORDER BY trade_date, entry_id
        ) AS n
    FROM marts.v_transactions
    WHERE txn_type IN (
        'BUY', 'SELL', 'TRANSFER_IN', 'TRANSFER_OUT', 'SPLIT', 'OPENING_BALANCE'
    )
),
walk AS (
    SELECT
        account_id, instrument_id, n, trade_date, txn_type, entry_id,
        quantity::numeric AS qty,
        (-net_amount)::numeric AS cost_local,
        (-net_amount_base)::numeric AS cost_base,
        0::numeric AS realised_local,
        0::numeric AS realised_base
    FROM ordered
    WHERE n = 1

    UNION ALL

    SELECT
        o.account_id, o.instrument_id, o.n, o.trade_date, o.txn_type, o.entry_id,
        w.qty + o.quantity,
        CASE
            WHEN o.txn_type = 'SPLIT' THEN w.cost_local
            WHEN o.quantity > 0 THEN w.cost_local - o.net_amount
            ELSE w.cost_local - (w.cost_local / NULLIF(w.qty, 0)) * ABS(o.quantity)
        END,
        CASE
            WHEN o.txn_type = 'SPLIT' THEN w.cost_base
            WHEN o.quantity > 0 THEN w.cost_base - o.net_amount_base
            ELSE w.cost_base - (w.cost_base / NULLIF(w.qty, 0)) * ABS(o.quantity)
        END,
        CASE
            WHEN o.quantity < 0 AND o.txn_type <> 'SPLIT'
            THEN o.net_amount - (w.cost_local / NULLIF(w.qty, 0)) * ABS(o.quantity)
            ELSE 0
        END,
        CASE
            WHEN o.quantity < 0 AND o.txn_type <> 'SPLIT'
            THEN o.net_amount_base - (w.cost_base / NULLIF(w.qty, 0)) * ABS(o.quantity)
            ELSE 0
        END
    FROM walk w
    JOIN ordered o USING (account_id, instrument_id)
    WHERE o.n = w.n + 1
)
SELECT
    account_id, instrument_id, n, trade_date, txn_type, entry_id,
    qty,
    cost_local, cost_base,
    CASE WHEN qty = 0 THEN NULL ELSE cost_local / qty END AS avg_cost_local,
    CASE WHEN qty = 0 THEN NULL ELSE cost_base / qty END AS avg_cost_base,
    realised_local, realised_base,
    realised_base - realised_local AS currency_effect
FROM walk;

-- Verbatim from 005_income_pnl.sql.
CREATE VIEW marts.v_realised_pnl AS
SELECT
    lw.account_id, lw.instrument_id, lw.trade_date, lw.entry_id,
    t.quantity AS quantity_sold,
    t.net_amount AS proceeds_local,
    t.net_amount - lw.realised_local AS cost_of_disposal_local,
    lw.realised_local,
    t.net_amount * t.fx_rate_to_base AS proceeds_base,
    (t.net_amount * t.fx_rate_to_base) - lw.realised_base AS cost_of_disposal_base,
    lw.realised_base,
    lw.currency_effect
FROM marts.v_ledger_walk lw
JOIN marts.v_transactions t ON t.entry_id = lw.entry_id
WHERE lw.txn_type = 'SELL';

CREATE VIEW marts.v_income_yield AS
WITH trailing_income AS (
    SELECT instrument_id, SUM((gross_amount - tax) * fx_rate_to_base) AS income_base
    FROM core.transactions
    WHERE txn_type IN ('DIVIDEND', 'INTEREST', 'STAKING')
      AND trade_date >= CURRENT_DATE - INTERVAL '12 months'
      AND instrument_id IS NOT NULL
    GROUP BY instrument_id
),
latest_walk AS (
    SELECT DISTINCT ON (account_id, instrument_id)
        account_id, instrument_id, avg_cost_base, qty
    FROM marts.v_ledger_walk
    ORDER BY account_id, instrument_id, n DESC
),
cost_basis AS (
    SELECT instrument_id, SUM(avg_cost_base * qty) AS total_cost_basis_base
    FROM latest_walk
    WHERE qty > 0
    GROUP BY instrument_id
)
SELECT
    ti.instrument_id, ti.income_base, cb.total_cost_basis_base,
    ti.income_base / NULLIF(cb.total_cost_basis_base, 0) AS yield_on_cost
FROM trailing_income ti
JOIN cost_basis cb ON cb.instrument_id = ti.instrument_id;

CREATE VIEW marts.v_pnl_summary AS
WITH realised AS (
    SELECT
        COALESCE(SUM(realised_base), 0) AS realised_base,
        COALESCE(SUM(currency_effect), 0) AS realised_currency_effect
    FROM marts.v_realised_pnl
),
unrealised AS (
    SELECT COALESCE(SUM(vp.market_value_base - (lw.avg_cost_base * lw.qty)), 0)
        AS unrealised_base
    FROM marts.v_positions vp
    JOIN LATERAL (
        SELECT avg_cost_base, qty
        FROM marts.v_ledger_walk w
        WHERE w.account_id = vp.account_id AND w.instrument_id = vp.instrument_id
        ORDER BY w.n DESC
        LIMIT 1
    ) lw ON true
),
income AS (
    SELECT COALESCE(SUM(net_base), 0) AS income_base
    FROM marts.v_income
),
costs AS (
    SELECT COALESCE(SUM(
        CASE WHEN txn_type NOT IN ('DIVIDEND', 'INTEREST', 'STAKING')
             THEN (fee + tax) * fx_rate_to_base ELSE 0 END
        + CASE WHEN txn_type IN ('FEE', 'TAX')
               THEN ABS(net_amount) * fx_rate_to_base ELSE 0 END
    ), 0) AS costs_base
    FROM core.transactions
)
SELECT
    r.realised_base, u.unrealised_base, i.income_base, c.costs_base,
    r.realised_currency_effect,
    r.realised_base + u.unrealised_base + i.income_base - c.costs_base
        AS total_return
FROM realised r, unrealised u, income i, costs c;

-- Three custody layers now that the renames above split "who holds this"
-- into ultimate_parent (the group), account-level custodian (the
-- day-to-day holder of your units), and fund-level custodian (who holds
-- the FUND's own assets) — was two layers (institution, depositary).
CREATE VIEW marts.v_custody_exposure AS
SELECT 'ultimate_parent' AS layer, a.ultimate_parent AS custodian,
       SUM(vp.market_value_base) AS value_base
FROM marts.v_positions vp
JOIN core.accounts a ON a.account_id = vp.account_id
WHERE a.ultimate_parent IS NOT NULL
GROUP BY a.ultimate_parent

UNION ALL

SELECT 'account_custodian' AS layer, a.custodian AS custodian,
       SUM(vp.market_value_base) AS value_base
FROM marts.v_positions vp
JOIN core.accounts a ON a.account_id = vp.account_id
WHERE a.custodian IS NOT NULL
GROUP BY a.custodian

UNION ALL

SELECT 'fund_custodian' AS layer, fnd.custodian AS custodian,
       SUM(vp.market_value_base) AS value_base
FROM marts.v_positions vp
JOIN core.instrument_fund fnd ON fnd.instrument_id = vp.instrument_id
WHERE fnd.custodian IS NOT NULL
GROUP BY fnd.custodian;
