-- Target allocation per account: what fraction of a new contribution
-- should go to each underlying instrument (fonds euros, each unite de
-- compte sub-fund, ...). Generic on account_id, not AV-specific — a PER
-- contract works the same way. Seeded from config/account_allocations.csv,
-- one full snapshot per (account_id, as_of_date): changing the target
-- (an "arbitrage") means appending a new, later as_of_date block with the
-- complete new fund list/weights, never editing an existing row in place.
-- SUM(weight) per (account_id, as_of_date) is validated to be ~1 by the
-- seeder, not by a CHECK here — that needs a cross-row aggregate, which a
-- column CHECK can't express.
CREATE TABLE core.account_target_allocations (
    account_id    text NOT NULL REFERENCES core.accounts,
    as_of_date    date NOT NULL,
    instrument_id text NOT NULL REFERENCES core.instruments,
    weight        numeric(9,6) NOT NULL CHECK (weight > 0 AND weight <= 1),
    note          text,
    PRIMARY KEY (account_id, as_of_date, instrument_id)
);
