"""google-ads-write-mcp — a small, allow-listed WRITE-side MCP server for Google Ads,
plus a read-only Keyword Planner tool.

Safety model
------------
* Every tool is a DRY RUN by default: the request is sent with ``validate_only=True``;
  Google validates auth + payload and changes nothing.  Pass ``confirm=true`` to apply.
* Additive and status changes are exposed freely.  The one destructive tool,
  ``remove_entity``, only touches entities under campaigns carrying the managed label
  (``MANAGED_LABEL``, default "claude-managed"), which ``create_search_campaign`` attaches
  to every campaign it creates.  Removal in Google Ads is permanent; there is no restore.
* Every call (dry run or applied) is appended to an audit log (JSONL), without secrets.
* Credentials come from the same files the official read-only Google Ads MCP uses
  (developer-token file + Ads ADC ``authorized_user`` JSON). ``main()`` resolves them from
  environment variables or their default locations; nothing is stored by this package.

Configuration (environment variables, all optional):
  GOOGLE_ADS_DEVELOPER_TOKEN        the token itself, or
  GOOGLE_ADS_DEVELOPER_TOKEN_FILE   path to a file holding it
                                    (default ~/.config/google-ads-mcp/developer-token)
  GOOGLE_APPLICATION_CREDENTIALS    Ads ADC JSON
                                    (default ~/.config/google-ads-mcp/gcloud/application_default_credentials.json)
  GOOGLE_ADS_LOGIN_CUSTOMER_ID      manager (MCC) id when the target account is under one
                                    (GOOGLE_ADS_MCP_LOGIN_CUSTOMER_ID is accepted as an alias)
  GOOGLE_ADS_WRITE_AUDIT_LOG        audit log path
                                    (default ~/.local/share/google-ads-write-mcp/audit.jsonl)
  GOOGLE_ADS_WRITE_MANAGED_LABEL    Google Ads label that marks campaigns this server created and
                                    may remove (default "claude-managed")
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Union

from google.api_core import exceptions as gexc
from google.api_core import protobuf_helpers
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.oauth2.credentials import Credentials
from mcp.server.fastmcp import FastMCP

DEFAULT_TOKEN_FILE = "~/.config/google-ads-mcp/developer-token"
DEFAULT_ADC_FILE = "~/.config/google-ads-mcp/gcloud/application_default_credentials.json"
DEFAULT_AUDIT_LOG = "~/.local/share/google-ads-write-mcp/audit.jsonl"

AUDIT_LOG = os.path.expanduser(os.environ.get("GOOGLE_ADS_WRITE_AUDIT_LOG", DEFAULT_AUDIT_LOG))

# Campaigns created by this server carry this label. remove_entity refuses
# anything that does not sit under such a campaign.
MANAGED_LABEL = os.environ.get("GOOGLE_ADS_WRITE_MANAGED_LABEL", "claude-managed")


def _configure() -> Dict[str, str]:
    """Resolve credentials from env/defaults into the env vars the client reads.
    Returns a secret-free summary for --check."""
    token_file = os.path.expanduser(os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    if not os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
        if os.path.isfile(token_file):
            with open(token_file) as fh:
                os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"] = fh.readline().strip()
    adc = os.path.expanduser(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", DEFAULT_ADC_FILE))
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = adc
    if not os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID") and os.environ.get("GOOGLE_ADS_MCP_LOGIN_CUSTOMER_ID"):
        os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = os.environ["GOOGLE_ADS_MCP_LOGIN_CUSTOMER_ID"]
    return {
        "developer_token": "set" if os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN") else f"MISSING (looked in {token_file})",
        "ads_adc": adc if os.path.isfile(adc) else f"MISSING ({adc})",
        "login_customer_id": os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or "(none: direct accounts)",
        "audit_log": AUDIT_LOG,
        "managed_label": MANAGED_LABEL,
    }

mcp = FastMCP(
    "google-ads-write",
    instructions=(
        "Write-side companion to the read-only Google Ads MCP. Every tool is a DRY RUN "
        "(validate_only) unless confirm=true. Use the read MCP (search) to look up ids / "
        "resource names first. The only delete tool (remove_entity) is restricted to campaigns "
        f"carrying the {MANAGED_LABEL!r} label (attached by create_search_campaign), and removal is "
        "permanent. keyword_ideas is read-only Keyword Planner data. Customer ids may contain dashes."
    ),
)

# --------------------------------------------------------------------------- helpers
_client: Optional[GoogleAdsClient] = None


def _get_client() -> GoogleAdsClient:
    global _client
    if _client is None:
        adc_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
        dev_token = os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"]
        login_cid = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or None
        with open(adc_path) as fh:
            adc = json.load(fh)
        creds = Credentials(
            None,
            refresh_token=adc["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=adc["client_id"],
            client_secret=adc["client_secret"],
            scopes=["https://www.googleapis.com/auth/adwords"],
        )
        _client = GoogleAdsClient(
            credentials=creds,
            developer_token=dev_token,
            login_customer_id=login_cid,
            use_proto_plus=True,
        )
    return _client


def _cid(customer_id: Union[str, int]) -> str:
    return str(customer_id).replace("-", "").strip()


def _audit(tool: str, customer_id: str, payload: Dict[str, Any], result: Dict[str, Any], applied: bool) -> None:
    rec = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "customer_id": customer_id,
        "login_customer_id": os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or None,
        "applied": applied,
        "payload": payload,
        "result": result,
    }
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _fail(tool: str, customer_id: str, payload: Dict[str, Any], msg: str) -> Dict[str, Any]:
    out = {"status": "INVALID_INPUT", "error": msg}
    _audit(tool, customer_id, payload, out, applied=False)
    return out


def _run_mutate(tool: str, customer_id: str, operations: list, confirm: bool, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Send one atomic MutateGoogleAdsRequest. Dry run unless confirm."""
    c = _get_client()
    svc = c.get_service("GoogleAdsService")
    req = c.get_type("MutateGoogleAdsRequest")
    req.customer_id = customer_id
    req.mutate_operations.extend(operations)
    req.validate_only = not confirm
    req.partial_failure = False
    try:
        resp = svc.mutate(request=req)
    except GoogleAdsException as exc:
        errs = []
        for err in exc.failure.errors:
            errs.append(
                {
                    "code": str(err.error_code).strip().replace("\n", " "),
                    "message": err.message,
                    "field_path": ".".join(fe.field_name for fe in err.location.field_path_elements),
                }
            )
        out = {"status": "REJECTED_BY_API", "dry_run": not confirm, "errors": errs, "request_id": exc.request_id}
        _audit(tool, customer_id, payload, out, applied=False)
        return out
    names: List[str] = []
    for r in resp.mutate_operation_responses:
        which = r._pb.WhichOneof("response")
        if which:
            sub = getattr(r, which)
            rn = getattr(sub, "resource_name", "")
            if rn:
                names.append(rn)
    out: Dict[str, Any] = {
        "status": "APPLIED" if confirm else "VALIDATED_DRY_RUN",
        "dry_run": not confirm,
        "operations": len(operations),
        "resource_names": names,
    }
    if not confirm:
        out["note"] = "Nothing was changed. Google validated the request. Re-run with confirm=true to apply."
    _audit(tool, customer_id, payload, out, applied=confirm)
    return out


def _len_check(items: List[str], max_len: int, label: str) -> Optional[str]:
    for t in items:
        if len(t) > max_len:
            return f"{label} too long ({len(t)} > {max_len}): {t!r}"
        if not t.strip():
            return f"{label} is empty"
    return None


_PIN = {
    None: None, "": None,
    "HEADLINE_1": "HEADLINE_1", "HEADLINE_2": "HEADLINE_2", "HEADLINE_3": "HEADLINE_3",
    "DESCRIPTION_1": "DESCRIPTION_1", "DESCRIPTION_2": "DESCRIPTION_2",
    "H1": "HEADLINE_1", "H2": "HEADLINE_2", "H3": "HEADLINE_3", "D1": "DESCRIPTION_1", "D2": "DESCRIPTION_2",
}


def _text_assets(c: GoogleAdsClient, items: List[Union[str, Dict[str, str]]]):
    """items: ["text", ...] or [{"text": "...", "pin": "HEADLINE_1"}, ...]"""
    out = []
    for it in items:
        if isinstance(it, str):
            text, pin = it, None
        else:
            text, pin = it.get("text", ""), it.get("pin") or it.get("pinned_field")
        if pin not in _PIN:
            raise ValueError(f"unknown pin {pin!r}; use HEADLINE_1/2/3 or DESCRIPTION_1/2")
        a = c.get_type("AdTextAsset")
        a.text = text
        if _PIN[pin]:
            a.pinned_field = getattr(c.enums.ServedAssetFieldTypeEnum, _PIN[pin])
        out.append(a)
    return out


# --------------------------------------------------------------------------- tools
@mcp.tool()
def create_responsive_search_ad(
    customer_id: str,
    ad_group_id: str,
    final_url: str,
    headlines: List[Union[str, Dict[str, str]]],
    descriptions: List[Union[str, Dict[str, str]]],
    path1: str = "",
    path2: str = "",
    status: str = "ENABLED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Create a NEW responsive search ad in an ad group (existing ads are untouched).

    headlines: 3-15 items, each <=30 chars; descriptions: 2-4 items, each <=90 chars.
    An item may be a string or {"text": "...", "pin": "HEADLINE_1|HEADLINE_2|HEADLINE_3|DESCRIPTION_1|DESCRIPTION_2"}.
    status: ENABLED or PAUSED. Dry run unless confirm=true.
    """
    cid = _cid(customer_id)
    payload = dict(ad_group_id=ad_group_id, final_url=final_url, headlines=headlines, descriptions=descriptions,
                   path1=path1, path2=path2, status=status)
    tool = "create_responsive_search_ad"
    htexts = [h if isinstance(h, str) else h.get("text", "") for h in headlines]
    dtexts = [d if isinstance(d, str) else d.get("text", "") for d in descriptions]
    if not (3 <= len(htexts) <= 15):
        return _fail(tool, cid, payload, f"need 3-15 headlines, got {len(htexts)}")
    if not (2 <= len(dtexts) <= 4):
        return _fail(tool, cid, payload, f"need 2-4 descriptions, got {len(dtexts)}")
    for err in (_len_check(htexts, 30, "headline"), _len_check(dtexts, 90, "description"),
                _len_check([path1] if path1 else [], 15, "path1"), _len_check([path2] if path2 else [], 15, "path2")):
        if err:
            return _fail(tool, cid, payload, err)
    if len(set(htexts)) != len(htexts):
        return _fail(tool, cid, payload, "duplicate headline text")
    if status not in ("ENABLED", "PAUSED"):
        return _fail(tool, cid, payload, "status must be ENABLED or PAUSED")
    c = _get_client()
    op = c.get_type("MutateOperation")
    aga = op.ad_group_ad_operation.create
    aga.ad_group = c.get_service("AdGroupService").ad_group_path(cid, str(ad_group_id))
    aga.status = getattr(c.enums.AdGroupAdStatusEnum, status)
    aga.ad.final_urls.append(final_url)
    try:
        aga.ad.responsive_search_ad.headlines.extend(_text_assets(c, headlines))
        aga.ad.responsive_search_ad.descriptions.extend(_text_assets(c, descriptions))
    except ValueError as exc:
        return _fail(tool, cid, payload, str(exc))
    if path1:
        aga.ad.responsive_search_ad.path1 = path1
    if path2:
        aga.ad.responsive_search_ad.path2 = path2
    return _run_mutate(tool, cid, [op], confirm, payload)


@mcp.tool()
def add_keywords(
    customer_id: str,
    ad_group_id: str,
    keywords: List[Dict[str, str]],
    status: str = "ENABLED",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Add positive keywords to an ad group. keywords: [{"text": "photo to pdf", "match_type": "PHRASE|EXACT|BROAD"}].
    Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(ad_group_id=ad_group_id, keywords=keywords, status=status)
    tool = "add_keywords"
    if not keywords:
        return _fail(tool, cid, payload, "no keywords given")
    c = _get_client()
    ops = []
    for kw in keywords:
        mt = (kw.get("match_type") or "PHRASE").upper()
        if mt not in ("EXACT", "PHRASE", "BROAD"):
            return _fail(tool, cid, payload, f"bad match_type {mt!r}")
        op = c.get_type("MutateOperation")
        crit = op.ad_group_criterion_operation.create
        crit.ad_group = c.get_service("AdGroupService").ad_group_path(cid, str(ad_group_id))
        crit.status = getattr(c.enums.AdGroupCriterionStatusEnum, status)
        crit.keyword.text = kw["text"]
        crit.keyword.match_type = getattr(c.enums.KeywordMatchTypeEnum, mt)
        ops.append(op)
    return _run_mutate(tool, cid, ops, confirm, payload)


@mcp.tool()
def add_negative_keywords(
    customer_id: str,
    keywords: List[str],
    campaign_id: Optional[str] = None,
    ad_group_id: Optional[str] = None,
    match_type: str = "PHRASE",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Add NEGATIVE keywords at campaign level (campaign_id) or ad-group level (ad_group_id).
    match_type: PHRASE (default) | EXACT | BROAD. Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(keywords=keywords, campaign_id=campaign_id, ad_group_id=ad_group_id, match_type=match_type)
    tool = "add_negative_keywords"
    if bool(campaign_id) == bool(ad_group_id):
        return _fail(tool, cid, payload, "give exactly one of campaign_id or ad_group_id")
    mt = match_type.upper()
    if mt not in ("EXACT", "PHRASE", "BROAD"):
        return _fail(tool, cid, payload, f"bad match_type {mt!r}")
    c = _get_client()
    ops = []
    for text in keywords:
        op = c.get_type("MutateOperation")
        if campaign_id:
            crit = op.campaign_criterion_operation.create
            crit.campaign = c.get_service("CampaignService").campaign_path(cid, str(campaign_id))
        else:
            crit = op.ad_group_criterion_operation.create
            crit.ad_group = c.get_service("AdGroupService").ad_group_path(cid, str(ad_group_id))
        crit.negative = True
        crit.keyword.text = text
        crit.keyword.match_type = getattr(c.enums.KeywordMatchTypeEnum, mt)
        ops.append(op)
    return _run_mutate(tool, cid, ops, confirm, payload)


def _campaign_asset_ops(c: GoogleAdsClient, cid: str, campaign_id: str, assets: list, field_type: str,
                        ad_group_id: Optional[str] = None):
    """Create N assets with temp ids and link each to the campaign - or, if ad_group_id is given,
    to that ad group (ad-group-level assets override campaign-level ones of the same type) - atomically."""
    ops = []
    camp_rn = c.get_service("CampaignService").campaign_path(cid, str(campaign_id)) if campaign_id else None
    ag_rn = c.get_service("AdGroupService").ad_group_path(cid, str(ad_group_id)) if ad_group_id else None
    for i, fill in enumerate(assets, start=1):
        temp_rn = f"customers/{cid}/assets/-{i}"
        op = c.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = temp_rn
        fill(asset)
        ops.append(op)
        link = c.get_type("MutateOperation")
        if ag_rn:
            la = link.ad_group_asset_operation.create
            la.ad_group = ag_rn
        else:
            la = link.campaign_asset_operation.create
            la.campaign = camp_rn
        la.asset = temp_rn
        la.field_type = getattr(c.enums.AssetFieldTypeEnum, field_type)
        ops.append(link)
    return ops


@mcp.tool()
def add_sitelinks(customer_id: str, campaign_id: str, sitelinks: List[Dict[str, str]], confirm: bool = False,
                  ad_group_id: Optional[str] = None) -> Dict[str, Any]:
    """Create sitelink assets and attach them to a campaign, or to one ad group when ad_group_id is given
    (ad-group sitelinks override the campaign's for that ad group).
    sitelinks: [{"link_text": <=25 chars, "final_url": "https://...", "description1": <=35, "description2": <=35}, ...]
    Link text must be unique and URLs should differ (Google will not serve two sitelinks with the same URL together).
    Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, ad_group_id=ad_group_id, sitelinks=sitelinks)
    tool = "add_sitelinks"
    if not sitelinks:
        return _fail(tool, cid, payload, "no sitelinks given")
    for s in sitelinks:
        for key, mx in (("link_text", 25), ("description1", 35), ("description2", 35)):
            val = s.get(key, "")
            if key == "link_text" and not val:
                return _fail(tool, cid, payload, "link_text required")
            if len(val) > mx:
                return _fail(tool, cid, payload, f"{key} too long ({len(val)} > {mx}): {val!r}")
        if not s.get("final_url", "").startswith("http"):
            return _fail(tool, cid, payload, f"final_url missing/invalid for {s.get('link_text')!r}")
        if bool(s.get("description1")) != bool(s.get("description2")):
            return _fail(tool, cid, payload, "description1 and description2 must be given together")
    c = _get_client()

    def mk(s):
        def fill(asset):
            asset.sitelink_asset.link_text = s["link_text"]
            if s.get("description1"):
                asset.sitelink_asset.description1 = s["description1"]
                asset.sitelink_asset.description2 = s["description2"]
            asset.final_urls.append(s["final_url"])
        return fill

    ops = _campaign_asset_ops(c, cid, campaign_id, [mk(s) for s in sitelinks], "SITELINK", ad_group_id)
    return _run_mutate(tool, cid, ops, confirm, payload)


@mcp.tool()
def add_callouts(customer_id: str, campaign_id: str, callouts: List[str], confirm: bool = False) -> Dict[str, Any]:
    """Create callout assets (each <=25 chars) and attach them to a campaign. Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, callouts=callouts)
    tool = "add_callouts"
    err = _len_check(callouts, 25, "callout") if callouts else "no callouts given"
    if err:
        return _fail(tool, cid, payload, err)
    c = _get_client()

    def mk(text):
        def fill(asset):
            asset.callout_asset.callout_text = text
        return fill

    ops = _campaign_asset_ops(c, cid, campaign_id, [mk(t) for t in callouts], "CALLOUT")
    return _run_mutate(tool, cid, ops, confirm, payload)


@mcp.tool()
def add_structured_snippet(customer_id: str, campaign_id: str, header: str, values: List[str], confirm: bool = False) -> Dict[str, Any]:
    """Create one structured-snippet asset (header e.g. "Types", "Services"; 3-10 values, each <=25 chars)
    and attach it to a campaign. Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, header=header, values=values)
    tool = "add_structured_snippet"
    if not (3 <= len(values) <= 10):
        return _fail(tool, cid, payload, f"need 3-10 values, got {len(values)}")
    err = _len_check(values, 25, "snippet value")
    if err:
        return _fail(tool, cid, payload, err)
    c = _get_client()

    def fill(asset):
        asset.structured_snippet_asset.header = header
        asset.structured_snippet_asset.values.extend(values)

    ops = _campaign_asset_ops(c, cid, campaign_id, [fill], "STRUCTURED_SNIPPET")
    return _run_mutate(tool, cid, ops, confirm, payload)


@mcp.tool()
def set_status(customer_id: str, resource_name: str, status: str, confirm: bool = False) -> Dict[str, Any]:
    """Set ENABLED or PAUSED on a campaign, ad group, ad (adGroupAds/...~...) or keyword (adGroupCriteria/...~...).
    resource_name must be the full resource name from the read MCP, e.g. customers/123/campaigns/456.
    Dry run unless confirm=true. (REMOVED is intentionally not supported.)"""
    cid = _cid(customer_id)
    payload = dict(resource_name=resource_name, status=status)
    tool = "set_status"
    if status not in ("ENABLED", "PAUSED"):
        return _fail(tool, cid, payload, "status must be ENABLED or PAUSED")
    c = _get_client()
    op = c.get_type("MutateOperation")
    if "/campaigns/" in resource_name:
        ent = op.campaign_operation.update
        ent.status = getattr(c.enums.CampaignStatusEnum, status)
        upd = op.campaign_operation
    elif "/adGroups/" in resource_name:
        ent = op.ad_group_operation.update
        ent.status = getattr(c.enums.AdGroupStatusEnum, status)
        upd = op.ad_group_operation
    elif "/adGroupAds/" in resource_name:
        ent = op.ad_group_ad_operation.update
        ent.status = getattr(c.enums.AdGroupAdStatusEnum, status)
        upd = op.ad_group_ad_operation
    elif "/adGroupCriteria/" in resource_name:
        ent = op.ad_group_criterion_operation.update
        ent.status = getattr(c.enums.AdGroupCriterionStatusEnum, status)
        upd = op.ad_group_criterion_operation
    else:
        return _fail(tool, cid, payload, "unsupported resource type; use campaigns/, adGroups/, adGroupAds/ or adGroupCriteria/")
    ent.resource_name = resource_name
    upd.update_mask.CopyFrom(protobuf_helpers.field_mask(None, ent._pb))
    return _run_mutate(tool, cid, [op], confirm, payload)


@mcp.tool()
def set_campaign_target_cpa(customer_id: str, campaign_id: str, target_cpa: float, confirm: bool = False) -> Dict[str, Any]:
    """Set the target CPA (in account currency, e.g. 9.5) of a campaign that uses Maximize Conversions.
    Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, target_cpa=target_cpa)
    tool = "set_campaign_target_cpa"
    if not (0 < target_cpa < 10000):
        return _fail(tool, cid, payload, "target_cpa out of range")
    c = _get_client()
    op = c.get_type("MutateOperation")
    camp = op.campaign_operation.update
    camp.resource_name = c.get_service("CampaignService").campaign_path(cid, str(campaign_id))
    camp.maximize_conversions.target_cpa_micros = int(round(target_cpa * 1_000_000))
    op.campaign_operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, camp._pb))
    return _run_mutate(tool, cid, [op], confirm, payload)


@mcp.tool()
def set_campaign_cpc_ceiling(customer_id: str, campaign_id: str, cpc_bid_ceiling: float, confirm: bool = False) -> Dict[str, Any]:
    """Set the max CPC bid ceiling (account currency, e.g. 5.0) of a campaign that uses Maximize Clicks
    (Campaign.target_spend). Pass 0 to remove the ceiling. Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, cpc_bid_ceiling=cpc_bid_ceiling)
    tool = "set_campaign_cpc_ceiling"
    if not (0 <= cpc_bid_ceiling < 1_000):
        return _fail(tool, cid, payload, "cpc_bid_ceiling out of range")
    c = _get_client()
    op = c.get_type("MutateOperation")
    camp = op.campaign_operation.update
    camp.resource_name = c.get_service("CampaignService").campaign_path(cid, str(campaign_id))
    camp.target_spend.cpc_bid_ceiling_micros = int(round(cpc_bid_ceiling * 1_000_000))
    op.campaign_operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, camp._pb))
    return _run_mutate(tool, cid, [op], confirm, payload)


@mcp.tool()
def set_campaign_daily_budget(customer_id: str, campaign_id: str, daily_budget: float, confirm: bool = False) -> Dict[str, Any]:
    """Set a campaign's daily budget (account currency). Refuses if the budget is shared with other campaigns.
    Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, daily_budget=daily_budget)
    tool = "set_campaign_daily_budget"
    if not (0 < daily_budget < 1_000_000):
        return _fail(tool, cid, payload, "daily_budget out of range")
    c = _get_client()
    ga = c.get_service("GoogleAdsService")
    q = ("SELECT campaign.campaign_budget, campaign_budget.explicitly_shared, campaign_budget.amount_micros "
         f"FROM campaign WHERE campaign.id = {int(campaign_id)}")
    rows = list(ga.search(customer_id=cid, query=q))
    if not rows:
        return _fail(tool, cid, payload, "campaign not found")
    row = rows[0]
    if row.campaign_budget.explicitly_shared:
        return _fail(tool, cid, payload, "budget is explicitly shared; refusing to change it via this tool")
    payload["previous_daily_budget"] = row.campaign_budget.amount_micros / 1e6
    op = c.get_type("MutateOperation")
    b = op.campaign_budget_operation.update
    b.resource_name = row.campaign.campaign_budget
    b.amount_micros = int(round(daily_budget * 1_000_000))
    op.campaign_budget_operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, b._pb))
    return _run_mutate(tool, cid, [op], confirm, payload)



def _managed_label_rn(c: GoogleAdsClient, cid: str) -> Optional[str]:
    """Resource name of the managed label in this account, or None if it does not exist yet."""
    ga = c.get_service("GoogleAdsService")
    q = f"SELECT label.resource_name FROM label WHERE label.name = '{MANAGED_LABEL}' AND label.status = 'ENABLED'"
    rows = list(ga.search(customer_id=cid, query=q))
    return rows[0].label.resource_name if rows else None


# --------------------------------------------------------------------------- campaign creation
def _rsa_checks(headlines, descriptions, path1, path2) -> Optional[str]:
    htexts = [h if isinstance(h, str) else h.get("text", "") for h in headlines]
    dtexts = [d if isinstance(d, str) else d.get("text", "") for d in descriptions]
    if not (3 <= len(htexts) <= 15):
        return f"need 3-15 headlines, got {len(htexts)}"
    if not (2 <= len(dtexts) <= 4):
        return f"need 2-4 descriptions, got {len(dtexts)}"
    for err in (_len_check(htexts, 30, "headline"), _len_check(dtexts, 90, "description"),
                _len_check([path1] if path1 else [], 15, "path1"), _len_check([path2] if path2 else [], 15, "path2")):
        if err:
            return err
    if len(set(htexts)) != len(htexts):
        return "duplicate headline text"
    return None


def _ad_group_ops(c: GoogleAdsClient, cid: str, campaign_rn: str, spec: Dict[str, Any], next_temp: int):
    """Build one ad group plus its keywords and one RSA, all referencing a temp ad-group id.

    spec: {"name", "final_url", "keywords": [{"text","match_type"}], "headlines", "descriptions",
           "path1", "path2", "status"(ENABLED|PAUSED), "cpc_bid"(optional, account currency),
           "final_url_suffix"(optional, e.g. "utm_source=google&utm_medium=cpc&utm_campaign=x__y"),
           "negative_keywords"(optional, [{"text","match_type"}] at ad-group level)}
    Returns (ops, next_temp) or raises ValueError with a human message.
    """
    name = (spec.get("name") or "").strip()
    if not name:
        raise ValueError("ad group name is empty")
    final_url = spec.get("final_url") or ""
    if not final_url.startswith("http"):
        raise ValueError(f"{name}: final_url must be an absolute http(s) URL")
    kws = spec.get("keywords") or []
    if not kws:
        raise ValueError(f"{name}: no keywords")
    headlines, descriptions = spec.get("headlines") or [], spec.get("descriptions") or []
    path1, path2 = spec.get("path1") or "", spec.get("path2") or ""
    err = _rsa_checks(headlines, descriptions, path1, path2)
    if err:
        raise ValueError(f"{name}: {err}")
    status = spec.get("status") or "ENABLED"
    if status not in ("ENABLED", "PAUSED"):
        raise ValueError(f"{name}: status must be ENABLED or PAUSED")

    ops = []
    ag_rn = f"customers/{cid}/adGroups/{next_temp}"
    next_temp -= 1
    op = c.get_type("MutateOperation")
    ag = op.ad_group_operation.create
    ag.resource_name = ag_rn
    ag.name = name
    ag.campaign = campaign_rn
    ag.status = getattr(c.enums.AdGroupStatusEnum, status)
    ag.type_ = c.enums.AdGroupTypeEnum.SEARCH_STANDARD
    if spec.get("cpc_bid"):
        ag.cpc_bid_micros = int(round(float(spec["cpc_bid"]) * 1_000_000))
    suffix = (spec.get("final_url_suffix") or "").strip().lstrip("?")
    if suffix:
        if " " in suffix or "=" not in suffix:
            raise ValueError(f"{name}: final_url_suffix must look like key=value&key=value")
        ag.final_url_suffix = suffix
    ops.append(op)

    for kw in spec.get("negative_keywords") or []:
        mt = (kw.get("match_type") or "PHRASE").upper()
        if mt not in ("EXACT", "PHRASE", "BROAD"):
            raise ValueError(f"{name}: bad negative match_type {mt!r}")
        op = c.get_type("MutateOperation")
        crit = op.ad_group_criterion_operation.create
        crit.ad_group = ag_rn
        crit.negative = True
        crit.keyword.text = kw["text"]
        crit.keyword.match_type = getattr(c.enums.KeywordMatchTypeEnum, mt)
        ops.append(op)

    for kw in kws:
        mt = (kw.get("match_type") or "PHRASE").upper()
        if mt not in ("EXACT", "PHRASE", "BROAD"):
            raise ValueError(f"{name}: bad match_type {mt!r} for {kw.get('text')!r}")
        text = (kw.get("text") or "").strip()
        if not text or len(text) > 80:
            raise ValueError(f"{name}: keyword empty or >80 chars: {text!r}")
        op = c.get_type("MutateOperation")
        crit = op.ad_group_criterion_operation.create
        crit.ad_group = ag_rn
        crit.status = c.enums.AdGroupCriterionStatusEnum.ENABLED
        crit.keyword.text = text
        crit.keyword.match_type = getattr(c.enums.KeywordMatchTypeEnum, mt)
        ops.append(op)

    op = c.get_type("MutateOperation")
    aga = op.ad_group_ad_operation.create
    aga.ad_group = ag_rn
    aga.status = c.enums.AdGroupAdStatusEnum.ENABLED
    aga.ad.final_urls.append(final_url)
    aga.ad.responsive_search_ad.headlines.extend(_text_assets(c, headlines))
    aga.ad.responsive_search_ad.descriptions.extend(_text_assets(c, descriptions))
    if path1:
        aga.ad.responsive_search_ad.path1 = path1
    if path2:
        aga.ad.responsive_search_ad.path2 = path2
    ops.append(op)
    return ops, next_temp


@mcp.tool()
def create_search_campaign(
    customer_id: str,
    name: str,
    daily_budget: float,
    locations: List[int],
    languages: List[int],
    ad_groups: List[Dict[str, Any]],
    bidding: str = "MAXIMIZE_CONVERSIONS",
    target_cpa: Optional[float] = None,
    cpc_bid_ceiling: Optional[float] = None,
    negative_keywords: Optional[List[Dict[str, str]]] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Create a complete Search campaign in ONE atomic mutate: budget, campaign, location and
    language criteria, campaign negatives, and every ad group with its keywords and one RSA.

    The campaign gets the managed label (default "claude-managed"; created in the account on first
    use) so remove_entity can later act on it. It is created PAUSED; enable it with set_status.
    locations: geo target constant ids (United States = 2840); location targeting is created
    presence-only (positive and negative geo_target_type = PRESENCE). languages: language constant ids
    (English = 1000); an empty list means "all languages" (no language criterion is created). bidding: MAXIMIZE_CONVERSIONS (optional target_cpa) or MAXIMIZE_CLICKS (optional cpc_bid_ceiling in
    account currency; omitted = uncapped).
    Headlines/descriptions accept plain strings or {"text", "pin": "H1".."H3" | "D1" | "D2"}.
    Network: Google Search only (no partners, no Display).
    ad_groups: [{"name", "final_url", "keywords": [{"text","match_type"}], "headlines": [...],
                 "descriptions": [...], "path1", "path2", "status", "final_url_suffix",
                 "negative_keywords": [...]}, ...]
    negative_keywords: [{"text", "match_type"}] at campaign level.
    Device bid adjustments (e.g. mobile-only) are a follow-up call to set_device_bid_modifiers with the
    returned campaign id: the device criteria only exist once the campaign does.
    Extensions (sitelinks, callouts, snippets, images) are separate tools - call them with the
    returned campaign id. Dry run unless confirm=true.
    """
    cid = _cid(customer_id)
    payload = dict(name=name, daily_budget=daily_budget, locations=locations, languages=languages,
                   bidding=bidding, target_cpa=target_cpa, cpc_bid_ceiling=cpc_bid_ceiling,
                   ad_groups=[{"name": g.get("name"), "keywords": len(g.get("keywords") or [])} for g in ad_groups],
                   negative_keywords=len(negative_keywords or []))
    tool = "create_search_campaign"
    if not name.strip():
        return _fail(tool, cid, payload, "campaign name is empty")
    if not (0 < daily_budget < 100_000):
        return _fail(tool, cid, payload, "daily_budget out of range")
    if not locations:
        return _fail(tool, cid, payload, "locations are required")
    languages = languages or []          # empty = all languages (no language criterion)
    if not ad_groups:
        return _fail(tool, cid, payload, "at least one ad group is required")
    if bidding not in ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CLICKS"):
        return _fail(tool, cid, payload, "bidding must be MAXIMIZE_CONVERSIONS or MAXIMIZE_CLICKS")
    if target_cpa is not None and not (0 < target_cpa < 10_000):
        return _fail(tool, cid, payload, "target_cpa out of range")
    if cpc_bid_ceiling is not None:
        if bidding != "MAXIMIZE_CLICKS":
            return _fail(tool, cid, payload, "cpc_bid_ceiling is only valid with MAXIMIZE_CLICKS")
        if not (0 < cpc_bid_ceiling < 1_000):
            return _fail(tool, cid, payload, "cpc_bid_ceiling out of range")
    names = [g.get("name") for g in ad_groups]
    if len(set(names)) != len(names):
        return _fail(tool, cid, payload, "duplicate ad group names")

    c = _get_client()
    ops: list = []
    temp = -1

    label_rn = _managed_label_rn(c, cid)
    if not label_rn:
        label_rn = f"customers/{cid}/labels/{temp}"
        temp -= 1
        op = c.get_type("MutateOperation")
        lab = op.label_operation.create
        lab.resource_name = label_rn
        lab.name = MANAGED_LABEL
        ops.append(op)

    budget_rn = f"customers/{cid}/campaignBudgets/{temp}"
    temp -= 1
    op = c.get_type("MutateOperation")
    b = op.campaign_budget_operation.create
    b.resource_name = budget_rn
    b.name = f"{name} - budget"
    b.amount_micros = int(round(daily_budget * 1_000_000))
    b.delivery_method = c.enums.BudgetDeliveryMethodEnum.STANDARD
    b.explicitly_shared = False
    ops.append(op)

    campaign_rn = f"customers/{cid}/campaigns/{temp}"
    temp -= 1
    op = c.get_type("MutateOperation")
    camp = op.campaign_operation.create
    camp.resource_name = campaign_rn
    camp.name = name
    camp.status = c.enums.CampaignStatusEnum.PAUSED
    camp.advertising_channel_type = c.enums.AdvertisingChannelTypeEnum.SEARCH
    camp.campaign_budget = budget_rn
    camp.network_settings.target_google_search = True
    camp.network_settings.target_search_network = False
    camp.network_settings.target_content_network = False
    camp.network_settings.target_partner_search_network = False
    camp.geo_target_type_setting.positive_geo_target_type = c.enums.PositiveGeoTargetTypeEnum.PRESENCE
    camp.geo_target_type_setting.negative_geo_target_type = c.enums.NegativeGeoTargetTypeEnum.PRESENCE
    if hasattr(camp, "contains_eu_political_advertising"):
        camp.contains_eu_political_advertising = (
            c.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING)
    if bidding == "MAXIMIZE_CONVERSIONS":
        if target_cpa:
            camp.maximize_conversions.target_cpa_micros = int(round(target_cpa * 1_000_000))
        else:
            camp.maximize_conversions.target_cpa_micros = 0
    else:
        # Maximize clicks is the TargetSpend strategy on Campaign — there is no
        # maximize_clicks field on the proto; assigning it raises AttributeError.
        camp.target_spend.cpc_bid_ceiling_micros = (
            int(round(cpc_bid_ceiling * 1_000_000)) if cpc_bid_ceiling else 0)
    ops.append(op)

    op = c.get_type("MutateOperation")
    cl = op.campaign_label_operation.create
    cl.campaign = campaign_rn
    cl.label = label_rn
    ops.append(op)

    for geo in locations:
        op = c.get_type("MutateOperation")
        cc = op.campaign_criterion_operation.create
        cc.campaign = campaign_rn
        cc.location.geo_target_constant = f"geoTargetConstants/{int(geo)}"
        ops.append(op)
    for lang in languages:
        op = c.get_type("MutateOperation")
        cc = op.campaign_criterion_operation.create
        cc.campaign = campaign_rn
        cc.language.language_constant = f"languageConstants/{int(lang)}"
        ops.append(op)
    for kw in negative_keywords or []:
        mt = (kw.get("match_type") or "PHRASE").upper()
        if mt not in ("EXACT", "PHRASE", "BROAD"):
            return _fail(tool, cid, payload, f"bad negative match_type {mt!r}")
        op = c.get_type("MutateOperation")
        cc = op.campaign_criterion_operation.create
        cc.campaign = campaign_rn
        cc.negative = True
        cc.keyword.text = kw["text"]
        cc.keyword.match_type = getattr(c.enums.KeywordMatchTypeEnum, mt)
        ops.append(op)

    for spec in ad_groups:
        try:
            ag_ops, temp = _ad_group_ops(c, cid, campaign_rn, spec, temp)
        except ValueError as exc:
            return _fail(tool, cid, payload, str(exc))
        ops.extend(ag_ops)

    if len(ops) > 10_000:
        return _fail(tool, cid, payload, f"{len(ops)} operations exceeds the 10,000 per-request limit; split the campaign")
    payload["operations"] = len(ops)
    return _run_mutate(tool, cid, ops, confirm, payload)


@mcp.tool()
def add_ad_group(
    customer_id: str,
    campaign_id: str,
    name: str,
    final_url: str,
    keywords: List[Dict[str, str]],
    headlines: List[Union[str, Dict[str, str]]],
    descriptions: List[Union[str, Dict[str, str]]],
    path1: str = "",
    path2: str = "",
    status: str = "ENABLED",
    final_url_suffix: str = "",
    negative_keywords: Optional[List[Dict[str, str]]] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Add one ad group - with its keywords, optional ad-group negatives, optional final URL suffix
    and one responsive search ad - to an EXISTING campaign, atomically. Same argument shapes as
    create_search_campaign's ad_groups entries. Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, name=name, final_url=final_url, keywords=len(keywords),
                   headlines=len(headlines), descriptions=len(descriptions), status=status,
                   final_url_suffix=final_url_suffix, negative_keywords=len(negative_keywords or []))
    tool = "add_ad_group"
    c = _get_client()
    campaign_rn = c.get_service("CampaignService").campaign_path(cid, str(campaign_id))
    spec = dict(name=name, final_url=final_url, keywords=keywords, headlines=headlines,
                descriptions=descriptions, path1=path1, path2=path2, status=status,
                final_url_suffix=final_url_suffix, negative_keywords=negative_keywords or [])
    try:
        ops, _ = _ad_group_ops(c, cid, campaign_rn, spec, -1)
    except ValueError as exc:
        return _fail(tool, cid, payload, str(exc))
    return _run_mutate(tool, cid, ops, confirm, payload)


# --------------------------------------------------------------------------- images
_IMAGE_FIELD_TYPES = {
    # field type -> (aspect ratio, min width, min height)
    "SQUARE_MARKETING_IMAGE": (1.0, 300, 300),
    "MARKETING_IMAGE": (1.91, 600, 314),
}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _image_dims(data: bytes) -> Optional[tuple]:
    """Width/height from PNG or JPEG headers; None if unrecognised."""
    import struct
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return (w, h)
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg
    return None


@mcp.tool()
def add_image_assets(
    customer_id: str,
    campaign_id: str,
    images: List[Dict[str, str]],
    ad_group_id: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Upload image files as image assets and attach them to a Search campaign as AD_IMAGE.

    With ad_group_id the images are linked to that AD GROUP instead of the campaign
    (campaign_id is still required for context/audit; Google allows up to 20 images per
    campaign and per ad group, and ad-group links override campaign links when serving).

    images: [{"path": "/absolute/file.png", "field_type": "SQUARE_MARKETING_IMAGE"|"MARKETING_IMAGE",
              "name": optional asset name}, ...]
    field_type here declares the SHAPE (1:1 or 1.91:1) and drives local validation; the link itself
    is always AD_IMAGE, which is the only image field type Search campaigns accept
    (MARKETING_IMAGE / SQUARE_MARKETING_IMAGE are Performance Max and Display field types).
    or, to attach an image already in the account: {"asset_resource_name": "customers/../assets/..",
    "field_type": ...}. PNG or JPEG, <=5 MB, 1:1 >=300x300 or 1.91:1 >=600x314; the file's aspect
    ratio must match the field_type. Google rejects an image identical to an existing asset
    (DUPLICATE_ASSET) - pass its asset_resource_name instead. Dry run unless confirm=true.
    """
    cid = _cid(customer_id)
    tool = "add_image_assets"
    payload: Dict[str, Any] = dict(campaign_id=campaign_id, ad_group_id=ad_group_id, images=[])
    if not images:
        return _fail(tool, cid, payload, "no images given")
    c = _get_client()
    camp_rn = c.get_service("CampaignService").campaign_path(cid, str(campaign_id))
    ag_rn = f"customers/{cid}/adGroups/{ad_group_id}" if ad_group_id else None
    ops = []
    for i, im in enumerate(images, start=1):
        ft = (im.get("field_type") or "").upper()
        if ft not in _IMAGE_FIELD_TYPES:
            return _fail(tool, cid, payload, f"field_type must be one of {sorted(_IMAGE_FIELD_TYPES)}")
        if im.get("asset_resource_name"):
            asset_rn = im["asset_resource_name"]
            payload["images"].append({"asset_resource_name": asset_rn, "field_type": ft})
        else:
            path = im.get("path") or ""
            if not os.path.isabs(path) or not os.path.isfile(path):
                return _fail(tool, cid, payload, f"path must be an absolute path to an existing file: {path!r}")
            if not path.lower().endswith((".png", ".jpg", ".jpeg")):
                return _fail(tool, cid, payload, f"only PNG/JPEG are accepted: {path!r}")
            with open(path, "rb") as fh:
                data = fh.read()
            if len(data) > _MAX_IMAGE_BYTES:
                return _fail(tool, cid, payload, f"{path}: {len(data)} bytes exceeds 5 MB")
            dims = _image_dims(data)
            if not dims:
                return _fail(tool, cid, payload, f"{path}: could not read image dimensions")
            w, h = dims
            ratio, min_w, min_h = _IMAGE_FIELD_TYPES[ft]
            if w < min_w or h < min_h:
                return _fail(tool, cid, payload, f"{path}: {w}x{h} below minimum {min_w}x{min_h} for {ft}")
            if abs((w / h) - ratio) > 0.02:
                return _fail(tool, cid, payload, f"{path}: aspect {w/h:.3f} does not match {ft} ({ratio})")
            asset_rn = f"customers/{cid}/assets/-{i}"
            op = c.get_type("MutateOperation")
            asset = op.asset_operation.create
            asset.resource_name = asset_rn
            asset.name = im.get("name") or os.path.splitext(os.path.basename(path))[0]
            asset.type_ = c.enums.AssetTypeEnum.IMAGE
            asset.image_asset.data = data
            ops.append(op)
            payload["images"].append({"path": path, "bytes": len(data), "dims": f"{w}x{h}", "field_type": ft})
        link = c.get_type("MutateOperation")
        if ag_rn:
            aga = link.ad_group_asset_operation.create
            aga.ad_group = ag_rn
            aga.asset = asset_rn
            aga.field_type = c.enums.AssetFieldTypeEnum.AD_IMAGE
        else:
            ca = link.campaign_asset_operation.create
            ca.campaign = camp_rn
            ca.asset = asset_rn
            ca.field_type = c.enums.AssetFieldTypeEnum.AD_IMAGE
        ops.append(link)
    return _run_mutate(tool, cid, ops, confirm, payload)


@mcp.tool()
def add_business_assets(
    customer_id: str,
    campaign_id: str,
    business_name: Optional[str] = None,
    logo_path: Optional[str] = None,
    logo_asset_resource_name: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Attach a business name and/or business logo to a Search campaign.

    business_name (<=25 chars, Google's limit) is created as a TEXT asset and linked as
    BUSINESS_NAME. For the logo pass logo_path (absolute PNG/JPEG, square within 2%,
    >=128x128, <=5 MB; 1200x1200 recommended) to upload a new asset, or
    logo_asset_resource_name to re-link an image asset the account already holds; either is
    linked as BUSINESS_LOGO. At least one input is required. Dry run unless confirm=true.
    """
    cid = _cid(customer_id)
    tool = "add_business_assets"
    payload: Dict[str, Any] = dict(campaign_id=campaign_id, business_name=business_name,
                                   logo_path=logo_path,
                                   logo_asset_resource_name=logo_asset_resource_name)
    if not business_name and not logo_path and not logo_asset_resource_name:
        return _fail(tool, cid, payload, "nothing to attach: pass business_name and/or a logo")
    if logo_path and logo_asset_resource_name:
        return _fail(tool, cid, payload, "pass either logo_path or logo_asset_resource_name")
    c = _get_client()
    camp_rn = c.get_service("CampaignService").campaign_path(cid, str(campaign_id))
    ops = []
    if business_name:
        bn = business_name.strip()
        if not bn or len(bn) > 25:
            return _fail(tool, cid, payload, f"business_name must be 1-25 chars, got {len(bn)}")
        name_rn = f"customers/{cid}/assets/-1"
        op = c.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = name_rn
        asset.type_ = c.enums.AssetTypeEnum.TEXT
        asset.text_asset.text = bn
        ops.append(op)
        link = c.get_type("MutateOperation")
        ca = link.campaign_asset_operation.create
        ca.campaign = camp_rn
        ca.asset = name_rn
        ca.field_type = c.enums.AssetFieldTypeEnum.BUSINESS_NAME
        ops.append(link)
    logo_rn = logo_asset_resource_name
    if logo_path:
        if not os.path.isabs(logo_path) or not os.path.isfile(logo_path):
            return _fail(tool, cid, payload, f"logo_path must be an absolute path to an existing file: {logo_path!r}")
        if not logo_path.lower().endswith((".png", ".jpg", ".jpeg")):
            return _fail(tool, cid, payload, f"only PNG/JPEG are accepted: {logo_path!r}")
        with open(logo_path, "rb") as fh:
            data = fh.read()
        if len(data) > _MAX_IMAGE_BYTES:
            return _fail(tool, cid, payload, f"{logo_path}: {len(data)} bytes exceeds 5 MB")
        dims = _image_dims(data)
        if not dims:
            return _fail(tool, cid, payload, f"{logo_path}: could not read image dimensions")
        w, h = dims
        if w < 128 or h < 128:
            return _fail(tool, cid, payload, f"{logo_path}: {w}x{h} below the 128x128 logo minimum")
        if abs((w / h) - 1.0) > 0.02:
            return _fail(tool, cid, payload, f"{logo_path}: logo must be square, got {w}x{h}")
        logo_rn = f"customers/{cid}/assets/-2"
        op = c.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = logo_rn
        asset.name = os.path.splitext(os.path.basename(logo_path))[0]
        asset.type_ = c.enums.AssetTypeEnum.IMAGE
        asset.image_asset.data = data
        ops.append(op)
        payload["logo"] = {"bytes": len(data), "dims": f"{w}x{h}"}
    if logo_rn:
        link = c.get_type("MutateOperation")
        ca = link.campaign_asset_operation.create
        ca.campaign = camp_rn
        ca.asset = logo_rn
        ca.field_type = c.enums.AssetFieldTypeEnum.BUSINESS_LOGO
        ops.append(link)
    return _run_mutate(tool, cid, ops, confirm, payload)


# --------------------------------------------------------------------------- removal (guarded)
@mcp.tool()
def remove_entity(customer_id: str, resource_name: Union[str, List[str]], confirm: bool = False) -> Dict[str, Any]:
    """PERMANENTLY remove a campaign, ad group, ad (adGroupAds/...), keyword (adGroupCriteria/...),
    campaign criterion such as a campaign negative keyword (campaignCriteria/...), or an asset LINK
    (campaignAssets/..., adGroupAssets/... - the asset itself stays in the account's asset library).

    resource_name may be one resource name or a list of names of the SAME kind; a list is removed
    in one atomic mutate. Refuses unless every entity sits under a campaign carrying the managed
    label (default "claude-managed") - i.e. one created by this server. Google Ads has no restore
    for removed entities; historical data stays in reports but the entity cannot be re-enabled.
    Dry run unless confirm=true.
    """
    cid = _cid(customer_id)
    names = [resource_name] if isinstance(resource_name, str) else list(resource_name)
    payload = dict(resource_names=names)
    tool = "remove_entity"
    if not names:
        return _fail(tool, cid, payload, "no resource names given")
    kinds = {
        "/campaigns/": ("campaign", "campaign", "campaign.resource_name"),
        "/adGroups/": ("ad_group", "ad_group", "ad_group.resource_name"),
        "/adGroupAds/": ("ad_group_ad", "ad_group_ad", "ad_group_ad.resource_name"),
        "/adGroupCriteria/": ("ad_group_criterion", "ad_group_criterion", "ad_group_criterion.resource_name"),
        "/campaignCriteria/": ("campaign_criterion", "campaign_criterion", "campaign_criterion.resource_name"),
        "/campaignAssets/": ("campaign_asset", "campaign_asset", "campaign_asset.resource_name"),
        "/adGroupAssets/": ("ad_group_asset", "ad_group_asset", "ad_group_asset.resource_name"),
    }
    matched = {k for n in names for k in kinds if k in n}
    if len(matched) != 1:
        return _fail(tool, cid, payload, "all resource names must be of one supported kind: campaigns/, adGroups/, "
                                         "adGroupAds/, adGroupCriteria/, campaignCriteria/, campaignAssets/ or adGroupAssets/")
    kind, resource, rn_field = kinds[matched.pop()]
    c = _get_client()
    ga = c.get_service("GoogleAdsService")
    in_list = ",".join(f"'{n}'" for n in names)
    q = f"SELECT campaign.name, campaign.labels, {rn_field} FROM {resource} WHERE {rn_field} IN ({in_list})"
    rows = list(ga.search(customer_id=cid, query=q))
    found = {}
    for r in rows:
        obj = getattr(r, resource)
        found[obj.resource_name] = (r.campaign.name, list(r.campaign.labels))
    missing = [n for n in names if n not in found]
    if missing:
        return _fail(tool, cid, payload, f"not found (already removed, or wrong customer): {missing[:5]}")
    label_rn = _managed_label_rn(c, cid)
    unmanaged = sorted({cn for cn, labels in found.values() if not label_rn or label_rn not in labels})
    if unmanaged:
        return _fail(tool, cid, payload,
                     f"refusing: parent campaign(s) {unmanaged} do not carry the {MANAGED_LABEL!r} label")
    payload["campaigns"] = sorted({cn for cn, _ in found.values()})
    ops = []
    for n in names:
        op = c.get_type("MutateOperation")
        getattr(op, f"{kind}_operation").remove = n
        ops.append(op)
    return _run_mutate(tool, cid, ops, confirm, payload)




# --------------------------------------------------------------------------- URL updates
@mcp.tool()
def set_final_urls(customer_id: str, targets: List[Dict[str, Any]], confirm: bool = False) -> Dict[str, Any]:
    """Update final URLs and/or final URL suffixes in one atomic mutate.

    targets: [{"resource_name": <ad (adGroupAds/..~..) | ad group (adGroups/..) | asset (assets/..)>,
               "final_url": "https://..." (ads and assets),
               "final_url_suffix": "utm_..." or "" to clear (ad groups and ads)}, ...]
    Ads keep their text and history: Google allows changing an ad's final URLs in place.
    Typical use: move UTMs from an ad-group suffix into the final URL itself, or retag a campaign.
    Dry run unless confirm=true.
    """
    cid = _cid(customer_id)
    payload = dict(targets=targets)
    tool = "set_final_urls"
    if not targets:
        return _fail(tool, cid, payload, "no targets given")
    c = _get_client()
    ops = []
    for t in targets:
        rn = t.get("resource_name") or ""
        url = t.get("final_url")
        suffix = t.get("final_url_suffix")
        if url is not None and not str(url).startswith("http"):
            return _fail(tool, cid, payload, f"{rn}: final_url must be an absolute http(s) URL")
        if suffix is not None and (" " in suffix or (suffix and "=" not in suffix)):
            return _fail(tool, cid, payload, f"{rn}: final_url_suffix must look like key=value&key=value (or empty)")
        op = c.get_type("MutateOperation")
        if "/adGroupAds/" in rn:
            ad_id = rn.rsplit("~", 1)[-1]
            ad = op.ad_operation.update
            ad.resource_name = f"customers/{cid}/ads/{ad_id}"
            if url is not None:
                del ad.final_urls[:]
                ad.final_urls.append(url)
            if suffix is not None:
                ad.final_url_suffix = suffix
            if url is None and suffix is None:
                return _fail(tool, cid, payload, f"{rn}: give final_url and/or final_url_suffix")
            op.ad_operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, ad._pb))
        elif "/adGroups/" in rn:
            if suffix is None:
                return _fail(tool, cid, payload, f"{rn}: ad groups take final_url_suffix only")
            ag = op.ad_group_operation.update
            ag.resource_name = rn
            ag.final_url_suffix = suffix
            mask = protobuf_helpers.field_mask(None, ag._pb)
            if "final_url_suffix" not in mask.paths:
                mask.paths.append("final_url_suffix")      # clearing to "" must still be in the mask
            op.ad_group_operation.update_mask.CopyFrom(mask)
        elif "/assets/" in rn:
            if url is None:
                return _fail(tool, cid, payload, f"{rn}: assets take final_url only")
            asset = op.asset_operation.update
            asset.resource_name = rn
            del asset.final_urls[:]
            asset.final_urls.append(url)
            op.asset_operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, asset._pb))
        else:
            return _fail(tool, cid, payload, f"unsupported resource: {rn}")
        ops.append(op)
    return _run_mutate(tool, cid, ops, confirm, payload)


# --------------------------------------------------------------------------- in-place RSA edit
@mcp.tool()
def update_responsive_search_ad(
    customer_id: str,
    ad_id: str,
    headlines: Optional[List[Union[str, Dict[str, str]]]] = None,
    descriptions: Optional[List[Union[str, Dict[str, str]]]] = None,
    path1: Optional[str] = None,
    path2: Optional[str] = None,
    final_url: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Edit an EXISTING responsive search ad in place (AdService update): the ad keeps its id and
    reporting history, goes back through policy review, and its asset performance labels / Ad
    Strength reset. Every argument left None is unchanged. headlines / descriptions, when given,
    REPLACE the whole list (3-15 headlines <=30 chars, 2-4 descriptions <=90 chars; items are strings
    or {"text", "pin": "HEADLINE_1|HEADLINE_2|HEADLINE_3|DESCRIPTION_1|DESCRIPTION_2"}). path1 / path2:
    "" clears. ad_id: the numeric ad id or an adGroupAds/<group>~<ad> resource name.
    Dry run unless confirm=true.
    """
    cid = _cid(customer_id)
    payload = dict(ad_id=ad_id, headlines=headlines, descriptions=descriptions, path1=path1, path2=path2,
                   final_url=final_url)
    tool = "update_responsive_search_ad"
    ad_num = str(ad_id).rsplit("~", 1)[-1].rsplit("/", 1)[-1]
    if not ad_num.isdigit():
        return _fail(tool, cid, payload, f"cannot parse an ad id from {ad_id!r}")
    if headlines is None and descriptions is None and path1 is None and path2 is None and final_url is None:
        return _fail(tool, cid, payload, "nothing to change: give headlines, descriptions, path1, path2 or final_url")
    if headlines is not None:
        htexts = [h if isinstance(h, str) else h.get("text", "") for h in headlines]
        if not (3 <= len(htexts) <= 15):
            return _fail(tool, cid, payload, f"need 3-15 headlines, got {len(htexts)}")
        if len(set(htexts)) != len(htexts):
            return _fail(tool, cid, payload, "duplicate headline text")
        err = _len_check(htexts, 30, "headline")
        if err:
            return _fail(tool, cid, payload, err)
    if descriptions is not None:
        dtexts = [d if isinstance(d, str) else d.get("text", "") for d in descriptions]
        if not (2 <= len(dtexts) <= 4):
            return _fail(tool, cid, payload, f"need 2-4 descriptions, got {len(dtexts)}")
        err = _len_check(dtexts, 90, "description")
        if err:
            return _fail(tool, cid, payload, err)
    for label, p in (("path1", path1), ("path2", path2)):
        if p:
            err = _len_check([p], 15, label)
            if err:
                return _fail(tool, cid, payload, err)
    if final_url is not None and not str(final_url).startswith("http"):
        return _fail(tool, cid, payload, "final_url must be an absolute http(s) URL")
    c = _get_client()
    op = c.get_type("MutateOperation")
    ad = op.ad_operation.update
    ad.resource_name = f"customers/{cid}/ads/{ad_num}"
    paths = []
    try:
        if headlines is not None:
            del ad.responsive_search_ad.headlines[:]
            ad.responsive_search_ad.headlines.extend(_text_assets(c, headlines))
            paths.append("responsive_search_ad.headlines")
        if descriptions is not None:
            del ad.responsive_search_ad.descriptions[:]
            ad.responsive_search_ad.descriptions.extend(_text_assets(c, descriptions))
            paths.append("responsive_search_ad.descriptions")
    except ValueError as exc:
        return _fail(tool, cid, payload, str(exc))
    if path1 is not None:
        ad.responsive_search_ad.path1 = path1
        paths.append("responsive_search_ad.path1")
    if path2 is not None:
        ad.responsive_search_ad.path2 = path2
        paths.append("responsive_search_ad.path2")
    if final_url is not None:
        del ad.final_urls[:]
        ad.final_urls.append(final_url)
        paths.append("final_urls")
    # explicit mask: repeated fields and "" clears must be named, comparison-built masks drop them
    op.ad_operation.update_mask.paths.extend(paths)
    return _run_mutate(tool, cid, [op], confirm, payload)


# --------------------------------------------------------------------------- campaign final URL suffix
@mcp.tool()
def set_campaign_final_url_suffix(customer_id: str, campaign_id: str, final_url_suffix: str, confirm: bool = False) -> Dict[str, Any]:
    """Set the CAMPAIGN-level final URL suffix, or clear it with "". Google appends the suffix to every
    final URL in the campaign at click time and substitutes ValueTrack tokens, e.g.
    "utm_term={keyword}&kw_match={matchtype}&device={device}". An ad-group or ad suffix overrides the
    campaign's, so keep the suffix at one level. Ads, their text and history are untouched.
    Dry run unless confirm=true.
    """
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, final_url_suffix=final_url_suffix)
    tool = "set_campaign_final_url_suffix"
    suffix = final_url_suffix or ""
    if " " in suffix or (suffix and "=" not in suffix) or suffix.startswith(("?", "&")):
        return _fail(tool, cid, payload, "final_url_suffix must look like key=value&key=value (no leading ? or &), or be empty")
    c = _get_client()
    op = c.get_type("MutateOperation")
    camp = op.campaign_operation.update
    camp.resource_name = c.get_service("CampaignService").campaign_path(cid, str(campaign_id))
    camp.final_url_suffix = suffix
    op.campaign_operation.update_mask.paths.append("final_url_suffix")   # "" must still be in the mask
    return _run_mutate(tool, cid, [op], confirm, payload)


# --------------------------------------------------------------------------- device bid modifiers
# Google's fixed criterion ids for device criteria (campaignCriteria/<campaign>~<id>).
_DEVICE_IDS = {"DESKTOP": 30000, "MOBILE": 30001, "TABLET": 30002}


def _device_ops(c: GoogleAdsClient, cid: str, campaign_id: str, modifiers: Dict[str, Optional[float]]):
    """modifiers: {"DESKTOP": -100, "TABLET": -100, "MOBILE": None} as percent adjustments.
    Device criteria always exist implicitly, so this is always an UPDATE by the fixed resource
    name campaignCriteria/<campaign>~<device id>. The update mask names bid_modifier explicitly:
    protobuf field masks built by comparison drop fields at their default, and 0.0 (-100%) is
    the default for a double - the silent no-op that bit the first live attempt."""
    ops = []
    for dev, pct in modifiers.items():
        if pct is None:
            continue
        if dev not in _DEVICE_IDS:
            raise ValueError(f"unknown device {dev!r}; use DESKTOP, TABLET, MOBILE")
        if not (-100 <= pct <= 900):
            raise ValueError(f"{dev}: adjustment must be between -100 and +900 percent")
        op = c.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.update
        crit.resource_name = f"customers/{cid}/campaignCriteria/{int(campaign_id)}~{_DEVICE_IDS[dev]}"
        crit.bid_modifier = round(1 + pct / 100.0, 2)
        op.campaign_criterion_operation.update_mask.paths.append("bid_modifier")
        ops.append(op)
    return ops


@mcp.tool()
def set_device_bid_modifiers(
    customer_id: str,
    campaign_id: str,
    desktop: Optional[float] = None,
    tablet: Optional[float] = None,
    mobile: Optional[float] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Set campaign-level device bid adjustments in percent (-100 to +900). -100 excludes the device:
    a mobile-only Search campaign is desktop=-100, tablet=-100. Smart Bidding strategies honour only
    the -100 case; other values are advisory under Maximize conversions / tCPA.
    Leave a device None to keep it unchanged. Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, desktop=desktop, tablet=tablet, mobile=mobile)
    tool = "set_device_bid_modifiers"
    wanted = {"DESKTOP": desktop, "TABLET": tablet, "MOBILE": mobile}
    if all(v is None for v in wanted.values()):
        return _fail(tool, cid, payload, "nothing to change")
    c = _get_client()
    try:
        ops = _device_ops(c, cid, campaign_id, wanted)
    except ValueError as exc:
        return _fail(tool, cid, payload, str(exc))
    return _run_mutate(tool, cid, ops, confirm, payload)


def _location_ops(c: GoogleAdsClient, cid: str, campaign_id: str, modifiers: Dict[str, Optional[float]]):
    """modifiers: {"2840": 30, "2344": -30, "geoTargetConstants/2410": 0} as percent adjustments on the
    campaign's EXISTING positive location criteria. Location criteria are real rows (unlike devices), so
    the resource names come from a read; a geo the campaign does not target is an error rather than a
    silent add — this tool never changes targeting. 0 resets the adjustment (bid_modifier 1.0). The
    update mask names bid_modifier explicitly for the same reason as _device_ops."""
    ga = c.get_service("GoogleAdsService")
    q = (
        "SELECT campaign.id, campaign_criterion.resource_name, "
        "campaign_criterion.location.geo_target_constant, campaign_criterion.bid_modifier "
        f"FROM campaign_criterion WHERE campaign.id = {int(campaign_id)} "
        "AND campaign_criterion.type = LOCATION AND campaign_criterion.negative = FALSE"
    )
    targeted: Dict[str, str] = {}
    for row in ga.search(customer_id=cid, query=q):
        geo = str(row.campaign_criterion.location.geo_target_constant)
        targeted[geo.rsplit("/", 1)[-1]] = row.campaign_criterion.resource_name
    ops = []
    for key, pct in modifiers.items():
        if pct is None:
            continue
        gid = str(key).strip().rsplit("/", 1)[-1]
        if not gid.isdigit():
            raise ValueError(f"{key!r}: use a geo target constant id such as 2840 or geoTargetConstants/2840")
        if gid not in targeted:
            raise ValueError(
                f"campaign {campaign_id} does not target geoTargetConstants/{gid} "
                f"(targeted: {', '.join(sorted(targeted)) or 'none'}); this tool never adds locations"
            )
        if not (-90 <= float(pct) <= 900):
            raise ValueError(
                f"geoTargetConstants/{gid}: location adjustments must be between -90 and +900 percent "
                "(-100 is not valid for locations; exclude a location with a negative criterion instead)"
            )
        op = c.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.update
        crit.resource_name = targeted[gid]
        crit.bid_modifier = round(1 + float(pct) / 100.0, 2)
        op.campaign_criterion_operation.update_mask.paths.append("bid_modifier")
        ops.append(op)
    return ops


@mcp.tool()
def set_location_bid_modifiers(
    customer_id: str,
    campaign_id: str,
    modifiers: Dict[str, float],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Set campaign-level LOCATION bid adjustments in percent (-90 to +900) on locations the campaign
    already targets. modifiers maps a geo target constant id (2840 = US, 2344 = Hong Kong; also accepts
    "geoTargetConstants/2840") to the adjustment: {"2840": 30, "2344": -30}. 0 removes an adjustment.
    Honoured by Manual CPC and Maximize clicks; Smart Bidding (tCPA / Maximize conversions) ignores
    location adjustments. Targeting is never changed: a geo the campaign does not target is rejected.
    One atomic mutate; dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, modifiers=modifiers)
    tool = "set_location_bid_modifiers"
    if not modifiers:
        return _fail(tool, cid, payload, "nothing to change")
    c = _get_client()
    try:
        ops = _location_ops(c, cid, campaign_id, modifiers)
    except ValueError as exc:
        return _fail(tool, cid, payload, str(exc))
    except GoogleAdsException as exc:
        return _fail(tool, cid, payload, f"could not read campaign locations: {exc.failure.errors[0].message if exc.failure.errors else exc}")
    if not ops:
        return _fail(tool, cid, payload, "nothing to change")
    return _run_mutate(tool, cid, ops, confirm, payload)


def _audience_ops(c: GoogleAdsClient, cid: str, campaign_id: str, user_interest_ids: List[Union[str, int]],
                  bid_modifier_pct: Optional[float]):
    """Observation-mode audiences on a Search campaign: (1) the campaign's targeting setting gets
    AUDIENCE -> bid_only=true (Google's "Observation"), so the segments never narrow who sees the ads;
    other dimensions already in target_restrictions are preserved; (2) one campaign_criterion per
    user_interest id (in-market / affinity, customers/<cid>/userInterests/<id>), optionally with a
    bid adjustment (-90..+900 percent; None = no adjustment, pure reporting). Segments the campaign
    already carries are skipped rather than duplicated."""
    ga = c.get_service("GoogleAdsService")
    q = (
        "SELECT campaign.id, campaign.advertising_channel_type, "
        "campaign.targeting_setting.target_restrictions FROM campaign "
        f"WHERE campaign.id = {int(campaign_id)}"
    )
    rows = list(ga.search(customer_id=cid, query=q))
    if not rows:
        raise ValueError(f"campaign {campaign_id} not found")
    camp = rows[0].campaign
    if camp.advertising_channel_type != c.enums.AdvertisingChannelTypeEnum.SEARCH:
        raise ValueError(f"campaign {campaign_id} is not a Search campaign")
    q2 = (
        "SELECT campaign_criterion.user_interest.user_interest_category FROM campaign_criterion "
        f"WHERE campaign.id = {int(campaign_id)} AND campaign_criterion.type = USER_INTEREST "
        "AND campaign_criterion.negative = FALSE"
    )
    present = {str(r.campaign_criterion.user_interest.user_interest_category).rsplit("/", 1)[-1]
               for r in ga.search(customer_id=cid, query=q2)}
    if bid_modifier_pct is not None and not (-90 <= float(bid_modifier_pct) <= 900):
        raise ValueError("bid_modifier_pct must be between -90 and +900 percent")
    ops = []
    # (1) targeting setting: AUDIENCE dimension in observation (bid_only) mode.
    op = c.get_type("MutateOperation")
    upd = op.campaign_operation.update
    upd.resource_name = c.get_service("CampaignService").campaign_path(cid, campaign_id)
    aud_dim = c.enums.TargetingDimensionEnum.AUDIENCE
    kept = False
    for tr in camp.targeting_setting.target_restrictions:
        r = c.get_type("TargetRestriction")
        r.targeting_dimension = tr.targeting_dimension
        r.bid_only = True if tr.targeting_dimension == aud_dim else tr.bid_only
        kept = kept or tr.targeting_dimension == aud_dim
        upd.targeting_setting.target_restrictions.append(r)
    if not kept:
        r = c.get_type("TargetRestriction")
        r.targeting_dimension = aud_dim
        r.bid_only = True
        upd.targeting_setting.target_restrictions.append(r)
    op.campaign_operation.update_mask.paths.append("targeting_setting.target_restrictions")
    ops.append(op)
    # (2) one criterion per new segment.
    added: List[str] = []
    for raw in user_interest_ids:
        uid = str(raw).strip().rsplit("/", 1)[-1]
        if not uid.isdigit():
            raise ValueError(f"{raw!r}: use a user interest id such as 80276 or customers/<cid>/userInterests/80276")
        if uid in present or uid in added:
            continue
        op = c.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.create
        crit.campaign = upd.resource_name
        crit.user_interest.user_interest_category = c.get_service("UserInterestService").user_interest_path(cid, uid)
        if bid_modifier_pct is not None:
            crit.bid_modifier = round(1 + float(bid_modifier_pct) / 100.0, 2)
        ops.append(op)
        added.append(uid)
    return ops, added, sorted(present)


@mcp.tool()
def add_campaign_audiences(
    customer_id: str,
    campaign_id: str,
    user_interest_ids: List[Union[str, int]],
    bid_modifier_pct: Optional[float] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Attach in-market / affinity segments to a Search campaign in OBSERVATION mode: the campaign's
    AUDIENCE targeting dimension is set to bid_only (Google's "Observation"), so nothing narrows who
    sees the ads; the segments only become reporting rows (and, with bid_modifier_pct, a bid
    adjustment from -90 to +900 percent applied to every segment in this call). user_interest_ids:
    ids from the read MCP, e.g. SELECT user_interest.user_interest_id, user_interest.name FROM
    user_interest WHERE user_interest.taxonomy_type = 'IN_MARKET' AND user_interest.name LIKE
    '%Software%' (80276 = In-market Software, 80279 = Business & Productivity Software, 80530 =
    Enterprise Software). Segments already on the campaign are skipped. Targeting mode is never
    set by this tool. One atomic mutate; dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, user_interest_ids=user_interest_ids, bid_modifier_pct=bid_modifier_pct)
    tool = "add_campaign_audiences"
    if not user_interest_ids:
        return _fail(tool, cid, payload, "no user_interest_ids given")
    c = _get_client()
    try:
        ops, added, present = _audience_ops(c, cid, campaign_id, user_interest_ids, bid_modifier_pct)
    except ValueError as exc:
        return _fail(tool, cid, payload, str(exc))
    except GoogleAdsException as exc:
        return _fail(tool, cid, payload, f"could not read the campaign: {exc.failure.errors[0].message if exc.failure.errors else exc}")
    if not added:
        return _fail(tool, cid, payload, f"every requested segment is already on the campaign (present: {', '.join(present) or 'none'})")
    out = _run_mutate(tool, cid, ops, confirm, payload)
    out["segments_added"] = added
    out["segments_already_present"] = present
    out["mode"] = "OBSERVATION (bid_only)"
    return out


# --------------------------------------------------------------------------- keyword planner (read-only)
_KP_MIN_INTERVAL = 1.1          # GenerateKeywordIdeas is capped at 1 request/second per customer id
_kp_last_call = [0.0]


@mcp.tool()
def keyword_ideas(
    customer_id: str,
    seeds: Optional[List[str]] = None,
    url: Optional[str] = None,
    geo_target_ids: Optional[List[int]] = None,
    language_id: int = 1000,
    limit: int = 200,
    include_zero_volume: bool = False,
) -> Dict[str, Any]:
    """Keyword Planner ideas with US-style monthly search volume and bid ranges. READ-ONLY: creates
    nothing, so there is no confirm flag. Costs one API operation per call, rate-limited to 1/second.

    seeds: up to 20 seed keywords; url: a landing page to seed from (either or both).
    geo_target_ids: geo target constant ids (default [2840] = United States); language_id: language
    constant id (default 1000 = English). Google Search network only (no partners).
    Returns ideas sorted by avg_monthly_searches desc, up to `limit`. Volumes are rounded 12-month
    averages and close variants share one aggregated number.
    """
    cid = _cid(customer_id)
    tool = "keyword_ideas"
    seeds = [s.strip() for s in (seeds or []) if s and s.strip()]
    payload = dict(seeds=seeds, url=url, geo_target_ids=geo_target_ids, language_id=language_id, limit=limit)
    if not seeds and not url:
        return _fail(tool, cid, payload, "give seeds, a url, or both")
    if len(seeds) > 20:
        return _fail(tool, cid, payload, "at most 20 seeds per call")
    c = _get_client()
    svc = c.get_service("KeywordPlanIdeaService")
    req = c.get_type("GenerateKeywordIdeasRequest")
    req.customer_id = cid
    req.language = f"languageConstants/{int(language_id)}"
    for g in (geo_target_ids or [2840]):
        req.geo_target_constants.append(f"geoTargetConstants/{int(g)}")
    req.include_adult_keywords = False
    req.keyword_plan_network = c.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
    if seeds and url:
        req.keyword_and_url_seed.url = url
        req.keyword_and_url_seed.keywords.extend(seeds)
    elif seeds:
        req.keyword_seed.keywords.extend(seeds)
    else:
        req.url_seed.url = url

    for attempt in range(4):
        wait = _KP_MIN_INTERVAL - (time.monotonic() - _kp_last_call[0])
        if wait > 0:
            time.sleep(wait)
        _kp_last_call[0] = time.monotonic()
        try:
            pages = svc.generate_keyword_ideas(request=req)
            ideas = []
            for r in pages:
                m = r.keyword_idea_metrics
                vol = int(m.avg_monthly_searches or 0)
                if vol == 0 and not include_zero_volume:
                    continue
                ideas.append({
                    "text": r.text,
                    "avg_monthly_searches": vol,
                    "competition": m.competition.name if m.competition else "",
                    "competition_index": int(m.competition_index or 0),
                    "low_top_of_page_bid": round((m.low_top_of_page_bid_micros or 0) / 1e6, 2),
                    "high_top_of_page_bid": round((m.high_top_of_page_bid_micros or 0) / 1e6, 2),
                })
            break
        except gexc.ResourceExhausted:
            time.sleep(2 ** attempt)
        except GoogleAdsException as exc:
            errs = [{"code": str(e.error_code).strip().replace("\n", " "), "message": e.message} for e in exc.failure.errors]
            out = {"status": "REJECTED_BY_API", "errors": errs, "request_id": exc.request_id}
            _audit(tool, cid, payload, out, applied=False)
            return out
    else:
        return _fail(tool, cid, payload, "rate limited after 4 attempts")
    ideas.sort(key=lambda x: -x["avg_monthly_searches"])
    out = {"status": "OK", "total_ideas": len(ideas), "returned": min(len(ideas), limit), "ideas": ideas[:limit]}
    _audit(tool, cid, payload, {"status": "OK", "total_ideas": len(ideas)}, applied=False)
    return out


@mcp.tool()
def audit_log_tail(n: int = 20) -> List[Dict[str, Any]]:
    """Return the last n entries of this server's local audit log (dry runs and applied changes)."""
    if not os.path.exists(AUDIT_LOG):
        return []
    with open(AUDIT_LOG, encoding="utf-8") as fh:
        lines = fh.readlines()[-max(1, min(n, 200)):]
    return [json.loads(l) for l in lines]


def _fetch_image_url(url: str) -> Union[bytes, str]:
    """Fetch an https image with SSRF guards. Returns bytes, or an error string."""
    import ipaddress
    import socket
    import urllib.parse
    import urllib.request

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        return "image_url must be https"
    host = parsed.hostname or ""
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return f"cannot resolve {host}: {exc}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return f"{host} resolves to a non-public address; refusing to fetch"

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: ANN002, ANN003
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "google-ads-write-mcp"})
    try:
        with opener.open(req, timeout=20) as resp:
            data = resp.read(_MAX_IMAGE_BYTES + 1)
    except Exception as exc:  # includes HTTPError for redirects (disabled)
        return f"fetch failed: {exc}"
    if len(data) > _MAX_IMAGE_BYTES:
        return f"image exceeds 5 MB"
    return data


@mcp.tool()
def upload_image_asset(
    customer_id: str,
    name: str,
    image_url: Optional[str] = None,
    image_base64: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Upload one image into the account's asset library as an IMAGE asset (no campaign link).

    Pass exactly one source: image_url (https only; public hosts only, redirects refused,
    5 MB cap) - the right choice for anything over ~100 KB - or image_base64 (standard
    base64, no data: prefix; practical only for small files, since the encoded content
    travels inside the tool call). PNG or JPEG. Returns the asset resource name and
    detected dimensions; link it afterwards with add_image_assets via
    asset_resource_name (campaign level or ad_group_id). Google rejects bytes identical
    to an existing asset with DUPLICATE_ASSET - reuse that asset's resource name instead.
    Dry run unless confirm=true."""
    import base64
    import hashlib

    cid = _cid(customer_id)
    tool = "upload_image_asset"
    payload: Dict[str, Any] = dict(name=name, source="url" if image_url else "base64")
    if bool(image_url) == bool(image_base64):
        return _fail(tool, cid, payload, "pass exactly one of image_url or image_base64")
    if image_url:
        payload["image_url"] = image_url
        data = _fetch_image_url(image_url)
        if isinstance(data, str):
            return _fail(tool, cid, payload, data)
    else:
        try:
            data = base64.b64decode(image_base64, validate=True)
        except Exception as exc:
            return _fail(tool, cid, payload, f"invalid base64: {exc}")
        if len(data) > _MAX_IMAGE_BYTES:
            return _fail(tool, cid, payload, "image exceeds 5 MB")
    dims = _image_dims(data)
    if not dims:
        return _fail(tool, cid, payload, "not a readable PNG/JPEG image")
    payload["bytes"] = len(data)
    payload["sha256"] = hashlib.sha256(data).hexdigest()
    payload["dims"] = f"{dims[0]}x{dims[1]}"
    c = _get_client()
    op = c.get_type("MutateOperation")
    asset = op.asset_operation.create
    asset.name = name
    asset.type_ = c.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = data
    out = _run_mutate(tool, cid, [op], confirm, payload)
    out["bytes"] = len(data)
    out["dims"] = payload["dims"]
    out["sha256"] = payload["sha256"]
    return out


@mcp.tool()
def set_campaign_conversion_goals(
    customer_id: str,
    campaign_id: str,
    goals: List[Dict[str, Any]],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Set campaign-specific conversion goals by toggling the biddable flag per goal.

    goals: [{"category": "PURCHASE", "origin": "WEBSITE", "biddable": true}, ...] -
    category is a ConversionActionCategory (PURCHASE, SIGNUP, SUBMIT_LEAD_FORM, ...),
    origin a ConversionOrigin (WEBSITE, APP, ...). Toggling any goal switches the
    campaign from account-default goals to campaign-specific goals, so to bid only on
    purchases pass PURCHASE/WEBSITE biddable=true AND every other goal the account has
    as biddable=false (list them via the read MCP: SELECT campaign_conversion_goal.category,
    campaign_conversion_goal.origin, campaign_conversion_goal.biddable FROM
    campaign_conversion_goal WHERE campaign.id = <id>). One atomic request; dry run
    unless confirm=true."""
    cid = _cid(customer_id)
    tool = "set_campaign_conversion_goals"
    payload = dict(campaign_id=str(campaign_id), goals=goals)
    if not goals:
        return _fail(tool, cid, payload, "no goals given")
    c = _get_client()
    ops = []
    for g in goals:
        cat = str(g.get("category", "")).upper()
        origin = str(g.get("origin", "")).upper()
        # client.enums.<X>Enum is already the inner enum class (same pattern as set_status).
        if cat not in c.enums.ConversionActionCategoryEnum.__members__:
            return _fail(tool, cid, payload, f"unknown category {cat!r}")
        if origin not in c.enums.ConversionOriginEnum.__members__:
            return _fail(tool, cid, payload, f"unknown origin {origin!r}")
        if "biddable" not in g:
            return _fail(tool, cid, payload, "each goal needs a boolean 'biddable'")
        op = c.get_type("MutateOperation")
        goal = op.campaign_conversion_goal_operation.update
        goal.resource_name = f"customers/{cid}/campaignConversionGoals/{campaign_id}~{cat}~{origin}"
        goal.biddable = bool(g["biddable"])
        # Explicit mask: proto3 drops False from generated masks (same pitfall as
        # device modifier 0.0), which would silently skip biddable=false updates.
        op.campaign_conversion_goal_operation.update_mask.paths.append("biddable")
        ops.append(op)
    return _run_mutate(tool, cid, ops, confirm, payload)


def _ads_errors(exc: GoogleAdsException) -> List[Dict[str, str]]:
    return [
        {
            "code": str(err.error_code).strip().replace("\n", " "),
            "message": err.message,
            "field_path": ".".join(fe.field_name for fe in err.location.field_path_elements),
        }
        for err in exc.failure.errors
    ]


@mcp.tool()
def create_experiment(
    customer_id: str,
    base_campaign_id: str,
    name: str,
    start_date: str,
    end_date: str,
    traffic_split_percent: int = 50,
    description: str = "",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Create a SEARCH_CUSTOM A/B experiment on an existing Search campaign.

    Applied in two steps: the experiment shell, then a control arm (the base campaign)
    and a treatment arm with traffic_split_percent of traffic. Google builds a DRAFT
    copy of the base campaign for the treatment arm; its resource name is returned as
    treatment_draft_campaign — edit that draft with the other tools, then call
    schedule_experiment to start serving. Dates are YYYY-MM-DD (start must be in the
    future). Dry run validates only the experiment shell; nothing is created."""
    cid = _cid(customer_id)
    tool = "create_experiment"
    payload = dict(base_campaign_id=str(base_campaign_id), name=name, start=start_date, end=end_date,
                   split=traffic_split_percent)
    if not 1 <= int(traffic_split_percent) <= 99:
        return _fail(tool, cid, payload, "traffic_split_percent must be 1..99")
    c = _get_client()
    exp_svc = c.get_service("ExperimentService")
    op = c.get_type("ExperimentOperation")
    exp = op.create
    exp.name = name
    exp.description = description
    exp.suffix = "[exp]"
    exp.type_ = c.enums.ExperimentTypeEnum.SEARCH_CUSTOM
    exp.status = c.enums.ExperimentStatusEnum.SETUP
    exp.start_date = start_date
    exp.end_date = end_date
    exp_req = c.get_type("MutateExperimentsRequest")
    exp_req.customer_id = cid
    exp_req.operations.append(op)
    exp_req.validate_only = not confirm
    try:
        exp_resp = exp_svc.mutate_experiments(request=exp_req)
    except GoogleAdsException as exc:
        out = {"status": "REJECTED_BY_API", "dry_run": not confirm, "errors": _ads_errors(exc), "request_id": exc.request_id}
        _audit(tool, cid, payload, out, applied=False)
        return out
    if not confirm:
        out = {
            "status": "VALIDATED_DRY_RUN", "dry_run": True, "operations": 1,
            "note": "Nothing was created. The experiment shell validated; arms are only created on confirm=true "
                    "(they need the real experiment resource). Re-run with confirm=true to apply.",
        }
        _audit(tool, cid, payload, out, applied=False)
        return out
    exp_rn = exp_resp.results[0].resource_name
    arm_svc = c.get_service("ExperimentArmService")
    base_rn = f"customers/{cid}/campaigns/{base_campaign_id}"
    ops = []
    for arm_name, is_control, split in (
        ("control", True, 100 - int(traffic_split_percent)),
        ("treatment", False, int(traffic_split_percent)),
    ):
        aop = c.get_type("ExperimentArmOperation")
        arm = aop.create
        arm.experiment = exp_rn
        arm.name = arm_name
        arm.control = is_control
        arm.traffic_split = split
        if is_control:
            arm.campaigns.append(base_rn)
        ops.append(aop)
    arm_req = c.get_type("MutateExperimentArmsRequest")
    arm_req.customer_id = cid
    arm_req.operations.extend(ops)
    arm_req.response_content_type = c.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE
    try:
        arm_resp = arm_svc.mutate_experiment_arms(request=arm_req)
    except GoogleAdsException as exc:
        out = {"status": "PARTIAL_FAILURE", "experiment": exp_rn, "errors": _ads_errors(exc),
               "request_id": exc.request_id,
               "note": "Experiment shell was created but the arms were rejected; fix and add arms, or remove the experiment in the UI."}
        _audit(tool, cid, payload, out, applied=True)
        return out
    draft = ""
    arms = []
    for r in arm_resp.results:
        arms.append(r.resource_name)
        arm_obj = getattr(r, "experiment_arm", None)
        if arm_obj is not None and not arm_obj.control and arm_obj.in_design_campaigns:
            draft = arm_obj.in_design_campaigns[0]
    out = {
        "status": "APPLIED", "dry_run": False, "operations": 1 + len(ops),
        "experiment": exp_rn, "arms": arms, "treatment_draft_campaign": draft,
        "note": "Edit the treatment draft campaign with the other tools, then run schedule_experiment.",
    }
    _audit(tool, cid, payload, out, applied=True)
    return out


def _experiment_lifecycle(tool: str, customer_id: str, experiment_id: str, confirm: bool) -> Dict[str, Any]:
    """Shared body for schedule/end/promote: same dry-run, error and audit handling."""
    cid = _cid(customer_id)
    payload = dict(experiment_id=str(experiment_id))
    c = _get_client()
    svc = c.get_service("ExperimentService")
    rn = f"customers/{cid}/experiments/{experiment_id}"
    try:
        if tool == "schedule_experiment":
            req = c.get_type("ScheduleExperimentRequest")
            req.resource_name = rn
            req.validate_only = not confirm
            svc.schedule_experiment(request=req)
        elif tool == "end_experiment":
            req = c.get_type("EndExperimentRequest")
            req.experiment = rn
            req.validate_only = not confirm
            svc.end_experiment(request=req)
        else:
            req = c.get_type("PromoteExperimentRequest")
            req.resource_name = rn
            req.validate_only = not confirm
            svc.promote_experiment(request=req)
    except GoogleAdsException as exc:
        out = {"status": "REJECTED_BY_API", "dry_run": not confirm, "errors": _ads_errors(exc), "request_id": exc.request_id}
        _audit(tool, cid, payload, out, applied=False)
        return out
    out = {
        "status": "APPLIED" if confirm else "VALIDATED_DRY_RUN",
        "dry_run": not confirm,
        "operations": 1,
        "experiment": rn,
    }
    if not confirm:
        out["note"] = "Nothing was changed. Google validated the request. Re-run with confirm=true to apply."
    elif tool != "end_experiment":
        out["note"] = "Accepted; Google finishes this asynchronously. Check experiment.status via the read MCP."
    _audit(tool, cid, payload, out, applied=confirm)
    return out


@mcp.tool()
def schedule_experiment(customer_id: str, experiment_id: str, confirm: bool = False) -> Dict[str, Any]:
    """Start a SETUP experiment serving: materializes the treatment draft campaign and begins the
    traffic split on the scheduled dates. Dry run unless confirm=true. Asynchronous on Google's side."""
    return _experiment_lifecycle("schedule_experiment", customer_id, experiment_id, confirm)


@mcp.tool()
def end_experiment(customer_id: str, experiment_id: str, confirm: bool = False) -> Dict[str, Any]:
    """End a running experiment: the treatment arm stops serving and the base campaign resumes full
    traffic. Dry run unless confirm=true."""
    return _experiment_lifecycle("end_experiment", customer_id, experiment_id, confirm)


@mcp.tool()
def promote_experiment(customer_id: str, experiment_id: str, confirm: bool = False) -> Dict[str, Any]:
    """Promote a running experiment: the treatment settings replace the base campaign (the winner
    becomes the live campaign). Dry run unless confirm=true. Asynchronous on Google's side."""
    return _experiment_lifecycle("promote_experiment", customer_id, experiment_id, confirm)


def main() -> None:
    """Console entrypoint. Modes:
      google-ads-write-mcp                      run the MCP server on stdio
      google-ads-write-mcp --check              print resolved config (no secrets) and exit
      google-ads-write-mcp --list-tools         list tool names and exit
      google-ads-write-mcp --call TOOL ARGS.json  run one tool with JSON arguments and print the result
    """
    summary = _configure()
    if "--check" in sys.argv:
        print(json.dumps(summary, indent=1))
        missing = [k for k, v in summary.items() if str(v).startswith("MISSING")]
        sys.exit(1 if missing else 0)
    if "--list-tools" in sys.argv:
        import asyncio
        for t in asyncio.run(mcp.list_tools()):
            print(t.name)
        sys.exit(0)
    if "--call" in sys.argv:
        i = sys.argv.index("--call")
        tool_name, args_path = sys.argv[i + 1], sys.argv[i + 2]
        fn = globals()[tool_name]
        with open(args_path) as fh:
            kwargs = json.load(fh)
        print(json.dumps(fn(**kwargs), indent=1, default=str))
        sys.exit(0)
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
