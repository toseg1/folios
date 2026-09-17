-- Plain views, not materialised. At a few thousand transactions the query
-- cost is irrelevant and a view can never be stale (build-plan step 9).

CREATE VIEW marts.v_transactions AS
SELECT
    t.entry_id, t.txn_hash, t.account_id, a.broker, a.institution,
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

-- Account x instrument. Current holdings, latest close, latest FX. A
-- snapshot, not a series — v_positions_daily below is the series.
CREATE VIEW marts.v_positions AS
WITH positions AS (
    SELECT account_id, instrument_id, SUM(quantity) AS quantity
    FROM core.transactions
    WHERE instrument_id IS NOT NULL
    GROUP BY account_id, instrument_id
    HAVING SUM(quantity) <> 0
),
latest_price AS (
    SELECT DISTINCT ON (instrument_id)
        instrument_id, price_date, close_price
    FROM core.prices
    ORDER BY instrument_id, price_date DESC
),
latest_fx AS (
    SELECT DISTINCT ON (quote_ccy)
        quote_ccy, rate_date, rate
    FROM core.fx_rates
    WHERE base_ccy = 'EUR'
    ORDER BY quote_ccy, rate_date DESC
)
SELECT
    p.account_id, p.instrument_id, i.name, i.asset_class, i.instrument_type,
    i.currency, p.quantity,
    lp.price_date AS latest_price_date, lp.close_price AS latest_price,
    CASE WHEN i.currency = 'EUR' THEN 1 ELSE lf.rate END AS fx_rate_to_base,
    lf.rate_date AS fx_rate_date,
    p.quantity * lp.close_price
        / CASE WHEN i.currency = 'EUR' THEN 1 ELSE NULLIF(lf.rate, 0) END
        AS market_value_base
FROM positions p
JOIN core.instruments i ON i.instrument_id = p.instrument_id
LEFT JOIN latest_price lp ON lp.instrument_id = p.instrument_id
LEFT JOIN latest_fx lf ON lf.quote_ccy = i.currency;

-- Date x account x instrument. generate_series spine from each pair's
-- first trade through today, cumulative quantity, forward-filled close
-- and FX — every Saturday must not be null in base currency (build-plan
-- §3's FX-resolution note, extended to prices the same way).
--
-- Forward-fill technique: COUNT(x) OVER (ORDER BY date) only increments
-- on an actual value, so it tags every day since with the "last real
-- value's group"; MAX(x) OVER (PARTITION BY that group) then carries it
-- forward. No IGNORE NULLS needed (not universally available), no
-- correlated subquery per row.
CREATE VIEW marts.v_positions_daily AS
WITH instrument_accounts AS (
    SELECT DISTINCT t.account_id, t.instrument_id, i.currency
    FROM core.transactions t
    JOIN core.instruments i ON i.instrument_id = t.instrument_id
    WHERE t.instrument_id IS NOT NULL
),
bounds AS (
    SELECT MIN(trade_date) AS min_date
    FROM core.transactions
    WHERE instrument_id IS NOT NULL
),
spine AS (
    SELECT ia.account_id, ia.instrument_id, ia.currency, gs::date AS as_of_date
    FROM instrument_accounts ia
    CROSS JOIN bounds b
    CROSS JOIN LATERAL generate_series(
        b.min_date::timestamp, CURRENT_DATE::timestamp, interval '1 day'
    ) AS gs
),
deltas AS (
    SELECT account_id, instrument_id, trade_date AS as_of_date, SUM(quantity) AS delta
    FROM core.transactions
    WHERE instrument_id IS NOT NULL
    GROUP BY account_id, instrument_id, trade_date
),
running_qty AS (
    SELECT
        s.account_id, s.instrument_id, s.currency, s.as_of_date,
        SUM(COALESCE(d.delta, 0)) OVER (
            PARTITION BY s.account_id, s.instrument_id ORDER BY s.as_of_date
        ) AS quantity
    FROM spine s
    LEFT JOIN deltas d
        ON d.account_id = s.account_id AND d.instrument_id = s.instrument_id
       AND d.as_of_date = s.as_of_date
),
price_points AS (
    SELECT DISTINCT instrument_id, as_of_date FROM running_qty
),
price_grouped AS (
    SELECT
        pp.instrument_id, pp.as_of_date, pr.close_price,
        COUNT(pr.close_price) OVER (
            PARTITION BY pp.instrument_id ORDER BY pp.as_of_date
        ) AS grp
    FROM price_points pp
    LEFT JOIN core.prices pr
        ON pr.instrument_id = pp.instrument_id AND pr.price_date = pp.as_of_date
),
price_ffill AS (
    SELECT instrument_id, as_of_date,
           MAX(close_price) OVER (PARTITION BY instrument_id, grp) AS close_price
    FROM price_grouped
),
fx_points AS (
    SELECT DISTINCT currency, as_of_date FROM running_qty WHERE currency <> 'EUR'
),
fx_grouped AS (
    SELECT
        fp.currency, fp.as_of_date, fr.rate,
        COUNT(fr.rate) OVER (
            PARTITION BY fp.currency ORDER BY fp.as_of_date
        ) AS grp
    FROM fx_points fp
    LEFT JOIN core.fx_rates fr
        ON fr.quote_ccy = fp.currency AND fr.base_ccy = 'EUR'
       AND fr.rate_date = fp.as_of_date
),
fx_ffill AS (
    SELECT currency, as_of_date,
           MAX(rate) OVER (PARTITION BY currency, grp) AS fx_rate_to_base
    FROM fx_grouped
)
SELECT
    rq.account_id, rq.instrument_id, rq.as_of_date, rq.quantity,
    pf.close_price,
    CASE WHEN rq.currency = 'EUR' THEN 1 ELSE ff.fx_rate_to_base END AS fx_rate_to_base,
    rq.quantity * pf.close_price
        / CASE WHEN rq.currency = 'EUR' THEN 1 ELSE NULLIF(ff.fx_rate_to_base, 0) END
        AS market_value_base
FROM running_qty rq
LEFT JOIN price_ffill pf
    ON pf.instrument_id = rq.instrument_id AND pf.as_of_date = rq.as_of_date
LEFT JOIN fx_ffill ff
    ON ff.currency = rq.currency AND ff.as_of_date = rq.as_of_date;

-- Account x currency. Excludes OPENING_BALANCE — it establishes a
-- security position's cost basis without cash actually moving.
CREATE VIEW marts.v_cash_balances AS
SELECT account_id, currency, SUM(net_amount) AS balance
FROM core.transactions
WHERE txn_type <> 'OPENING_BALANCE'
GROUP BY account_id, currency;

-- UNION ALL of instrument positions and cash balances, denormalised with
-- every dimension a dashboard might group by. Position-level — see
-- v_lookthrough_allocation below for the decomposed alternative. Both are
-- legitimate and give different answers; label which one is on screen.
CREATE VIEW marts.v_allocation AS
SELECT
    vp.account_id, a.fiscal_envelope, vp.instrument_id,
    vp.asset_class, vp.instrument_type, i.region, i.sector, vp.currency,
    i.protection_type, vp.market_value_base AS value_base
FROM marts.v_positions vp
JOIN core.accounts a ON a.account_id = vp.account_id
JOIN core.instruments i ON i.instrument_id = vp.instrument_id

UNION ALL

SELECT
    cb.account_id, a.fiscal_envelope, NULL AS instrument_id,
    'CASH' AS asset_class, NULL AS instrument_type, NULL AS region,
    NULL AS sector, cb.currency, NULL AS protection_type,
    cb.balance AS value_base
FROM marts.v_cash_balances cb
JOIN core.accounts a ON a.account_id = cb.account_id;

-- Dimension x code, portfolio-wide. Position value x exposure weight for
-- ETPs (latest snapshot on or before today), unioned with direct
-- (non-ETP) holdings counted at weight 1. Cash has no sector or country,
-- so it never appears here — that's expected, not a gap.
CREATE VIEW marts.v_lookthrough_allocation AS
WITH etp_positions AS (
    SELECT vp.instrument_id, SUM(vp.market_value_base) AS market_value_base
    FROM marts.v_positions vp
    JOIN core.instruments i ON i.instrument_id = vp.instrument_id
    WHERE i.asset_class = 'ETP'
    GROUP BY vp.instrument_id
),
latest_exposure AS (
    SELECT DISTINCT ON (instrument_id, dimension, code)
        instrument_id, dimension, code, label, weight
    FROM core.etp_exposure
    WHERE as_of_date <= CURRENT_DATE
    ORDER BY instrument_id, dimension, code, as_of_date DESC
),
lookthrough AS (
    SELECT le.dimension, le.code, le.label,
           ep.market_value_base * le.weight AS value_base
    FROM etp_positions ep
    JOIN latest_exposure le ON le.instrument_id = ep.instrument_id
),
direct AS (
    SELECT 'sector' AS dimension, i.sector AS code, i.sector AS label,
           SUM(vp.market_value_base) AS value_base
    FROM marts.v_positions vp
    JOIN core.instruments i ON i.instrument_id = vp.instrument_id
    WHERE i.asset_class <> 'ETP' AND i.sector IS NOT NULL
    GROUP BY i.sector

    UNION ALL

    SELECT 'country' AS dimension, i.domicile_country AS code,
           i.domicile_country AS label, SUM(vp.market_value_base) AS value_base
    FROM marts.v_positions vp
    JOIN core.instruments i ON i.instrument_id = vp.instrument_id
    WHERE i.asset_class <> 'ETP' AND i.domicile_country IS NOT NULL
    GROUP BY i.domicile_country
)
SELECT
    combined.dimension, combined.code,
    COALESCE(dim.label_en, MAX(combined.label)) AS label,
    SUM(combined.value_base) AS value_base
FROM (
    SELECT dimension, code, label, value_base FROM lookthrough
    UNION ALL
    SELECT dimension, code, label, value_base FROM direct
) combined
-- Same code can arrive with different raw labels (the scraped "Technology"
-- vs. an instrument's own stored "TECHNOLOGY") — group by code alone and
-- prefer the canonical core.dimensions label so they collapse to one row,
-- not two slices of the same pie.
LEFT JOIN core.dimensions dim
    ON dim.dimension = combined.dimension AND dim.code = combined.code
GROUP BY combined.dimension, combined.code, dim.label_en;

-- Month x account. Embedded fee/tax on trades, plus standalone FEE/TAX
-- rows (whose own fee/tax columns are always 0 — the amount is in
-- net_amount — so no double count). Withholding on DIVIDEND/INTEREST is
-- deliberately excluded: it belongs to v_income only, not here.
CREATE VIEW marts.v_costs AS
SELECT
    date_trunc('month', trade_date)::date AS month,
    account_id,
    SUM(CASE WHEN txn_type NOT IN ('DIVIDEND', 'INTEREST', 'STAKING')
             THEN fee + tax ELSE 0 END) AS embedded_costs,
    SUM(CASE WHEN txn_type IN ('FEE', 'TAX') THEN ABS(net_amount) ELSE 0 END)
        AS standalone_costs,
    SUM(CASE WHEN txn_type NOT IN ('DIVIDEND', 'INTEREST', 'STAKING')
             THEN fee + tax ELSE 0 END)
        + SUM(CASE WHEN txn_type IN ('FEE', 'TAX') THEN ABS(net_amount) ELSE 0 END)
        AS total_costs
FROM core.transactions
GROUP BY date_trunc('month', trade_date), account_id;

-- Account x instrument x date, derived vs statement_positions, plus a
-- cash leg vs statement_cash. Empty is good — every row here is a
-- discrepancy between what was entered and what a statement says.
CREATE VIEW marts.v_reconciliation AS
SELECT
    'position' AS leg, sp.account_id, sp.instrument_id, sp.as_of AS as_of_date,
    sp.quantity AS statement_value,
    COALESCE(d.derived_value, 0) AS derived_value,
    sp.quantity - COALESCE(d.derived_value, 0) AS diff
FROM core.statement_positions sp
LEFT JOIN LATERAL (
    SELECT SUM(t.quantity) AS derived_value
    FROM core.transactions t
    WHERE t.account_id = sp.account_id AND t.instrument_id = sp.instrument_id
      AND t.trade_date <= sp.as_of
) d ON true
WHERE sp.quantity <> COALESCE(d.derived_value, 0)

UNION ALL

SELECT
    'cash' AS leg, sc.account_id, NULL AS instrument_id, sc.as_of AS as_of_date,
    sc.balance AS statement_value,
    COALESCE(d.derived_value, 0) AS derived_value,
    sc.balance - COALESCE(d.derived_value, 0) AS diff
FROM core.statement_cash sc
LEFT JOIN LATERAL (
    SELECT SUM(t.net_amount) AS derived_value
    FROM core.transactions t
    WHERE t.account_id = sc.account_id AND t.currency = sc.currency
      AND t.trade_date <= sc.as_of AND t.txn_type <> 'OPENING_BALANCE'
) d ON true
WHERE sc.balance <> COALESCE(d.derived_value, 0);

-- Source table: max date, row count, last successful load (from
-- load_runs, where that audit trail exists yet — prices/fx/exposure
-- don't have their own audit log table, so that column is NULL there).
CREATE VIEW marts.v_data_freshness AS
SELECT
    'transactions' AS source_table, MAX(t.trade_date) AS max_date, COUNT(*) AS row_count,
    (SELECT MAX(finished_at) FROM core.load_runs
     WHERE source = 'manual' AND status = 'success') AS last_successful_sync
FROM core.transactions t

UNION ALL

SELECT 'prices', MAX(price_date), COUNT(*), NULL
FROM core.prices

UNION ALL

SELECT 'fx_rates', MAX(rate_date), COUNT(*), NULL
FROM core.fx_rates

UNION ALL

SELECT 'etp_exposure', MAX(as_of_date), COUNT(*), NULL
FROM core.etp_exposure;
