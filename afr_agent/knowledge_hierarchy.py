"""把校读过的论证结构转换成只引用已入账记录的阅读索引。"""

from . import semantic

TITLE_LIMIT = 100
REASON_LIMIT = 260
SCOPE_LIMIT = 160
MAX_BRANCHES = 4
MAX_ARGUMENTS = 6


def _text(value, limit, *, empty=False):
    return (isinstance(value, str) and (empty or bool(value)) and value == value.strip()
            and len(value) <= limit and not any(ord(char) < 32 for char in value))


def candidate(branches, events, normalize):
    """模型只能以序号指向本次主题的校验结果，路径和锚点由记录提供。"""
    if not isinstance(branches, list) or not 1 <= len(branches) <= MAX_BRANCHES:
        return []
    result = []
    titles = set()
    reasons = set()
    for branch in branches:
        if not isinstance(branch, dict) or set(branch) != {"title", "reason", "arguments"}:
            continue
        title, reason, arguments = branch["title"], branch["reason"], branch["arguments"]
        if (not _text(title, TITLE_LIMIT) or not _text(reason, REASON_LIMIT)
                or not isinstance(arguments, list) or not 1 <= len(arguments) <= MAX_ARGUMENTS):
            continue
        title_key, reason_key = normalize(title), normalize(reason)
        if not title_key or not reason_key or title_key in titles or reason_key in reasons or title_key == reason_key:
            continue
        accepted = []
        for argument in arguments:
            if not isinstance(argument, dict) or set(argument) != {"event_number", "title", "reason", "scope"}:
                continue
            number = argument["event_number"]
            if (type(number) is not int or not 1 <= number <= len(events)
                    or not _text(argument["title"], TITLE_LIMIT) or not _text(argument["reason"], REASON_LIMIT)
                    or not _text(argument["scope"], SCOPE_LIMIT, empty=True)):
                continue
            argument_title, argument_reason = normalize(argument["title"]), normalize(argument["reason"])
            if (not argument_title or not argument_reason or argument_title in {title_key, argument_reason}
                    or argument_reason in {title_key, reason_key}):
                continue
            accepted.append({key: argument[key] for key in ("title", "reason", "scope")}
                            | {"event": events[number - 1]["id"]})
        if accepted:
            titles.add(title_key)
            reasons.add(reason_key)
            result.append({"id": semantic.fingerprint(title_key)[:16], "title": title, "reason": reason,
                           "arguments": accepted})
    return result


def admitted(title, reason, branches, history, new_ids, normalize, *, prior=None):
    """以本地有效记录续接旧论证，本批有效新增决定是否接纳新的论证结构。"""
    superseded = {event["supersedes"] for event in history if event["supersedes"]}
    active = {event["id"] for event in history if event["kind"] != "撤回"} - superseded
    retained = {}
    seen_events = set()
    seen_titles = set()
    seen_reasons = set()
    for branch in (prior or {}).get("branches", ()):
        arguments = [argument for argument in branch["arguments"] if argument["event"] in active]
        if not arguments:
            continue
        preserved = {**branch, "arguments": arguments}
        match_title = branch.get("match_title", branch["title"])
        if len(arguments) < len(branch["arguments"]):
            preserved.update(match_title=match_title, title=arguments[0]["title"], reason=arguments[0]["reason"])
        retained[normalize(match_title)] = preserved
        seen_events.update(argument["event"] for argument in arguments)
        seen_titles.update(normalize(argument["title"]) for argument in arguments)
        seen_reasons.update(normalize(argument["reason"]) for argument in arguments)

    additions = dict(retained)
    selected_fresh = False
    for branch in branches:
        key = normalize(branch["title"])
        existing = additions.get(key)
        if existing is None and len(additions) >= MAX_BRANCHES:
            continue
        accepted = []
        for argument in branch["arguments"]:
            event = argument["event"]
            title_key, reason_key = normalize(argument["title"]), normalize(argument["reason"])
            if (event not in active or event in seen_events
                    or title_key in seen_titles or reason_key in seen_reasons):
                continue
            accepted.append(argument)
            seen_events.add(event)
            seen_titles.add(title_key)
            seen_reasons.add(reason_key)
        if not accepted:
            continue
        selected_fresh = selected_fresh or any(argument["event"] in new_ids for argument in accepted)
        if existing:
            additions[key] = {**existing, "arguments": [*existing["arguments"], *accepted]}
        else:
            branch_reason = branch["reason"] if len(accepted) == len(branch["arguments"]) else accepted[0]["reason"]
            additions[key] = {**branch, "reason": branch_reason, "arguments": accepted}
    if selected_fresh and (retained or len(additions) >= 2):
        retained = additions
    if not retained:
        return None
    return {"title": title, "reason": reason, "branches": list(retained.values()), "generated": True}
