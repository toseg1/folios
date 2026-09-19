# config/ field reference

Every field in every `config/` file, what it means, and its possible
values. Source of truth for all of this is the schema itself
(`migrations/001_schemas_and_core.sql` .. the latest) and the seeder
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

Some fields below are marked **auto** — populated from yfinance/justETF
at instrument-creation time (via `new_instrument.YFinanceInfoProvider`
for EQUITY, mapped through `config/exposure_mapping.csv`) rather than
typed by hand. They're still ordinary CSV columns and can always be
overridden manually.

## config/dimensions.csv

One row per (dimension, code) pair — the controlled vocabulary every
dimension-valued field below draws from.

| Column | Meaning |
| --- | --- |
| `dimension` | Which field this code belongs to, e.g. `fiscal_envelope`, `sector`. Always matches a field name from the tables below. |
| `code` | The value itself, e.g. `CTO`, `TECHNOLOGY`. What you actually type into `accounts.yml` / `instruments.csv`. |
| `label_en` | Display label. |
| `sort_order` | Integer; controls display order in Metabase, not meaning. |

## config/accounts.yml

One list of mappings, one per account. Required fields have no
default; everything else is optional and defaults as noted.

| Field | Required | Meaning | Values |
| --- | --- | --- | --- |
| `account_id` | **yes** | Stable short code you reference everywhere (transactions, the Form dropdown). Primary key — never reuse one for a different account. | Free text, your convention (e.g. `BOURSOBANK-CTO`) |
| `broker` | **yes** | The brand/app you actually use day to day. | Free text |
| `ultimate_parent` | no | The legal group actually bearing the risk — the sense used in your own reporting (e.g. `Boursorama SA` behind the BoursoBank brand). Used by `v_custody_exposure` to catch concentration risk: several "different" brokers backed by the same group show up as one slice. If unknown, repeat `broker`. | Free text |
| `custodian` | no | **Custody layer 1b**, the second custody layer: who actually holds it day to day, when that's a different entity from `ultimate_parent`. Examples: `custody_type = BROKER` — Trade Republic's custodian is CACEIS; `custody_type = SELF_CUSTODY` — the wallet app, e.g. MetaMask; `custody_type = INSURER` — the underwriter behind an AV contract, e.g. Generali. Not restricted to those three — usable for any account. | Free text |
| `account_type` | **yes** | *Dimension.* Broad structural category: does this account hold securities, or just money? | `securities`, `cash`, `crypto`|
| `fiscal_envelope` | no | *Dimension.* The tax/regulatory wrapper this specific account is. One account = one envelope, for life — it's a property of the account, not the transaction. | `PEA`, `PEA_PME`, `CTO`, `AV`, `PER`, `LIVRET_A`, `LDDS`, `LEP`, `PEL`, `CAT`, `CURRENT` |
| `custody_type` | no | *Dimension.* **Custody layer 1**: who holds your units, and what happens if they fail. `BANK` = you're a creditor of the bank (protected by `guarantee_scheme` up to a ceiling). `BROKER` = securities held in custody on your behalf (see `holding_structure` for how cleanly segregated). `INSURER` = inside an `AV`/`PER` insurance wrapper. `EXCHANGE` = held by a crypto exchange (still their balance sheet). `SELF_CUSTODY` = you hold the keys, no institution involved. | `BROKER`, `BANK`, `INSURER`, `EXCHANGE`, `SELF_CUSTODY`, `DIRECT_REGISTRAR` |
| `holding_structure` | no, defaults `UNKNOWN` | Documented, not dimension-checked. **Custody layer 1c**, refines `BROKER`/`EXCHANGE`: exactly how segregated your units are, i.e. what's left of them if the intermediary goes bankrupt. `DIRECT_REGISTERED` (nominatif pur — registered directly in your name at the issuer/registrar, the cleanest case) : `NOMINEE_SEGREGATED` (held in the broker's name but in a segregated sub-account, legally yours) : `OMNIBUS` (pooled with other clients' assets, harder to trace back to you specifically) : `UNKNOWN`. | `DIRECT_REGISTERED`, `NOMINEE_SEGREGATED`, `OMNIBUS`, `UNKNOWN` |
| `key_storage` | no | Documented, not dimension-checked. **Custody layer 2b**: how your crypto keys are actually stored. Only meaningful — and only allowed by a database CHECK — when `custody_type = SELF_CUSTODY`; you cannot answer this about an exchange-held balance. | `COLD_HARDWARE`, `COLD_PAPER`, `MULTISIG`, `MPC`, `HOT_SOFTWARE` |
| `contractual_rate` | no | The locked-in interest rate, for products where the rate is a property of *this* account (`PEL`, `CAT`). Never use this for administered rates like Livret A/LDDS — those change by government decree and aren't a property of your account. | Decimal, e.g. `0.0175` for 1.75% |
| `maturity_date` | no | When a locked product becomes available (vs. protected-but-available). | ISO date `YYYY-MM-DD` |
| `deposit_ceiling` | no | The regulatory cap for this envelope, used to show remaining headroom (Livret A: 22950, LDDS: 12000). | Decimal amount |
| `guarantee_scheme` | no | Which deposit/investor protection scheme covers this account. | `FGDR`, `FGAP`, `NONE` |
| `base_currency` | **yes** | The currency this account's balance/statements are denominated in. | ISO 4217 code, e.g. `EUR`, `USD` |
| `opened_on` | no | When you opened the account. Approximate is fine. | ISO date |
| `closed_on` | no | When you closed it, if you have. | ISO date |
| `is_active` | no, defaults `true` | Whether the account is still in use. | `true` / `false` |

**Assurance vie / life insurance**: an AV contract is just one account
(`fiscal_envelope = AV`). What's inside it — the *fonds euros*
compartment, each unité de compte sub-fund — is modeled as ordinary
`config/instruments.csv` rows (fonds euros is typically
`asset_class = FUND`, `instrument_type = OTHER`, `price_source = manual`
since it has no market price) held via ordinary transactions in that
one account. `marts.v_av_positions` rolls an AV contract's holdings back
up by instrument/asset_class.

## config/instruments.csv

One row per instrument (a share, an ETF, a bond, a crypto token, …).
Equities need no subtype row — everything they need is here. Funds,
bonds and crypto also need a matching row in the subtype file below,
keyed on the same `instrument_id`.

| Column | Required | Meaning | Values |
| --- | --- | --- | --- |
| `instrument_id` | **yes** | Stable short code, your convention. Primary key. | Free text |
| `isin` | no | ISIN, if it has one (crypto and some private funds don't). Unique if set. | 12-char ISIN |
| `yf_symbol` | no | The exact string yfinance needs to pull prices when `price_source = yfinance` — often has an exchange suffix outside the US, or a different shape for crypto. Never derived from `ticker`; type it exactly as yfinance expects. | Free text, e.g. `AAPL`, `DEMW.PA`, `BTC-EUR` |
| `ticker` | no | The plain ticker as quoted on its exchange, for display — distinct from `yf_symbol`. | Free text, e.g. `AAPL`, `DEMW` |
| `name` | **yes** | Display name. | Free text |
| `asset_class` | **yes** | *Dimension.* The broad category — also decides whether a subtype row is expected (`ETP`/`FUND` → `instruments_fund.csv`, `BOND` → `instruments_bond.csv`, `CRYPTO` → `instruments_crypto.csv`). Deliberately no `CASH`: cash is derived from transaction sums, never a held instrument. Commodity and Structured Product are out of scope for now. | `EQUITY`, `ETP`, `FUND`, `BOND`, `CRYPTO` |
| `instrument_type` | no | *Dimension.* The level-2 breakdown within `asset_class` — `EQUITY` → `SHARE`; `BOND` → `BOND`; `FUND` → `UCITS` / `AIF` / `OTHER`; `ETP` → `ETF` / `ETC` / `ETN`; `CRYPTO` → `COIN` / `RWA` (a tokenized real-world asset is a crypto instrument_type, not its own asset_class — it still gets an `instruments_crypto.csv` row, since it lives on a chain like any other token). See `legal_structure` in the fund subtype table for what an ETP/FUND actually *is* underneath — an ETN is not a fund and owns nothing. | `SHARE`, `BOND`, `UCITS`, `AIF`, `OTHER`, `ETF`, `ETC`, `ETN`, `COIN`, `RWA` |
| `currency` | **yes** | The instrument's trading/quote currency. | ISO 4217 code |
| `sector` | no | *Dimension.* **Auto** for EQUITY (yfinance `.info['sector']`) and exchange-traded ETP/FUND (justETF), mapped through `config/exposure_mapping.csv`. Manual, no source for a non-listed FUND/BOND/CRYPTO. Left blank rather than seeded with an unmapped code if the fetched label has no mapping row yet — that shows up as a warning, add the row and re-run. | e.g. `TECHNOLOGY`, `FINANCE`, `DIVERSIFIED` — extend as needed |
| `industry` | no | *Dimension.* Same automation/mapping story as `sector`, one level more specific (yfinance `.info['industry']`). | e.g. `SOFTWARE`, `SEMICONDUCTORS` — extend as needed |
| `description` | no | Free text, not a dimension. **Auto** for EQUITY (`.info['longBusinessSummary']`) and exchange-traded ETP/FUND (justETF's own description) — a cached paragraph, no mapping involved. | Free text |
| `issuer` | no | Who issues/manages it. For an ETP/FUND this means the fund provider/management company (e.g. iShares) — distinct from `instruments_fund.csv`'s own `management_company`, which stays SCPI-specific. | Free text |
| `domicile_country` | no | *Dimension.* Where the instrument is domiciled. **Auto** for EQUITY/exchange-traded ETP/FUND, same mapping story as `sector`. | ISO 3166-1 alpha-2, e.g. `IE`, `FR` — as a `country` dimension code |
| `protection_type` | no | *Dimension.* | `GUARANTEED`, `MARKET_EXPOSED`, `PARTIALLY_PROTECTED`, `UNKNOWN` |
| `is_pea_eligible` | no | Whether it can be held in a French PEA. `NULL`/blank means unknown, shown as such rather than guessed. | `true` / `false` / blank |
| `price_source` | no, defaults `yfinance` | Where `folios prices` pulls its close price from. Database-enforced, not just a dimension. | `yfinance`, `manual` |
| `is_active` | no, defaults `true` | | `true` / `false` |

## config/instruments_fund.csv

One row per instrument with `asset_class` in `ETP`/`FUND`, keyed on
`instrument_id`.

| Column | Meaning | Values |
| --- | --- | --- |
| `legal_structure` | **The field that decides everything downstream** — what you actually own if the issuer fails. Manual — structurally load-bearing, so not auto-derived even where justETF could suggest a value. | `UCITS_FUND` (ETF/OPCVM/SICAV — ring-fenced assets, a custodian, real holdings, look-through applies) : `NON_UCITS_FUND` (a fund, outside UCITS protection) : `COLLATERALISED_NOTE` (most ETCs — debt backed by collateral, not a fund) : `UNSECURED_NOTE` (ETNs — unsecured debt of the issuer, no ring-fenced assets, no custodian; if the issuer fails you're a creditor, not an owner) : `SCPI` (French property vehicle) |
| `is_ucits` | Whether it's UCITS-compliant. | `true` / `false` |
| `rhp_years` | Recommended holding period, from the PRIIPs KID. Manual, no automatable source. | Integer years |
| `distribution_policy` | Whether income is paid out or reinvested. | `ACCUMULATING`, `DISTRIBUTING` |
| `ongoing_charges` | Annual cost (TER), **as a decimal fraction** — 0.20% is `0.0020`, not `0.20`. | Decimal fraction |
| `sri` | PRIIPs KID risk indicator, stored exactly as published. Ordinal 1–7 — never average this, only distribute it. Manual, no automatable source (KID-only figure). | Integer 1–7 |
| `replication_method` | How the fund tracks its index. | `PHYSICAL_FULL`, `PHYSICAL_SAMPLED`, `SYNTHETIC` |
| `swap_counterparty` | Only for synthetic replication. Manual, no confirmed source. | Free text |
| `uses_sec_lending` | Whether the fund lends out its holdings. Manual, no confirmed source. | `true` / `false` |
| `custodian` | **Custody layer 2a**: who holds the *fund's* underlying assets (as opposed to `accounts.custodian`, which is who holds *your units of the fund*). `NULL` required if `legal_structure = UNSECURED_NOTE` — a note has no fund assets, so it can't have a custodian. | Free text |
| `sfdr_article` | EU sustainability disclosure classification. Manual — justETF shows it on-site but doesn't expose it via the library used here. | `6`, `8`, `9` |
| `benchmark_index` | The index it tracks — used for overlap detection without full look-through. | Free text |
| `justetf_id` | justETF's own identifier, if pulling exposure data from there. Usually identical to `isin`. | Free text |
| `subscription_price` / `withdrawal_price` | SCPI only — the two prices SCPIs quote instead of a market price; value the position at `withdrawal_price`. Manual, no automatable source. | Decimal |
| `management_company` | SCPI only. | Free text |
| `property_sector` | SCPI only. | Free text |
| `occupancy_rate` | SCPI only, decimal fraction. | Decimal fraction |
| `distribution_rate` | SCPI only, decimal fraction. | Decimal fraction |
| `investment_focus` | justETF's own free-text description of the fund's focus. | Free text |
| `fund_size` | Assets under management. | Decimal |
| `strategy_risk` | justETF's strategy-risk label. | Free text |
| `sustainability` | justETF's own sustainability label — distinct from `sfdr_article`. | Free text |
| `currency_risk` | justETF's currency-risk label. | Free text |
| `volatility_1y_eur` | Trailing 1-year volatility, in EUR, as published. | Decimal fraction |
| `inception_date` | Fund inception/listing date. | ISO date |
| `distribution_frequency` | How often a distributing fund actually pays out — distinct from `distribution_policy` (whether it pays out at all). | `ANNUAL`, `SEMI_ANNUAL`, `QUARTERLY`, `MONTHLY` |

`fund_currency`/`fund_domicile`/`fund_provider` from justETF are
deliberately not separate columns here — they're the same thing as
`core.instruments.currency`/`domicile_country`/`issuer` for an
ETP/FUND row.

## config/instruments_bond.csv

One row per instrument with `asset_class = BOND`, keyed on
`instrument_id`. Nothing here is automatable — a directly-held bond
isn't on justETF or yfinance's structured data.

| Column | Meaning | Values |
| --- | --- | --- |
| `coupon_rate` | Annual coupon ("interest rate"), as a decimal fraction. | Decimal fraction |
| `coupon_frequency` | How often the coupon pays ("payment frequency"). | `ANNUAL`, `SEMI_ANNUAL`, `QUARTERLY`, `ZERO` |
| `maturity_date` | | ISO date |
| `face_value` | Redemption value per unit. Optional extra, not always filled in. | Decimal |
| `issuer_type` | Who issued it. Optional extra. | `SOVEREIGN`, `CORPORATE`, `FINANCIAL`, `SUPRANATIONAL` |
| `credit_rating` | As published by the rating agency. Optional extra. | Free text, e.g. `AA`, `BBB+` |
| `seniority` | Where it ranks in a default. Optional extra. | `SENIOR_SECURED`, `SENIOR_UNSECURED`, `SUBORDINATED` |
| `is_callable` | Whether the issuer can redeem it early. Defaults `false`. Optional extra. | `true` / `false` |

The issuer itself lives on `core.instruments.issuer`, shared with
every other asset class, not duplicated here.

## config/instruments_crypto.csv

One row per instrument with `asset_class = CRYPTO`, keyed on
`instrument_id`. Also used for `instrument_type = RWA` (a tokenized
real-world asset) — it's a crypto instrument_type, not its own asset
class, since it lives on a chain like any other token.

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

Translates a provider's own raw labels into your `dimensions.csv`
codes. Two uses now: ETP look-through ingestion (`core.etp_exposure`,
via `folios exposure --refresh`) and direct instrument-level
`sector`/`industry`/`domicile_country` population at creation time (see
`new_instrument.py`). Optional file — a missing file just means no
mapping happens yet, and fetched labels are left unmapped (visible as a
warning for a direct instrument, or as an `UNMAPPED:<label>` row for
look-through exposure — the two paths use different fallbacks, since
`core.instruments` columns are dimension-checked and `core.etp_exposure`
isn't).

| Column | Meaning | Values |
| --- | --- | --- |
| `dimension` | Which dimension this maps into. | e.g. `sector`, `industry`, `country` |
| `source_label` | The label exactly as the provider publishes it. | Free text, e.g. `Technology`, `United States` |
| `code` | The `dimensions.csv` code it maps to. For look-through ingestion only, `UNMAPPED` marks anything you don't want to bother classifying yet — it stays visible as its own slice rather than silently vanishing. | Must match a `dimensions.csv` code, or (look-through only) `UNMAPPED` |
