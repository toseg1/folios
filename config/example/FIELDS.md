# config/ field reference

Every field in every `config/` file, what it means, and its possible
values. Source of truth for all of this is the schema itself
(`migrations/001_schemas_and_core.sql`) and the seeder
(`src/folios/seed.py`) — this file is a human-readable index into
those, not a new source of truth. If the two ever disagree, the
migration and the seeder win.

Two kinds of fields below:

- **Dimension-valued** — validated by the seeder against
  `config/dimensions.csv` at `folios init` time. An unknown code is a
  hard error, nothing gets written. Extend these by adding a row to
  `dimensions.csv`, no migration needed (`CLAUDE.md`: "Dimensions are
  data, transaction types are code").
- **Free text / documented-but-unchecked** — the schema comments list
  the intended values, but the seeder does not enforce them. A typo
  here doesn't block `folios init`, it just shows up as a stray value
  in a chart later.

## config/dimensions.csv

One row per (dimension, code) pair — the controlled vocabulary every
dimension-valued field below draws from.

| Column | Meaning |
| --- | --- |
| `dimension` | Which field this code belongs to, e.g. `fiscal_envelope`, `sector`. Always matches a field name from the tables below. |
| `code` | The value itself, e.g. `CTO`, `TECHNOLOGY`. What you actually type into `accounts.yml` / `instruments.csv`. |
| `label_fr` | French display label. |
| `label_en` | English display label. |
| `sort_order` | Integer; controls display order in Metabase, not meaning. |

## config/accounts.yml

One list of mappings, one per account. Required fields have no
default; everything else is optional and defaults as noted.

| Field | Required | Meaning | Values |
| --- | --- | --- | --- |
| `account_id` | **yes** | Stable short code you reference everywhere (transactions, the Form dropdown). Primary key — never reuse one for a different account. | Free text, your convention (e.g. `BOURSOBANK-CTO`) |
| `broker` | **yes** | The brand/app you actually use day to day. | Free text |
| `institution` | no | The legal entity actually holding custody — matters when it differs from the brand (a neobroker routing custody through a separate regulated bank). Used by `v_custody_exposure` to catch concentration risk: several "different" brokers custodying through the same bank show up as one slice. If unknown, repeat `broker`. | Free text |
| `account_type` | **yes** | *Dimension.* Broad structural category: does this account hold securities, or just money? | `securities`, `cash`, `crypto`|
| `fiscal_envelope` | no | *Dimension.* The tax/regulatory wrapper this specific account is. One account = one envelope, for life — it's a property of the account, not the transaction. | `PEA`, `PEA_PME`, `CTO`, `AV`, `PER`, `LIVRET_A`, `LDDS`, `LEP`, `PEL`, `CAT`, `CURRENT` |
| `custody_type` | no | *Dimension.* **Custody layer 1**: who holds your units, and what happens if they fail. `BANK` = you're a creditor of the bank (protected by `guarantee_scheme` up to a ceiling). `BROKER` = securities held in custody on your behalf (see `holding_structure` for how cleanly segregated). `INSURER` = inside an `AV`/`PER` insurance wrapper. `EXCHANGE` = held by a crypto exchange (still their balance sheet). `SELF_CUSTODY` = you hold the keys, no institution involved. | `BROKER`, `BANK`, `INSURER`, `EXCHANGE`, `SELF_CUSTODY`, `DIRECT_REGISTRAR` |
| `holding_structure` | no, defaults `UNKNOWN` | Documented, not dimension-checked. **Custody layer 1b**, refines `BROKER`/`EXCHANGE`: exactly how segregated your units are, i.e. what's left of them if the intermediary goes bankrupt. `DIRECT_REGISTERED` (nominatif pur — registered directly in your name at the issuer/registrar, the cleanest case) : `NOMINEE_SEGREGATED` (held in the broker's name but in a segregated sub-account, legally yours) : `OMNIBUS` (pooled with other clients' assets, harder to trace back to you specifically) : `UNKNOWN`. | `DIRECT_REGISTERED`, `NOMINEE_SEGREGATED`, `OMNIBUS`, `UNKNOWN` |
| `key_storage` | no | Documented, not dimension-checked. **Custody layer 2b**: how your crypto keys are actually stored. Only meaningful — and only allowed by a database CHECK — when `custody_type = SELF_CUSTODY`; you cannot answer this about an exchange-held balance. | `COLD_HARDWARE`, `COLD_PAPER`, `MULTISIG`, `MPC`, `HOT_SOFTWARE` |
| `contractual_rate` | no | The locked-in interest rate, for products where the rate is a property of *this* account (`PEL`, `CAT`). Never use this for administered rates like Livret A/LDDS — those change by government decree and aren't a property of your account. | Decimal, e.g. `0.0175` for 1.75% |
| `maturity_date` | no | When a locked product becomes available (vs. protected-but-available). | ISO date `YYYY-MM-DD` |
| `deposit_ceiling` | no | The regulatory cap for this envelope, used to show remaining headroom (Livret A: 22950, LDDS: 12000). | Decimal amount |
| `guarantee_scheme` | no | Which deposit/investor protection scheme covers this account. | `FGDR`, `FGAP`, `NONE` |
| `base_currency` | **yes** | The currency this account's balance/statements are denominated in. | ISO 4217 code, e.g. `EUR`, `USD` |
| `opened_on` | no | When you opened the account. Approximate is fine. | ISO date |
| `closed_on` | no | When you closed it, if you have. | ISO date |
| `is_active` | no, defaults `true` | Whether the account is still in use. | `true` / `false` |

## config/instruments.csv

One row per instrument (a share, an ETF, a bond, a crypto token, …).
Equities need no subtype row — everything they need is here. Funds,
bonds and crypto also need a matching row in the subtype file below,
keyed on the same `instrument_id`.

| Column | Required | Meaning | Values |
| --- | --- | --- | --- |
| `instrument_id` | **yes** | Stable short code, your convention. Primary key. | Free text |
| `isin` | no | ISIN, if it has one. Unique if set. | 12-char ISIN |
| `yf_symbol` | no | Yahoo Finance ticker, used to pull prices when `price_source = yfinance`. | Free text, e.g. `AAPL`, `DEMW.PA` |
| `name` | **yes** | Display name. | Free text |
| `asset_class` | **yes** | *Dimension.* The broad category — also decides whether a subtype row is expected (`ETP`/`FUND` → `instruments_fund.csv`, `BOND` → `instruments_bond.csv`, `CRYPTO` → `instruments_crypto.csv`). Deliberately no `CASH`: cash is derived from transaction sums, never a held instrument. | `EQUITY`, `ETP`, `FUND`, `BOND`, `CRYPTO` |
| `instrument_type` | no | *Dimension.* The specific wrapper. `ETP` is the umbrella asset class; this is what it actually is underneath — an ETN is not a fund and owns nothing (see `legal_structure` in the fund subtype table). | `ETF`, `ETC`, `ETN`, `OPCVM`, `SICAV`, `SCPI`, `SHARE`, `BOND`, `TOKEN` |
| `currency` | **yes** | The instrument's trading/quote currency. | ISO 4217 code |
| `region` | no | *Dimension.* | e.g. `EUROPE`, `GLOBAL`, `FRANCE` — extend as needed |
| `sector` | no | *Dimension.* | e.g. `TECHNOLOGY`, `FINANCE`, `DIVERSIFIED` — extend as needed |
| `issuer` | no | Who issues/manages it. | Free text |
| `domicile_country` | no | Where the instrument is domiciled. | ISO 3166-1 alpha-2, e.g. `IE`, `FR` |
| `protection_type` | no | *Dimension.* | `GUARANTEED`, `MARKET_EXPOSED`, `PARTIALLY_PROTECTED`, `UNKNOWN` |
| `is_pea_eligible` | no | Whether it can be held in a French PEA. `NULL`/blank means unknown, shown as such rather than guessed. | `true` / `false` / blank |
| `price_source` | no, defaults `yfinance` | Where `folios prices` pulls its close price from. Database-enforced, not just a dimension. | `yfinance`, `manual` |
| `is_active` | no, defaults `true` | | `true` / `false` |

## config/instruments_fund.csv

One row per instrument with `asset_class` in `ETP`/`FUND`, keyed on
`instrument_id`.

| Column | Meaning | Values |
| --- | --- | --- |
| `legal_structure` | **The field that decides everything downstream** — what you actually own if the issuer fails. | `UCITS_FUND` (ETF/OPCVM/SICAV — ring-fenced assets, a depositary, real holdings, look-through applies) : `NON_UCITS_FUND` (a fund, outside UCITS protection) : `COLLATERALISED_NOTE` (most ETCs — debt backed by collateral, not a fund) : `UNSECURED_NOTE` (ETNs — unsecured debt of the issuer, no ring-fenced assets, no depositary; if the issuer fails you're a creditor, not an owner) : `SCPI` (French property vehicle) |
| `is_ucits` | Whether it's UCITS-compliant. | `true` / `false` |
| `rhp_years` | Recommended holding period, from the PRIIPs KID. | Integer years |
| `distribution_policy` | Whether income is paid out or reinvested. | `ACCUMULATING`, `DISTRIBUTING` |
| `ongoing_charges` | Annual cost, **as a decimal fraction** — 0.20% is `0.0020`, not `0.20`. | Decimal fraction |
| `sri` | PRIIPs KID risk indicator, stored exactly as published. Ordinal 1–7 — never average this, only distribute it. | Integer 1–7 |
| `replication_method` | How the fund tracks its index. | `PHYSICAL_FULL`, `PHYSICAL_SAMPLED`, `SYNTHETIC` |
| `swap_counterparty` | Only for synthetic replication. | Free text |
| `uses_sec_lending` | Whether the fund lends out its holdings. | `true` / `false` |
| `depositary` | **Custody layer 2a**: who holds the *fund's* underlying assets (as opposed to `accounts.custody_type`, which is who holds *your units of the fund*). `NULL` required if `legal_structure = UNSECURED_NOTE` — a note has no fund assets, so it can't have a depositary. | Free text |
| `sfdr_article` | EU sustainability disclosure classification. | `6`, `8`, `9` |
| `benchmark_index` | The index it tracks — used for overlap detection without full look-through. | Free text |
| `justetf_id` | justETF's own identifier, if pulling exposure data from there. | Free text |
| `subscription_price` / `withdrawal_price` | SCPI only — the two prices SCPIs quote instead of a market price; value the position at `withdrawal_price`. | Decimal |
| `management_company` | SCPI only. | Free text |
| `property_sector` | SCPI only. | Free text |
| `occupancy_rate` | SCPI only, decimal fraction. | Decimal fraction |
| `distribution_rate` | SCPI only, decimal fraction. | Decimal fraction |

## config/instruments_bond.csv

One row per instrument with `asset_class = BOND`, keyed on
`instrument_id`.

| Column | Meaning | Values |
| --- | --- | --- |
| `coupon_rate` | Annual coupon, as a decimal fraction. | Decimal fraction |
| `coupon_frequency` | How often the coupon pays. | `ANNUAL`, `SEMI_ANNUAL`, `QUARTERLY`, `ZERO` |
| `maturity_date` | | ISO date |
| `face_value` | Redemption value per unit. | Decimal |
| `issuer_type` | Who issued it. | `SOVEREIGN`, `CORPORATE`, `FINANCIAL`, `SUPRANATIONAL` |
| `credit_rating` | As published by the rating agency. | Free text, e.g. `AA`, `BBB+` |
| `seniority` | Where it ranks in a default. | `SENIOR_SECURED`, `SENIOR_UNSECURED`, `SUBORDINATED` |
| `is_callable` | Whether the issuer can redeem it early. Defaults `false`. | `true` / `false` |

## config/instruments_crypto.csv

One row per instrument with `asset_class = CRYPTO`, keyed on
`instrument_id`.

| Column | Meaning | Values |
| --- | --- | --- |
| `chain` | **Required.** Which chain it lives on — USDC on Ethereum and USDC on Solana are deliberately two different instruments: different contract, different bridge exposure, different failure mode. | Free text, e.g. `Bitcoin`, `Ethereum`, `Solana` |
| `contract_address` | Token contract address, if applicable (not for native assets like BTC). | Free text |
| `token_standard` | | Free text, e.g. `NATIVE`, `ERC20`, `SPL` |
| `consensus` | | `POW`, `POS` |
| `is_stablecoin` | Defaults `false`. | `true` / `false` |
| `peg_currency` | If a stablecoin, what it's pegged to. | ISO 4217 code |

## config/aliases.csv

Many-to-one: several spellings can map to the same instrument. This is
what `symbol` in a transaction CSV actually resolves against — never
rename an alias to fix a mismatch, add a new one instead.

| Column | Meaning | Values |
| --- | --- | --- |
| `alias` | The spelling you'll type as `symbol` in a transaction row, or that the Form/pull path produces. | Free text |
| `source` | Where this alias came from. `manual` for anything you typed by hand or via `folios add`; other values are written automatically by adapters (e.g. a future `folios pull` source). | `manual`, or an adapter-specific value |
| `instrument_id` | Which instrument this resolves to. Must exist in `config/instruments.csv`. | Must match an `instrument_id` |

## config/exposure_mapping.csv

Translates a fund provider's own labels (as published by justETF, an
issuer factsheet, etc.) into your `dimensions.csv` codes, for
look-through allocation. Optional file — a missing file just means no
look-through mapping happens yet.

| Column | Meaning | Values |
| --- | --- | --- |
| `dimension` | Which dimension this maps into. | e.g. `sector`, `country` |
| `source_label` | The label exactly as the provider publishes it. | Free text, e.g. `Technology`, `United States` |
| `code` | The `dimensions.csv` code it maps to. Use `UNMAPPED` for anything you don't want to bother classifying yet — it stays visible as its own slice rather than silently vanishing. | Must match a `dimensions.csv` code, or `UNMAPPED` |
