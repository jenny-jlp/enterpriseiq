"""Small, inspectable task contracts; no task IDs or benchmark answers."""

import re
import json

from enterpriseiq.dataset import SNAPSHOT_ID


def normalize_question(question):
    # Normalize domain synonyms before interpreting scope; never normalize source text.
    replacements = {
        "年经常性收入": "ARR", "年度经常性收入": "ARR",
        "年 recurring revenue": "ARR", "annual recurring revenue": "ARR",
        "产品领域": "产品组件", "产品模块": "产品组件", "product areas": "components",
        "一览": "全部", "每一张": "每张", "逐个": "逐一", "完整清单": "全部工单清单",
        "未关闭": "开放", "客户名": "客户名称",
    }
    for old, new in replacements.items():
        question = re.sub(re.escape(old), new, question, flags=re.I)
    return question


def semantic_scope(question):
    question = normalize_question(question)
    money = bool(re.search(r"ARR|收入|敞口", question, re.I))
    product = bool(re.search(r"组件|产品区域|产品组件|PART[- ]?ID|product area|component", question, re.I))
    geographic = bool(re.search(r"地理|地理区域|地区|地域|国家|省份|geograph|\bregion\b", question, re.I))
    if re.search(r"(?:不是|不要按|非|不按|而非|不涉及|并非).{0,8}(?:地理|地区|地域)|not.{0,12}geograph", question, re.I):
        geographic = False
    if money and geographic:
        return "unsupported_geographic_arr"
    if money and re.search(r"区域|area", question, re.I) and not product:
        return "clarification_required"
    aggregate = money and bool(re.search(r"去重|汇总|敞口|总额|总计|最高|前三|前\s*[1-9一二三四五]|排名|统计|\btop\b", question, re.I))
    return "direct_component" if aggregate and product else "global" if aggregate else "investigation"


def task_requirements(question):
    question = normalize_question(question)
    scope = semantic_scope(question)
    tickets = bool(re.search(r"工单|tickets?", question, re.I))
    components = bool(re.search(r"组件|components?|product area", question, re.I))
    customers = bool(re.search(r"客户|accounts?|customers?", question, re.I))
    collection = bool(re.search(r"逐一|每[个张条]|全部|所有|for each|\ball\b|\bevery\b", question, re.I))
    tables = []
    if scope == "investigation" and tickets and components and customers and collection:
        tables.append("ticket_contexts")
    if scope == "direct_component" and (collection or re.search(r"名称|数量|工单数|\bname\b|\bcount\b", question, re.I)):
        tables.append("arr_exposure")
    match = re.search(r"(?:前\s*|top\s*|最高的?\s*)(10|[1-9一二三四五六七八九十])", question, re.I)
    top = int(match[1]) if match and match[1].isdigit() else "一二三四五六七八九十".index(match[1])+1 if match else None
    priorities = sorted({p.lower() for p in re.findall(r"(?<![A-Za-z0-9])p[0-3](?![A-Za-z0-9])", question, re.I)})
    return {"required_tables": tables, "top_n": top, "priorities": priorities or None}


def task_contract(question):
    normalized = normalize_question(question)
    return {"version": 1, "snapshot_id": SNAPSHOT_ID, "question": question,
            "scope": semantic_scope(question), **task_requirements(question),
            "entity_ids": sorted(set(re.findall(r"\b(?:ACC|OPP|TKT|ISS|PART|TRN)-\d+\b", question))),
            "requires_document": bool(re.search(r"通话|会议|QBR|transcript", normalized, re.I)),
            "requires_eta": bool(re.search(r"ETA|承诺.*(?:日期|修复)", normalized, re.I)),
            "requires_open_questions": bool(re.search(r"待确认|待核实", normalized)),
            "requires_source_actions": bool(re.search(r"推进|行动|条件|next steps?|actions?|advance|proceed", normalized, re.I)
                                          and re.search(r"通话|会议|QBR|transcript", normalized, re.I)),
            "limits": ["fixed_snapshot", "shared_component_is_candidate", "preserve_currency"]}


def investigation_gaps(state, validation=None):
    """Observed coverage is not verified task completion. Preserve both dimensions."""
    from enterpriseiq.reliability import coverage_gaps, tool_results

    contract = state.get("task_contract") or task_contract(state["question"])
    calls = tool_results(state)
    ok = [c for c in calls.values() if c.get("result", {}).get("status") == "ok"]
    requirements = [("answer", bool(state.get("evidence")))]
    names = {c["name"] for c in ok}
    for table in contract["required_tables"]:
        requirements.append((table, ("list_ticket_contexts" if table == "ticket_contexts" else "calculate_arr_exposure") in names))
    if contract["requires_document"]:
        requirements.append(("original_document", "read_document" in names))
    if contract["requires_eta"]:
        requirements.append(("explicit_eta", any(re.search(r"ETA|estimated|committed", json.dumps(c, ensure_ascii=False), re.I) for c in ok)))
    passed = validation is not None and validation["report_validation"] == "passed"
    ledger = [{"code": key, "question": contract["question"] if key == "answer" else key,
               "status": "verified" if passed else "pending_validation" if observed else "unresolved"}
              for key, observed in requirements]
    ledger.extend({**g, "status": "unresolved"} for g in (validation["report"]["gaps"] if validation else coverage_gaps(state)))
    if validation:
        ledger.extend({"code": "open_question", "question": q, "status": "unresolved"}
                      for q in validation["report"].get("open_questions_unverified", []))
    return ledger
