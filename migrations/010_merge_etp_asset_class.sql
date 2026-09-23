-- asset_class ETP and FUND merge into a single FUND value: instrument_type
-- (ETF/ETC/ETN vs UCITS/AIF/OTHER) already tracks 1:1 with the old split
-- and is the only signal actually needed downstream. legal_structure is
-- dropped too — is_ucits stays as the sole remaining wrapper signal. This
-- gives up the ETC/unsecured-note/SCPI distinction legal_structure used
-- to carry (see marts.v_wrapper_risk below and
-- note_has_no_custodian, neither of which has a legal_structure-free
-- equivalent) in exchange for not carrying two overlapping enums.

-- v_wrapper_risk depended on legal_structure — regroup by is_ucits before
-- the column is dropped. Coarser than before: ETC/ETN/NON_UCITS_FUND/SCPI
-- all collapse into NON_UCITS_OR_NOTE; re-derive a finer split from
-- instrument_type later if this ever needs it back.
--
-- CREATE OR REPLACE can't rename an existing output column
-- (legal_structure -> wrapper_risk) — Postgres requires DROP+CREATE for
-- that, not just a new query body.
DROP VIEW marts.v_wrapper_risk;
CREATE VIEW marts.v_wrapper_risk AS
SELECT
    CASE
        WHEN fnd.instrument_id IS NULL THEN 'DIRECT_HOLDING'
        WHEN fnd.is_ucits IS TRUE THEN 'UCITS_FUND'
        WHEN fnd.is_ucits IS FALSE THEN 'NON_UCITS_OR_NOTE'
        ELSE 'UNKNOWN'
    END AS wrapper_risk,
    SUM(vp.market_value_base) AS value_base
FROM marts.v_positions vp
LEFT JOIN core.instrument_fund fnd ON fnd.instrument_id = vp.instrument_id
GROUP BY 1;

-- "An unsecured note has no fund assets, so it can have no custodian" only
-- meant something with legal_structure = 'UNSECURED_NOTE' on the same
-- row. is_ucits=false doesn't imply "no custodian" (ETCs, SCPIs, non-UCITS
-- funds can have one) so there's no drop-in replacement — the check goes,
-- not a looser version of it. Its own comment already called it
-- pedagogical, not load-bearing.
ALTER TABLE core.instrument_fund DROP CONSTRAINT note_has_no_custodian;
ALTER TABLE core.instrument_fund DROP COLUMN legal_structure;

-- v_lookthrough_allocation used asset_class = 'ETP' as "exchange-traded
-- wrapper, look-through via core.etp_exposure applies". That's now
-- instrument_type's job — the only field left that still distinguishes an
-- ETF/ETC/ETN from a non-listed fund now that both are asset_class=FUND.
CREATE OR REPLACE VIEW marts.v_lookthrough_allocation AS
WITH etp_positions AS (
    SELECT vp.instrument_id, SUM(vp.market_value_base) AS market_value_base
    FROM marts.v_positions vp
    JOIN core.instruments i ON i.instrument_id = vp.instrument_id
    WHERE i.instrument_type IN ('ETF', 'ETC', 'ETN')
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
    WHERE (i.instrument_type IS NULL OR i.instrument_type NOT IN ('ETF', 'ETC', 'ETN'))
      AND i.sector IS NOT NULL
    GROUP BY i.sector

    UNION ALL

    SELECT 'country' AS dimension, i.domicile_country AS code,
           i.domicile_country AS label, SUM(vp.market_value_base) AS value_base
    FROM marts.v_positions vp
    JOIN core.instruments i ON i.instrument_id = vp.instrument_id
    WHERE (i.instrument_type IS NULL OR i.instrument_type NOT IN ('ETF', 'ETC', 'ETN'))
      AND i.domicile_country IS NOT NULL
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
LEFT JOIN core.dimensions dim
    ON dim.dimension = combined.dimension AND dim.code = combined.code
GROUP BY combined.dimension, combined.code, dim.label_en;

-- config/dimensions.csv no longer has this row; seed() only upserts, it
-- never deletes, so without this the code lingers in core.dimensions
-- forever. Safe regardless of instrument data: nothing enforces a DB-level
-- FK from core.instruments.asset_class to core.dimensions.
DELETE FROM core.dimensions WHERE dimension = 'asset_class' AND code = 'ETP';
