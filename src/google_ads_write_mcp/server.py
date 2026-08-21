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


def _campaign_asset_ops(c: GoogleAdsClient, cid: str, campaign_id: str, assets: list, field_type: str):
    """Create N assets with temp ids and link each to the campaign, atomically."""
    ops = []
    camp_rn = c.get_service("CampaignService").campaign_path(cid, str(campaign_id))
    for i, fill in enumerate(assets, start=1):
        temp_rn = f"customers/{cid}/assets/-{i}"
        op = c.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = temp_rn
        fill(asset)
        ops.append(op)
        link = c.get_type("MutateOperation")
        ca = link.campaign_asset_operation.create
        ca.campaign = camp_rn
        ca.asset = temp_rn
        ca.field_type = getattr(c.enums.AssetFieldTypeEnum, field_type)
        ops.append(link)
    return ops


@mcp.tool()
def add_sitelinks(customer_id: str, campaign_id: str, sitelinks: List[Dict[str, str]], confirm: bool = False) -> Dict[str, Any]:
    """Create sitelink assets and attach them to a campaign.
    sitelinks: [{"link_text": <=25 chars, "final_url": "https://...", "description1": <=35, "description2": <=35}, ...]
    Dry run unless confirm=true."""
    cid = _cid(customer_id)
    payload = dict(campaign_id=campaign_id, sitelinks=sitelinks)
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

    ops = _campaign_asset_ops(c, cid, campaign_id, [mk(s) for s in sitelinks], "SITELINK")
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
    negative_keywords: Optional[List[Dict[str, str]]] = None,
    device_bid_modifiers: Optional[Dict[str, float]] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Create a complete Search campaign in ONE atomic mutate: budget, campaign, location and
    language criteria, campaign negatives, and every ad group with its keywords and one RSA.

    The campaign gets the managed label (default "claude-managed"; created in the account on first
    use) so remove_entity can later act on it. It is created PAUSED; enable it with set_status.
    locations: geo target constant ids (United States = 2840). languages: language constant ids
    (English = 1000). bidding: MAXIMIZE_CONVERSIONS (optional target_cpa) or MAXIMIZE_CLICKS.
    Network: Google Search only (no partners, no Display).
    ad_groups: [{"name", "final_url", "keywords": [{"text","match_type"}], "headlines": [...],
                 "descriptions": [...], "path1", "path2", "status", "final_url_suffix",
                 "negative_keywords": [...]}, ...]
    negative_keywords: [{"text", "match_type"}] at campaign level.
    device_bid_modifiers: {"DESKTOP": -100, "TABLET": -100} percent adjustments (-100 excludes a device;
    mobile-only = desktop and tablet at -100).
    Extensions (sitelinks, callouts, snippets, images) are separate tools - call them with the
    returned campaign id. Dry run unless confirm=true.
    """
    cid = _cid(customer_id)
    payload = dict(name=name, daily_budget=daily_budget, locations=locations, languages=languages,
                   bidding=bidding, target_cpa=target_cpa, device_bid_modifiers=device_bid_modifiers,
                   ad_groups=[{"name": g.get("name"), "keywords": len(g.get("keywords") or [])} for g in ad_groups],
                   negative_keywords=len(negative_keywords or []))
    tool = "create_search_campaign"
    if not name.strip():
        return _fail(tool, cid, payload, "campaign name is empty")
    if not (0 < daily_budget < 100_000):
        return _fail(tool, cid, payload, "daily_budget out of range")
    if not locations or not languages:
        return _fail(tool, cid, payload, "locations and languages are both required")
    if not ad_groups:
        return _fail(tool, cid, payload, "at least one ad group is required")
    if bidding not in ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CLICKS"):
        return _fail(tool, cid, payload, "bidding must be MAXIMIZE_CONVERSIONS or MAXIMIZE_CLICKS")
    if target_cpa is not None and not (0 < target_cpa < 10_000):
        return _fail(tool, cid, payload, "target_cpa out of range")
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
        camp.maximize_clicks.cpc_bid_ceiling_micros = 0
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
    if device_bid_modifiers:
        try:
            ops.extend(_device_ops(c, cid, campaign_rn, {}, {k.upper(): v for k, v in device_bid_modifiers.items()}))
        except ValueError as exc:
            return _fail(tool, cid, payload, str(exc))
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
    confirm: bool = False,
) -> Dict[str, Any]:
    """Upload image files as image assets and attach them to a Search campaign as AD_IMAGE.

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
    payload: Dict[str, Any] = dict(campaign_id=campaign_id, images=[])
    if not images:
        return _fail(tool, cid, payload, "no images given")
    c = _get_client()
    camp_rn = c.get_service("CampaignService").campaign_path(cid, str(campaign_id))
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
        ca = link.campaign_asset_operation.create
        ca.campaign = camp_rn
        ca.asset = asset_rn
        ca.field_type = c.enums.AssetFieldTypeEnum.AD_IMAGE
        ops.append(link)
    return _run_mutate(tool, cid, ops, confirm, payload)


# --------------------------------------------------------------------------- removal (guarded)
@mcp.tool()
def remove_entity(customer_id: str, resource_name: Union[str, List[str]], confirm: bool = False) -> Dict[str, Any]:
    """PERMANENTLY remove a campaign, ad group, ad (adGroupAds/...), keyword (adGroupCriteria/...) or
    campaign criterion such as a campaign negative keyword (campaignCriteria/...).

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
    }
    matched = {k for n in names for k in kinds if k in n}
    if len(matched) != 1:
        return _fail(tool, cid, payload, "all resource names must be of one supported kind: campaigns/, adGroups/, "
                                         "adGroupAds/, adGroupCriteria/ or campaignCriteria/")
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



# --------------------------------------------------------------------------- device bid modifiers
# Google's fixed criterion ids for device criteria.
_DEVICE_IDS = {"MOBILE": 30000, "TABLET": 30001, "DESKTOP": 30002}


def _device_ops(c: GoogleAdsClient, cid: str, campaign_rn: str, existing: Dict[str, str],
                modifiers: Dict[str, float]):
    """modifiers: {"DESKTOP": -100, "TABLET": -100, "MOBILE": 0} as percent adjustments.
    -100 excludes the device. Creates missing device criteria, updates existing ones."""
    ops = []
    for dev, pct in modifiers.items():
        if pct is None:
            continue
        if dev not in _DEVICE_IDS:
            raise ValueError(f"unknown device {dev!r}; use DESKTOP, TABLET, MOBILE")
        if not (-100 <= pct <= 900):
            raise ValueError(f"{dev}: adjustment must be between -100 and +900 percent")
        modifier = round(1 + pct / 100.0, 2)
        op = c.get_type("MutateOperation")
        if dev in existing:
            crit = op.campaign_criterion_operation.update
            crit.resource_name = existing[dev]
            crit.bid_modifier = modifier
            op.campaign_criterion_operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, crit._pb))
        else:
            crit = op.campaign_criterion_operation.create
            crit.campaign = campaign_rn
            crit.device.type_ = getattr(c.enums.DeviceEnum, dev)
            crit.bid_modifier = modifier
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
    ga = c.get_service("GoogleAdsService")
    campaign_rn = c.get_service("CampaignService").campaign_path(cid, str(campaign_id))
    q = (f"SELECT campaign_criterion.resource_name, campaign_criterion.device.type, campaign_criterion.bid_modifier "
         f"FROM campaign_criterion WHERE campaign.id = {int(campaign_id)} AND campaign_criterion.type = 'DEVICE'")
    existing = {r.campaign_criterion.device.type_.name: r.campaign_criterion.resource_name
                for r in ga.search(customer_id=cid, query=q)}
    try:
        ops = _device_ops(c, cid, campaign_rn, existing, wanted)
    except ValueError as exc:
        return _fail(tool, cid, payload, str(exc))
    return _run_mutate(tool, cid, ops, confirm, payload)


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
