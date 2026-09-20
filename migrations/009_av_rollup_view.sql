-- Assurance-vie rollup: no schema change needed to model an AV contract
-- (one core.accounts row, fiscal_envelope=AV, each compartment — fonds
-- euros, each unite de compte sub-fund — an ordinary core.instruments row
-- held via ordinary transactions in that account). The only real gap was a
-- view to actually see it rolled up: one AV contract's positions grouped
-- by instrument and asset_class, ready to dashboard.
CREATE VIEW marts.v_av_positions AS
SELECT
    vp.account_id, a.broker, a.ultimate_parent, a.custodian,
    vp.instrument_id, i.name AS instrument_name, i.asset_class,
    i.instrument_type, vp.quantity, vp.latest_price_date, vp.latest_price,
    vp.market_value_base
FROM marts.v_positions vp
JOIN core.accounts a ON a.account_id = vp.account_id
JOIN core.instruments i ON i.instrument_id = vp.instrument_id
WHERE a.fiscal_envelope = 'AV';
