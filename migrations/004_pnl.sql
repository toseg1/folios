-- The average-cost ledger walk. This migration file is the tested SQL
-- file (build-plan step 11) — covered directly by pytest, and Metabase
-- simply selects from the view.
--
-- Per (account_id, instrument_id), in (trade_date, entry_id) order:
--   BUY / TRANSFER_IN / OPENING_BALANCE: qty += q, cost += (q*price+fee+tax)
--   SELL / TRANSFER_OUT: qty -= q, cost -= avg_cost*q, realised = proceeds - avg_cost*q
--   SPLIT: qty changes, cost unchanged
-- where avg_cost = cost / qty taken BEFORE the sale — a sale never moves
-- the average, which is the entire point of the method and why there are
-- no lots to track.
--
-- Must be recursive: average cost is a linear recurrence with additive
-- (buys) and multiplicative (proportional cost reduction on a sell)
-- terms. The only window-function route to the multiplicative part would
-- be a running product via EXP(SUM(LN(x))) — a float in disguise,
-- forbidden by the money-path rule in §1.
--
-- Run twice in the same pass, not as two separate queries: once on each
-- row's own net_amount (avg_cost_local, realised_local, in the
-- instrument's trading currency) and once on net_amount * fx_rate_to_base
-- (avg_cost_base, realised_base, in EUR). realised_base - realised_local
-- is the currency effect — zero for a EUR instrument (fx_rate_to_base
-- always 1), and the FX-driven component of realised P&L for anything
-- else, since both columns come from the identical recurrence and differ
-- only in which frozen rate was baked into each row before the walk ran.
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
