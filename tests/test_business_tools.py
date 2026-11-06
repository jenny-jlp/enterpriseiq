from collections import defaultdict
from decimal import Decimal
import json

from enterpriseiq.tools import BusinessTools, tool_definitions


def raw_records(bundle, path):
    return json.loads(bundle.files[path], parse_float=Decimal)


def test_exposure_matches_independent_raw_data_baseline(populated, bundle):
    # Independent baseline uses raw arrays, not imported relations or tool SQL.
    accounts = {r["account_id"]: r for r in raw_records(bundle, "crm_json_data/accounts.json")}
    grouped, all_accounts, all_tickets = defaultdict(set), set(), set()
    for ticket in raw_records(bundle, "crm_json_data/tickets.json"):
        if ticket["status"] == "open" and ticket["priority"] in ("p0", "p1"):
            all_accounts.add(ticket["account_id"])
            all_tickets.add(ticket["ticket_id"])
            for component in ticket["components"]:
                grouped[component].add(ticket["account_id"])
    result = BusinessTools(populated).execute("calculate_arr_exposure", {})
    assert result["status"] == "ok" and result["complete"] is True
    assert len(result["groups"]) == 12
    for group in result["groups"]:
        expected_ids = grouped[group["part_id"]]
        assert set(group["account_ids"]) == expected_ids
        assert Decimal(group["arr"]) == sum(Decimal(accounts[key]["arr"]) for key in expected_ids)
        assert {row["account_id"]: row["account_name"] for row in group["accounts"]} == {
            key: accounts[key]["account_name"] for key in expected_ids}
        assert group["part_name"]
    assert set(result["global"]["account_ids"]) == all_accounts
    assert set(result["global"]["ticket_ids"]) == all_tickets
    assert Decimal(result["global"]["arr"]) == sum(Decimal(accounts[key]["arr"]) for key in all_accounts)
    assert next(g for g in result["groups"] if g["part_id"] == "PART-010")["arr"] == "732000"
    assert result["currency"] == "UNSPECIFIED_BENCHMARK"
    assert len({ref["evidence_id"] for ref in result["evidence"]}) == len(result["evidence"])


def test_ticket_pagination_and_candidate_issues_match_raw_baseline(populated, bundle):
    tools = BusinessTools(populated)
    tickets = raw_records(bundle, "crm_json_data/tickets.json")
    expected = {t["ticket_id"] for t in tickets if t["status"] == "open" and t["priority"] in ("p0", "p1")}
    seen, offset = [], 0
    while True:
        page = tools.execute("list_open_tickets", {"offset": offset, "limit": 10})
        assert page["status"] == "ok" and page["complete"] is False
        assert page["total_matches"] == len(expected) == 31
        seen.extend(item["data"]["ticket_id"] for item in page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) and set(seen) == expected
    issues = raw_records(bundle, "pm_json_data/issues.json")
    parts = {part for t in tickets if t["ticket_id"] in expected for part in t["components"]}
    for part in parts:
        wanted = {i["issue_id"] for i in issues if part in i["components"] and i["status"] == "in_progress" and i["priority"] in ("high", "highest")}
        result = tools.execute("find_component_issues", {"part_ids": [part], "limit": 100})
        assert result["complete"] is True
        assert {i["data"]["issue_id"] for i in result["items"]} == wanted
        assert result["relation_type"] == "shared_component"


def test_fractional_arr_is_exact_and_missing_arr_is_incomplete(populated):
    tools = BusinessTools(populated)
    populated.execute("""UPDATE enterpriseiq.records SET data=jsonb_set(data,'{arr}','432000.10')
        WHERE source_system='crm' AND entity_type='account' AND source_id='ACC-013'""")
    result = tools.execute("calculate_arr_exposure", {})
    assert next(g for g in result["groups"] if g["part_id"] == "PART-010")["arr"] == "732000.10"
    populated.execute("""UPDATE enterpriseiq.records SET data=data-'arr'
        WHERE source_system='crm' AND entity_type='account' AND source_id='ACC-013'""")
    result = tools.execute("calculate_arr_exposure", {})
    assert result["complete"] is False
    assert result["global"]["arr"] is None
    assert result["global"]["missing_arr_account_ids"] == ["ACC-013"]


def test_rejects_raw_queries_unknown_tools_and_injection(populated):
    tools = BusinessTools(populated)
    assert tools.execute("query_sql", {"sql": "DROP TABLE records"})["error"]["code"] == "unknown_tool"
    assert tools.execute("list_open_tickets", {"sql": "SELECT *"})["error"]["code"] == "invalid_arguments"
    assert tools.execute("list_open_tickets", {"limit": 101})["error"]["code"] == "invalid_arguments"
    assert tools.execute("list_open_tickets", {"priorities": ["high"]})["error"]["code"] == "invalid_arguments"
    assert tools.execute("get_record", {"entity_type": "issue", "source_id": "' OR 1=1 --"})["error"]["code"] == "not_found"
    assert tools.execute("search_accounts", {"name": "%"})["total_matches"] == 0
    assert tools.execute("get_record", {"entity_type": "issue", "source_id": "ISS-017"})["status"] == "ok"
    assert BusinessTools(populated, "nonexistent").execute("calculate_arr_exposure", {})["error"]["code"] == "snapshot_not_found"


def test_vantara_profile_opportunity_and_document_source_ranges(populated, bundle):
    tools = BusinessTools(populated)
    account = tools.execute("search_accounts", {"name": "vantara"})
    assert account["complete"] is True and account["items"][0]["data"]["account_id"] == "ACC-013"
    opps = tools.execute("list_account_opportunities", {"account_id": "ACC-013"})
    opp = next(item for item in opps["items"] if item["data"]["opportunity_id"] == "OPP-006")
    assert opp["data"]["acv"] == 280000 and opp["data"]["currency"] == "USD"
    docs = tools.execute("list_documents", {"account_id": "ACC-013"})
    assert docs["items"][0]["data"]["transcript_id"] == "TRN-001"
    document = tools.execute("read_document", {"entity_type": "transcript", "source_id": "TRN-001", "start_line": 3, "line_count": 5})
    assert document["complete"] is False and document["next_line"] == 8
    ref = document["evidence"]
    raw_lines = bundle.files[ref["source_path"]].decode().splitlines()
    assert document["text"] == "\n".join(f"{i}: {raw_lines[i-1]}" for i in range(3, 8))
    assert ref["start_line"] == 3 and ref["end_line"] == 7
    assert tools.execute("read_document", {"entity_type": "transcript", "source_id": "TRN-001", "start_line": 100000})["status"] == "error"


def test_tool_definitions_have_closed_typed_schemas():
    definitions = tool_definitions()
    assert len(definitions) == 9
    for tool in definitions:
        assert tool["function"]["parameters"]["additionalProperties"] is False
