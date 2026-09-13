# google-ads-write-mcp

A small, allow-listed **write-side MCP server for Google Ads**, plus a read-only
Keyword Planner tool. It is the companion to Google's official
[`google-ads-mcp`](https://github.com/googleads/google-ads-mcp), which is
read-only by design: use that one to look things up, and this one to change them.

Works with Claude Code (and any MCP client over stdio).

## Safety model

- **Dry run by default.** Every mutating tool sends its request with
  `validate_only=true` — Google checks auth and payload and changes nothing.
  Pass `confirm=true` to apply.
- **Atomic.** Each call is one `MutateGoogleAdsRequest` with partial failure
  off: either everything in it is created, or nothing is.
- **Additive.** Tools add ads, keywords, assets, ad groups and campaigns, or
  toggle ENABLED/PAUSED, or tune budget and target CPA. Nothing edits existing
  ad text. New campaigns are created PAUSED.
- **One guarded delete.** `remove_entity` refuses anything that does not sit
  under a campaign carrying the managed label (default `claude-managed`, which
  `create_search_campaign` attaches to every campaign it creates), and verifies
  that with a read query first. Removal in Google Ads is permanent; there is no
  restore.
- **Audited.** Every call — dry run or applied, success or rejection — is
  appended to a local JSONL audit log with timestamp, tool, customer, payload
  and API result. No secrets are written.
- **Local limits.** RSA headline/description counts and lengths, sitelink,
  callout and snippet lengths, image size and aspect ratio are checked before
  anything is sent.

## Tools

| Tool | Purpose |
|---|---|
| `create_search_campaign` | Build a complete Search campaign in one atomic mutate: budget, campaign, location and language criteria, campaign negatives, the managed label, every ad group with its keywords, optional ad-group negatives, optional final URL suffix (UTMs) and one responsive search ad. Created PAUSED; Google Search only; Maximize conversions (optional target CPA) or Maximize clicks |
| `add_ad_group` | Add one ad group with keywords, optional negatives, optional final URL suffix and one RSA to an existing campaign |
| `create_responsive_search_ad` | Add a new RSA to an ad group (existing ads untouched) |
| `add_keywords` | Add positive keywords to an ad group |
| `add_negative_keywords` | Add negatives at campaign or ad-group level |
| `add_sitelinks` / `add_callouts` / `add_structured_snippet` | Create the assets and attach them to a campaign; `add_sitelinks` can attach to one ad group instead (`ad_group_id`) |
| `upload_image_asset` | Put one image into the asset library (no link): `image_url` (https, public hosts, redirects refused, 5 MB) or `image_base64` for small files; returns the asset resource name to link with `add_image_assets` |
| `set_campaign_conversion_goals` | Campaign-specific conversion goals: toggle `biddable` per (category, origin) pair — e.g. bid only on PURCHASE/WEBSITE by setting it true and every other account goal false; one atomic request |
| `add_image_assets` | Upload PNG/JPEG files and attach them to a Search campaign (`AD_IMAGE`) — or to one ad group via `ad_group_id` (up to 20 images per campaign and per ad group); validates 1:1 / 1.91:1 shape, minimum size and 5 MB cap |
| `add_business_assets` | Attach a business name (TEXT asset, `BUSINESS_NAME`) and/or business logo (`BUSINESS_LOGO`, square >=128x128, uploaded or re-linked by asset resource name) to a Search campaign |
| `set_status` | ENABLED / PAUSED on a campaign, ad group, ad or keyword |
| `set_campaign_daily_budget` | Change a campaign's non-shared daily budget |
| `set_campaign_target_cpa` | Set tCPA on a Maximize-conversions campaign |
| `create_portfolio_bidding_strategy` | Create a portfolio (shared) Maximize Conversions bid strategy, optionally with a target CPA, and attach campaigns to it in the same atomic request — pools Smart Bidding's learning across campaigns |
| `set_campaign_bidding_strategy` | Attach campaigns to an existing portfolio bid strategy, or (no `bidding_strategy_id`) detach them back to campaign-level Maximize Conversions without a target |
| `set_bidding_strategy_target_cpa` | Change (or remove with 0) the target CPA of a portfolio strategy — Maximize Conversions or legacy Target CPA |
| `create_shared_budget` | Create an explicitly shared daily budget and move campaigns onto it atomically, so spend flows between them; old individual budgets are left unused |
| `set_shared_budget_amount` | Change the daily amount of an explicitly shared budget (`set_campaign_daily_budget` refuses those) |
| `set_campaign_cpc_ceiling` | Set (or remove with 0) the max-CPC ceiling on a Maximize-Clicks campaign (`target_spend.cpc_bid_ceiling_micros`) |
| `set_final_urls` | Update final URLs (ads, assets) and final URL suffixes (ad groups, ads) in one atomic mutate — e.g. move UTMs into the final URL itself |
| `update_responsive_search_ad` | Edit an existing RSA in place (same ad id and history; re-review, asset labels reset): replace headlines/descriptions, paths, final URL. Any argument left out is unchanged |
| `set_campaign_final_url_suffix` | Campaign-level final URL suffix (ValueTrack, e.g. `utm_term={keyword}&kw_match={matchtype}&device={device}`), `""` clears; ad-group/ad suffixes override it |
| `set_device_bid_modifiers` | Campaign-level device bid adjustments in percent; −100 excludes a device (mobile-only = desktop −100, tablet −100). Call it after `create_search_campaign` with the returned campaign id |
| `set_location_bid_modifiers` | Campaign-level location bid adjustments in percent (−90 to +900) on locations the campaign already targets, keyed by geo target constant id (`{"2840": 30, "2344": -30}`); 0 removes one. Honoured by Maximize clicks / Manual CPC, ignored by Smart Bidding. Never changes targeting |
| `add_campaign_audiences` | Attach in-market / affinity segments (`user_interest` ids) to a Search campaign in OBSERVATION mode: sets the AUDIENCE targeting dimension to bid-only (never narrows who sees the ads) and creates one campaign criterion per segment, optionally with a bid adjustment (−90 to +900). Already-present segments are skipped |
| `remove_entity` | Permanently remove a campaign, ad group, ad, keyword, campaign criterion (e.g. a campaign negative) or an asset link (campaign/ad-group asset) — one resource name or a list of the same kind, atomically — only under a campaign carrying the managed label |
| `keyword_ideas` | Keyword Planner ideas with monthly volume and bid ranges (read-only, 1 request/second) |
| `audit_log_tail` | Last N audit-log entries |
| `create_experiment` | SEARCH_CUSTOM A/B experiment on an existing Search campaign: experiment shell plus control/treatment arms with a traffic split; returns the treatment's draft campaign to edit with the other tools. Dry run validates the shell only — arms need the real experiment, so they are created on confirm |
| `schedule_experiment` | Start a SETUP experiment serving (materializes the treatment draft; asynchronous on Google's side) |
| `end_experiment` | End a running experiment; the base campaign resumes full traffic |
| `promote_experiment` | Apply the treatment to the base campaign — the winner becomes the live campaign (asynchronous) |

Customer ids may contain dashes. Use the read-only MCP first to look up
campaign and ad-group ids and resource names.

## Prerequisites

You need the same three things the official read-only server needs — this
package creates none of them:

1. A Google Ads API **developer token** (from a manager account's API Center).
2. An OAuth client in a Google Cloud project, and an **Ads ADC file** produced by
   `gcloud auth application-default login --scopes=https://www.googleapis.com/auth/adwords`
   (an `authorized_user` JSON with client id, client secret and refresh token).
3. **Edit access** on the Google Ads accounts you intend to change. Read-only
   users can query but every mutate is rejected.

Keep the token and the ADC file out of any repository.

## Install

```bash
pipx install "git+https://github.com/Luxand/google-ads-write-mcp@v0.5.2"
google-ads-write-mcp --check        # shows which credential files it found; no secrets printed
google-ads-write-mcp --list-tools
```

The install is pinned to a tag, so `pipx upgrade` will not move it; upgrade
with `pipx install --force "git+https://github.com/Luxand/google-ads-write-mcp@<new tag>"`.

## Configuration

Everything is optional if the defaults fit:

| Variable | Meaning | Default |
|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | the token itself | — |
| `GOOGLE_ADS_DEVELOPER_TOKEN_FILE` | file holding the token | `~/.config/google-ads-mcp/developer-token` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Ads ADC JSON | `~/.config/google-ads-mcp/gcloud/application_default_credentials.json` |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | manager (MCC) id when the target account sits under one; `GOOGLE_ADS_MCP_LOGIN_CUSTOMER_ID` is accepted as an alias | none |
| `GOOGLE_ADS_WRITE_AUDIT_LOG` | audit log path | `~/.local/share/google-ads-write-mcp/audit.jsonl` |
| `GOOGLE_ADS_WRITE_MANAGED_LABEL` | Google Ads label that marks campaigns `remove_entity` may touch; created in the account on first use | `claude-managed` |

A login customer id applies to the whole client, so register one server entry
per manager context you need.

## Register in Claude Code

```bash
# direct accounts
claude mcp add --scope user google-ads-write -- "$(command -v google-ads-write-mcp)"

# accounts under a manager
claude mcp add --scope user google-ads-write-luxand \
  --env GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890 \
  -- "$(command -v google-ads-write-mcp)"
```

Then in Claude Code, `/mcp` should list the server and its tools. A safe first
call is `audit_log_tail`, then any tool without `confirm` (a dry run).

## Scripted use

`--call TOOL ARGS.json` runs one tool from a JSON file through the same code
path, same dry-run default and same audit log:

```bash
google-ads-write-mcp --call create_search_campaign /abs/path/campaign.json
```

Useful for pushing a large campaign generated from a spreadsheet, where passing
hundreds of keywords as chat tool arguments is impractical. Add `"confirm": true`
to the JSON to apply.

## Things the API taught us

- Search campaigns accept image assets only as `AD_IMAGE`;
  `MARKETING_IMAGE` / `SQUARE_MARKETING_IMAGE` are Performance Max and Display
  field types and are rejected with `FIELD_TYPE_INCOMPATIBLE_WITH_CAMPAIGN_TYPE`.
- `contains_eu_political_advertising` is required on campaign creation in
  current API versions; this server sets it to "does not contain".
- Geo and language criteria are plain resource names
  (`geoTargetConstants/2840`, `languageConstants/1000`).
- `GenerateKeywordIdeas` is limited to 1 request per second per customer id and
  returns `RESOURCE_EXHAUSTED` above that; `keyword_ideas` spaces and retries.
- A mutate request may hold at most 10,000 operations; a 20-ad-group Search
  campaign with ~250 keywords is about 370.
- Device criteria (desktop 30000, mobile 30001, tablet 30002) exist implicitly
  on every campaign; set a bid modifier by *updating*
  `campaignCriteria/<campaign>~<id>`. Name `bid_modifier` in the update mask
  explicitly: a field mask built by comparing against defaults drops a 0.0
  (−100%) value and the request succeeds while changing nothing.

- Setting a campaign back to campaign-level Maximize Conversions means assigning
  an *empty* `MaximizeConversions` message to the `campaign_bidding_strategy`
  oneof and naming `maximize_conversions` in the update mask explicitly; a
  generated mask sees no set fields and sends nothing.

## License

MIT.
