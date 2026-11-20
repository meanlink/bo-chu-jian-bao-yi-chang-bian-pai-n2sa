"""停播归并的纯业务规则（仅依赖标准库，便于脱离 Web 栈复核）。

规则要点：
- 接线关系按生效日登记为快照；停播日使用“生效日不晚于当天”的最新一版；
- 下游停播能被上游停播日记解释时归因到上游，即正常中继截断；
- 同一天多条链路停播，若归因到同一上游，归入同一组并提示可能同源；
- 追溯路径上遇到应报日记的上游缺报，该组标待核并写明缺报对象与日期；
- 一条停播存在多条上游来源时全部列出，不擅自取其一。
"""

MISSING = "missing"
OUTAGE = "outage"
OK = "ok"
SIGNAL = "signal"

NOTE = {
    "relay": "下游停播由上游停播日记解释，按正常中继截断归因到上游。",
    "same_source": "同日多条链路停播且归因到同一上游，可能同源；请按原始播控日志核对上下级停机时间。",
    "multiple": "存在多个疑似源头，已全部列出；请按原始播控日志逐一核对。",
    "pending": "追溯路径上有上游缺报，先补齐原始播控日记再做同源判断。",
    "source": "",
}


def normalize_wiring(wiring):
    """整理接线快照，去重并保持登记顺序。"""
    wiring = wiring or {}
    edges, seen = [], set()
    for item in wiring.get("edges") or []:
        upstream = str(item.get("upstream") or "").strip()
        downstream = str(item.get("downstream") or "").strip()
        if not upstream or not downstream or upstream == downstream:
            continue
        if (upstream, downstream) not in seen:
            seen.add((upstream, downstream))
            edges.append((upstream, downstream))
    sources = []
    for name in wiring.get("sources") or []:
        name = str(name or "").strip()
        if name and name not in sources:
            sources.append(name)
    return edges, sources


def validate_wiring(edges, sources):
    """登记前校验；合法返回 None，否则返回中文错误。"""
    if not edges:
        return "至少登记一条接线关系"
    nodes = set(sources)
    for upstream, downstream in edges:
        nodes.add(upstream)
        nodes.add(downstream)
    if len(nodes) < 2:
        return "接线至少涉及两个节点"
    downstream_set = {downstream for _, downstream in edges}
    for source in sources:
        if source in downstream_set:
            return f"信号源 {source} 不能同时作为下游节点"
    parents = parent_map(edges)
    visiting, done = set(), set()

    def walk(node):
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for upstream in parents.get(node, ()):
            if walk(upstream):
                return True
        visiting.discard(node)
        done.add(node)
        return False

    for node in nodes:
        if walk(node):
            return "接线关系不能成环"
    return None


def parent_map(edges):
    """downstream -> [upstream, ...]，保持登记顺序。"""
    parents = {}
    for upstream, downstream in edges:
        bucket = parents.setdefault(downstream, [])
        if upstream not in bucket:
            bucket.append(upstream)
    return parents


def roots_of(node, parents):
    """向上追溯无上游的根节点；遇到环时返回空集。"""
    seen, out, stack = set(), set(), [node]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        upstreams = parents.get(current, ())
        if not upstreams:
            out.add(current)
        else:
            stack.extend(upstreams)
    return out


def interruption(row):
    if row is None:
        return None
    try:
        return float(row.get("interruption_min") or 0)
    except (TypeError, ValueError):
        return 0.0


def _resolve(node, records, parents, declared, known, threshold, stack):
    """沿上游追溯一个停播节点的证据。

    返回 dict:
      confirmed  有停播日记证实、且自身不再由上游解释的停播祖先；
      signals    追溯路径上遇到、按登记不产生播控日记的信号源（仅作提示）；
      missing    应报日记却缺报的上游，元素为 (节点, 原因)；
      traces     逐条追溯证据。
    """
    if node in stack:
        return {"confirmed": set(), "signals": set(), "missing": [],
                "traces": [{"node": node, "state": "cycle", "interruption_min": None}]}
    upstreams = parents.get(node, ())
    if not upstreams:
        return {"confirmed": set(), "signals": set(), "missing": [], "traces": []}

    result = {"confirmed": set(), "signals": set(), "missing": [], "traces": []}
    for upstream in upstreams:
        row = records.get(upstream)
        if row is None:
            if upstream in declared:
                result["signals"].add(upstream)
                result["traces"].append({"node": upstream, "state": SIGNAL, "interruption_min": None})
            else:
                reason = "缺停播报" if known is None or upstream in known else "未提供播控日记"
                result["missing"].append((upstream, reason))
                result["traces"].append({"node": upstream, "state": MISSING, "interruption_min": None})
            continue
        if (interruption(row) or 0) > threshold:
            deeper = _resolve(upstream, records, parents, declared, known, threshold, stack + (node,))
            result["signals"] |= deeper["signals"]
            result["missing"] += deeper["missing"]
            # 上游自身停播；若它能被更上游解释，则源头继续上移。
            origin = deeper["confirmed"] or {upstream}
            result["confirmed"] |= origin
            result["traces"].append({
                "node": upstream,
                "state": OUTAGE,
                "interruption_min": round(interruption(row) or 0, 4),
                "origin": sorted(origin),
            })
            result["traces"] += deeper["traces"]
            continue
        result["traces"].append({"node": upstream, "state": OK, "interruption_min": 0.0})
    return result


def _related(a, b, evidence):
    """两条停播是否可能同源：共享已证实源头、互为上下游，或共同缺同一段上游日记。

    没有任何上游日记可证实时，共同的信号根只作为“可能同源”的线索，
    因此只对两条都没有已证实源头的链路生效。
    """
    ea, eb = evidence[a], evidence[b]
    if ea["confirmed"] & eb["confirmed"]:
        return True
    if a in eb["confirmed"] or b in ea["confirmed"]:
        return True
    if not ea["confirmed"] and not eb["confirmed"] and ea["signals"] & eb["signals"]:
        return True
    if {name for name, _ in ea["missing"]} & {name for name, _ in eb["missing"]}:
        return True
    return False


def _components(nodes, evidence):
    nodes = sorted(nodes)
    used, groups = set(), []
    for node in nodes:
        if node in used:
            continue
        used.add(node)
        component, stack = [], [node]
        while stack:
            current = stack.pop()
            component.append(current)
            for other in nodes:
                if other not in used and _related(current, other, evidence):
                    used.add(other)
                    stack.append(other)
        groups.append(sorted(component))
    return groups


def _member(node, day, records, evidence, threshold):
    row = records[node]
    ev = evidence[node]
    confirmed = sorted(ev["confirmed"])
    signals = sorted(ev["signals"])
    missing = list(dict.fromkeys(ev["missing"]))

    if missing:
        attribution = "pending"
        pending_reason = "；".join(f"{name} 在 {day} {reason}" for name, reason in missing)
    else:
        pending_reason = ""
        if len(confirmed) > 1:
            attribution = "multiple"
        elif confirmed:
            attribution = "relay"
        else:
            attribution = "source"

    # 疑似源头：有日记证实的上游优先；缺报时把缺报对象列为待核源头；
    # 都没有则说明停播从本链路开始。
    items = [{"node": name, "basis": "diary", "confirmed": True} for name in confirmed]
    for name, reason in missing:
        items.append({"node": name, "basis": "missing", "confirmed": False, "reason": reason})
    if not items:
        items = [{"node": node, "basis": "diary", "confirmed": True}]

    duration_note = ""
    if attribution in ("relay", "multiple"):
        own = interruption(row) or 0
        biggest = max((interruption(records.get(name)) or 0) for name in confirmed) if confirmed else 0
        if biggest and own > biggest + 0.05:
            duration_note = (
                f"本链路停播 {own:g} 分钟，长于最长上游停播 {biggest:g} 分钟；"
                "中继截断不能解释全部时长，需另查本链路。"
            )

    return {
        "link": node,
        "interruption_min": round(interruption(row) or 0, 4),
        "planned_min": row.get("planned_min"),
        "aired_min": row.get("aired_min"),
        "status": "pending" if missing else "ready",
        "attribution": attribution,
        "suspected_sources": items,
        "signal_roots": signals,
        "missing_upstreams": [name for name, _ in missing],
        "pending_reason": pending_reason,
        "duration_note": duration_note,
        "traces": ev["traces"],
    }


def _attribution(members, confirmed, pending):
    if pending:
        return "pending"
    if len(confirmed) > 1:
        return "multiple"
    if len(members) == 1:
        return "source"
    links = {member["link"] for member in members}
    if confirmed and confirmed[0] in links:
        return "relay"
    if confirmed:
        return "same_source"
    return "multiple"


def _group(day, key, members, wiring_meta, records, extra_pending="", extra_note=""):
    pending = [member["pending_reason"] for member in members if member["pending_reason"]]
    if extra_pending:
        pending.append(extra_pending)
    pending = list(dict.fromkeys(pending))

    items, seen = [], set()
    for member in members:
        for item in member["suspected_sources"]:
            if item["node"] not in seen:
                seen.add(item["node"])
                items.append(item)
    items.sort(key=lambda item: (item["basis"] != "diary", item["node"]))
    confirmed = [item["node"] for item in items if item["basis"] == "diary"]
    signals = sorted({name for member in members for name in member["signal_roots"]})
    attribution = _attribution(members, confirmed, pending)
    note = extra_note or NOTE[attribution]
    if signals and attribution != "pending":
        note = (note + " " if note else "") + (
            f"接线根 {'、'.join(signals)} 是信号源，没有播控日记，该层只能人工核对。"
        )

    scope, scope_seen = [], set()
    for name in [member["link"] for member in members] + [item["node"] for item in items] + signals:
        if name not in scope_seen:
            scope_seen.add(name)
            scope.append(name)
    for member in members:
        for name in member["missing_upstreams"]:
            if name not in scope_seen:
                scope_seen.add(name)
                scope.append(name)

    wiring_meta = wiring_meta or {}
    snapshot = {}
    for name in sorted(scope):
        row = records.get(name)
        snapshot[name] = None if row is None else {
            "planned_min": row.get("planned_min"),
            "aired_min": row.get("aired_min"),
            "interruption_min": row.get("interruption_min"),
        }

    return {
        "day": day,
        "key": key,
        "status": "pending" if pending else "ready",
        "attribution": attribution,
        "same_source": len(members) > 1,
        "sources": items,
        "source_label": "、".join(item["node"] for item in items) if items else "待核",
        "confirmed_sources": confirmed,
        "signal_sources": signals,
        "pending_reason": "；".join(pending),
        "note": note,
        "scope": scope,
        "members": members,
        "evidence": {
            "day": day,
            "wiring_id": wiring_meta.get("id"),
            "wiring_effective_from": wiring_meta.get("effective_from"),
            "edges": wiring_meta.get("edges") or [],
            "sources": wiring_meta.get("sources") or [],
            "records": snapshot,
            "traces": {member["link"]: member["traces"] for member in members},
        },
    }


def compute_day(day, wiring, records, threshold=0.0, known=None, wiring_meta=None):
    """归并某一日的停播日记。

    wiring: 当日生效的接线快照，None 表示当日没有生效接线。
    records: {节点: 日记行}，只包含当日有报的节点。
    threshold: 停播判定阈值（分钟），停播分钟严格大于该值记为停播。
    known: 该批次中出现过日记的节点集合，用于区分“缺报”和“从未有日记”。
    """
    records = records or {}
    threshold = float(threshold or 0)
    if wiring_meta is None:
        edges, declared = normalize_wiring(wiring)
        wiring_meta = {
            "edges": [{"upstream": a, "downstream": b} for a, b in edges],
            "sources": declared,
        }
    outage_nodes = sorted(
        node for node, row in records.items()
        if interruption(row) is not None and interruption(row) > threshold
    )
    if not outage_nodes:
        return []

    empty = {"confirmed": set(), "signals": set(), "missing": [], "traces": []}
    if not wiring:
        return [_group(
            day, "no-wiring",
            [_member(node, day, records, {node: dict(empty)}, threshold) for node in outage_nodes],
            wiring_meta, records,
            extra_pending=f"{day} 没有生效接线，无法判断是否同源",
            extra_note="当日没有生效接线，无法判断同源。",
        )]

    edges, declared = normalize_wiring(wiring)
    parents = parent_map(edges)
    declared_set = set(declared)
    universe = set(declared_set)
    for upstream, downstream in edges:
        universe.add(upstream)
        universe.add(downstream)

    registered = [node for node in outage_nodes if node in universe]
    unregistered = [node for node in outage_nodes if node not in universe]
    groups = []

    if registered:
        evidence = {
            node: _resolve(node, records, parents, declared_set, known, threshold, ())
            for node in registered
        }
        for component in _components(registered, evidence):
            members = [_member(node, day, records, evidence, threshold) for node in component]
            confirmed = sorted({name for node in component for name in evidence[node]["confirmed"]})
            groups.append(_group(
                day,
                "|".join(confirmed) if confirmed else "|".join(component),
                members, wiring_meta, records,
            ))

    for node in unregistered:
        evidence = {node: _resolve(node, records, parents, declared_set, known, threshold, ())}
        groups.append(_group(
            day,
            f"unregistered:{node}",
            [_member(node, day, records, evidence, threshold)],
            wiring_meta, records,
            extra_pending=f"{node} 在 {day} 的接线未登记，无法判断是否同源",
            extra_note="该链路当日不在生效接线中，无法判断同源。",
        ))

    groups.sort(key=lambda item: item["key"])
    return groups


def affected(group, nodes):
    """补报或接线变更是否影响该组：成员、源头、待核缺报任一命中即需重算。"""
    scope = set(group.get("scope") or [])
    for member in group.get("members", []):
        scope.add(member["link"])
        scope.update(member.get("missing_upstreams") or [])
    scope.update(item["node"] for item in group.get("sources", []))
    return bool(scope & set(nodes))
