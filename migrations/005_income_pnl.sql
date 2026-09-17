-- Income and P&L summary views, per build-plan step 12.
--
-- Conventions pinned down there, carried through here:
-- - Withholding tax on income lives in v_income and nowhere else — it
--   must not also reduce a cost view, or total return is wrong by twice
--   the withholding.
-- - A dividend never touches cost basis or quantity (already true: only
--   BUY/SELL/TRANSFER_IN/TRANSFER_OUT/SPLIT/OPENING_BALANCE feed
--   v_ledger_walk).
-- - A buy/sell commission reduces realised gain exactly once (already
--   baked into v_ledger_walk via net_amount) and must not also appear
--   in a cost view here.

-- Disposal grain. Only real economic disposals (SELL) — TRANSFER_OUT
-- moves cost basis between accounts, it isn't a sale, and v_ledger_walk's
-- own "realised" for it (a mechanical side effect of sharing the sale
-- formula) would misrepresent it as a loss if reported as P&L here.
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

-- Month x instrument. Three streams, valued as published on the row
-- itself — dividends and interest are cash, staking is in kind (its
-- gross_amount is the market value at receipt, per the loader).
CREATE VIEW marts.v_income AS
SELECT
    date_trunc('month', trade_date)::date AS month,
    account_id, instrument_id, txn_type AS stream,
    gross_amount AS gross,
    tax AS withholding,
    gross_amount - tax AS net,
    (gross_amount - tax) * fx_rate_to_base AS net_base
FROM core.transactions
WHERE txn_type IN ('DIVIDEND', 'INTEREST', 'STAKING');

-- Instrument x account. ongoing_charges x market value — the annual cost
-- of merely holding, in euros. SUM() this across rows for the portfolio
-- total; no separate total row here to avoid mixing grains.
CREATE VIEW marts.v_fee_drag AS
SELECT
    vp.account_id, vp.instrument_id, fnd.ongoing_charges,
    vp.market_value_base * fnd.ongoing_charges AS annual_fee_drag_base
FROM marts.v_positions vp
JOIN core.instrument_fund fnd ON fnd.instrument_id = vp.instrument_id
WHERE fnd.ongoing_charges IS NOT NULL;

-- Two layers, unioned: who holds YOUR units (accounts.institution) and
-- who holds the FUND's assets (instrument_fund.depositary). This is what
-- surfaces five "diversified" ETFs all depositing at the same bank.
CREATE VIEW marts.v_custody_exposure AS
SELECT 'institution' AS layer, a.institution AS custodian,
       SUM(vp.market_value_base) AS value_base
FROM marts.v_positions vp
JOIN core.accounts a ON a.account_id = vp.account_id
WHERE a.institution IS NOT NULL
GROUP BY a.institution

UNION ALL

SELECT 'depositary' AS layer, fnd.depositary AS custodian,
       SUM(vp.market_value_base) AS value_base
FROM marts.v_positions vp
JOIN core.instrument_fund fnd ON fnd.instrument_id = vp.instrument_id
WHERE fnd.depositary IS NOT NULL
GROUP BY fnd.depositary;

-- Value by SRI band, 1-7. A distribution, never an average — the PRIIPs
-- scale is ordinal, and "portfolio SRI 4.2" is meaningless.
CREATE VIEW marts.v_risk_profile AS
SELECT fnd.sri, SUM(vp.market_value_base) AS value_base
FROM marts.v_positions vp
JOIN core.instrument_fund fnd ON fnd.instrument_id = vp.instrument_id
WHERE fnd.sri IS NOT NULL
GROUP BY fnd.sri;

-- Instrument x as_of_date x dimension. SUM(weight) and constituent count
-- per snapshot — every look-through figure in v_lookthrough_allocation
-- should be shown beside its coverage from here, never presented alone.
CREATE VIEW marts.v_lookthrough_coverage AS
SELECT instrument_id, as_of_date, dimension,
       SUM(weight) AS coverage, COUNT(*) AS constituent_count
FROM core.etp_exposure
GROUP BY instrument_id, as_of_date, dimension;

-- Value by legal_structure: ring-fenced funds (UCITS_FUND) vs
-- collateralised notes vs unsecured issuer debt (UNSECURED_NOTE) vs a
-- direct holding with no subtype row at all (a share, a bond held
-- directly, crypto — none of those are "wrapped").
CREATE VIEW marts.v_wrapper_risk AS
SELECT
    COALESCE(fnd.legal_structure, 'DIRECT_HOLDING') AS legal_structure,
    SUM(vp.market_value_base) AS value_base
FROM marts.v_positions vp
LEFT JOIN core.instrument_fund fnd ON fnd.instrument_id = vp.instrument_id
GROUP BY COALESCE(fnd.legal_structure, 'DIRECT_HOLDING');

-- Instrument, trailing 12 months. Income (converted to base at each
-- row's own frozen rate) over the current average cost basis (also
-- base) — yield on cost, not headline yield, and not the same number as
-- a fund's own published distribution_rate.
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

-- Portfolio-wide, all time. Realised + unrealised + income - costs =
-- total return, currency effect broken out separately (it's already
-- included inside realised_base, not an extra addend). Costs are
-- recomputed here in base currency rather than reusing v_costs, which is
-- local-currency by design — combining local costs with base-currency
-- realised/unrealised/income would silently mix currencies.
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
