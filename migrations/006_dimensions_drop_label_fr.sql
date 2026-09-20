-- label_fr was required and seeded on every core.dimensions row, but no
-- view or dashboard ever read it (only label_en, in v_lookthrough_allocation
-- — see docs/folios-data-model-review.md §5). Dropping it outright rather
-- than leaving dead, unread data around.
ALTER TABLE core.dimensions DROP COLUMN label_fr;
