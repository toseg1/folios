-- Top-10 holdings by company — stockdex.justetf_holdings_companies exists
-- and was never called (data-model-review §3.3). Different grain from
-- core.etp_exposure: named top-10 companies, not a full mapped taxonomy,
-- so it gets its own table rather than reusing etp_exposure's
-- (dimension, code) shape. Append-only snapshots, same convention as
-- etp_exposure.
CREATE TABLE core.etp_top_holdings (
    instrument_id  text NOT NULL REFERENCES core.instruments,
    as_of_date     date NOT NULL,
    rank           smallint NOT NULL,
    company_name   text NOT NULL,
    weight         numeric(9,6) NOT NULL CHECK (weight >= 0 AND weight <= 1),
    PRIMARY KEY (instrument_id, as_of_date, rank)
);
CREATE INDEX ON core.etp_top_holdings (instrument_id, as_of_date);

CREATE VIEW marts.v_top_holdings AS
WITH latest AS (
    SELECT instrument_id, MAX(as_of_date) AS as_of_date
    FROM core.etp_top_holdings
    GROUP BY instrument_id
)
SELECT h.instrument_id, h.as_of_date, h.rank, h.company_name, h.weight
FROM core.etp_top_holdings h
JOIN latest l ON l.instrument_id = h.instrument_id AND l.as_of_date = h.as_of_date
ORDER BY h.instrument_id, h.rank;
