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
| `add_sitelinks` / `add_callouts` / `add_structured_snippet` | Create the assets and attach them to a campaign |
| `add_image_assets` | Upload PNG/JPEG files and attach them to a Search campaign (`AD_IMAGE`); validates 1:1 / 1.91:1 shape, minimum size and 5 MB cap |
| `set_status` | ENABLED / PAUSED on a campaign, ad group, ad or keyword |
| `set_campaign_daily_budget` | Change a campaign's non-shared daily budget |
| `set_campaign_target_cpa` | Set tCPA on a Maximize-conversions campaign |
| `remove_entity` | Permanently remove a campaign, ad group, ad, keyword or campaign criterion (e.g. a campaign negative) — one resource name or a list of the same kind, atomically — only under a campaign carrying the managed label |
| `keyword_ideas` | Keyword Planner ideas with monthly volume and bid ranges (read-only, 1 request/second) |
| `audit_log_tail` | Last N audit-log entries |

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
pipx install "git+https://github.com/Luxand/google-ads-write-mcp@v0.2.1"
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

## License

MIT.
