"""录制差异分析：把一次录制变成可审阅的候选。

设计目标（对应「政务网改版后定位到相邻字段、稳健分仍认可」的风险）：
- 不直接落库步骤，先按字段名称（target_desc）、动作 action、样例值 sample_value
  与当前生效版本做比对，输出 added / missing / suspected_misalign / updated / unchanged；
- 疑似错位只给线索不自动纠正，必须人工确认后才能进入发布；
- content_fingerprint 对录制内容做确定性哈希，重复导入同一份录制不新增批次/版本。

比对基准：当前已发布版本的 steps_snapshot；流程从未发布时回退到工作区 steps 表。
"""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher

from src.constants.enums import DiffType

# 录入类动作才可能把值填进相邻字段（点击/断言错位不直接造成材料填错）
FIELD_FILL_ACTIONS = {"input", "upload"}
# 名称相似度达到该值视为同一字段的不同写法（如「法人姓名」→「法定代表人姓名」之外的近形场景）
NAME_SIMILAR_THRESHOLD = 0.72
# 名称包含匹配的最小长度，避免单字误匹配
MIN_CONTAIN_LEN = 3

# 选择器里相邻字段的常见线索
_NEIGHBOR_PATTERNS = ("nth-child", "following-sibling", "preceding-sibling", "adjacent", "next(", "prev(")

# 中文字段名 ↔ 选择器英文片段的语义映射：
# 页面改版后定位器常只剩 class 片段（如 .field-agent），纯文本包含无法识别它指向「经办人」。
FIELD_KEYWORD_TOKENS: dict[str, tuple[str, ...]] = {
    "经办人": ("agent", "handler", "operator", "attn", "contact-person", "liaison"),
    "法人": ("legal", "frdb", "representative"),
    "法定代表人": ("legal", "frdb", "representative"),
    "联系电话": ("phone", "mobile", "tel"),
    "手机": ("phone", "mobile", "tel"),
    "邮箱": ("email", "mail"),
    "银行账号": ("bank", "account", "acct", "card-no"),
    "账号": ("account", "acct", "bank"),
    "企业名称": ("company", "corp", "ent-name", "firm"),
    "信用代码": ("credit", "creditcode", "tyshxydm"),
    "注册资本": ("capital", "regcap"),
    "地址": ("address", "addr"),
    "身份证": ("idcard", "id-card", "identity"),
    "参保人数": ("headcount", "insured", "staff-count"),
    "研发投入": ("rd", "research"),
}


def _field_keywords(name: str) -> list[str]:
    return [keyword for keyword in FIELD_KEYWORD_TOKENS if keyword in str(name or "")]


def _selector_semantic_targets(selector_text: str) -> set[str]:
    """从定位器文本反查它语义上指向哪些中文字段关键词。"""
    haystack = str(selector_text or "").lower()
    hits: set[str] = set()
    for keyword, tokens in FIELD_KEYWORD_TOKENS.items():
        if keyword in haystack or any(token in haystack for token in tokens):
            hits.add(keyword)
    return hits


def normalize_name(name: str) -> str:
    """归一化字段名称：去空白/标点/括号说明，便于名称匹配。"""
    text = str(name or "").strip().lower()
    text = re.sub(r"[\s（）()【】\[\]：:，,。.\-—_/\\]+", "", text)
    # 去掉「录入/上传/填写」等动作前缀，只保留字段名本体
    for prefix in ("录入", "填写", "输入", "上传", "选择"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text


def name_similar(left: str, right: str) -> float:
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= MIN_CONTAIN_LEN and (a in b or b in a):
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def names_match(left: str, right: str) -> bool:
    return name_similar(left, right) >= NAME_SIMILAR_THRESHOLD


def _first_selector(step: dict) -> str:
    candidates = step.get("locator_candidates") or step.get("locators") or []
    if candidates and isinstance(candidates[0], dict):
        return str(candidates[0].get("selector", ""))
    return ""


def _locator_text_haystack(step: dict) -> str:
    parts = []
    for candidate in (step.get("locator_candidates") or step.get("locators") or []):
        if isinstance(candidate, dict):
            parts.append(str(candidate.get("selector", "")))
            parts.append(str(candidate.get("desc", "")))
            parts.append(str(candidate.get("text", "")))
    return " ".join(parts)


def content_fingerprint(steps: list[dict], replace: bool) -> str:
    """录制内容的确定性指纹：同一份录制（含 replace 标志）重复导入得到相同指纹。"""
    payload = {
        "replace": bool(replace),
        "steps": [
            {
                "action": str(raw.get("action", "click")),
                "target_desc": str(raw.get("target_desc") or raw.get("target") or ""),
                "sample_value": str(raw.get("sample_value") or raw.get("sample") or ""),
                "locator_candidates": [
                    {
                        "strategy": str(item.get("strategy", "css")),
                        "selector": str(item.get("selector", "")),
                        "desc": str(item.get("desc", "")),
                    }
                    for item in (raw.get("locator_candidates") or raw.get("locators") or [])
                    if isinstance(item, dict)
                ],
            }
            for raw in steps
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _recording_view(raw: dict, index: int) -> dict:
    return {
        "seq": index,
        "action": str(raw.get("action", "click")),
        "field_name": str(raw.get("target_desc") or raw.get("target") or f"录制步骤 {index}")[:255],
        "sample_value": str(raw.get("sample_value") or raw.get("sample") or "")[:255],
        "selector": _first_selector(raw),
        "raw": raw,
    }


def _baseline_view(step: dict) -> dict:
    return {
        "seq": int(step.get("seq", 0)),
        "action": str(step.get("action", "")),
        "field_name": str(step.get("target_desc", "")),
        "sample_value": str(step.get("sample_value") or ""),
        "selector": _first_selector(step),
        "raw": step,
    }


def _same_action(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def _cross_match(rec_name: str, baseline: list[dict], exclude_seq: int) -> dict | None:
    """在同序号之外找到名称匹配的基线字段——错位的典型信号。"""
    best = None
    best_score = NAME_SIMILAR_THRESHOLD
    for base in baseline:
        if base["seq"] == exclude_seq:
            continue
        score = name_similar(rec_name, base["field_name"])
        if score >= best_score:
            best, best_score = base, score
    return best


def _selector_points_to_other_field(rec: dict, baseline: list[dict], same_seq_base: dict | None) -> dict | None:
    """定位器语义上指向另一个基线字段（如「法人姓名」步骤的选择器含 field-agent=经办人）。"""
    haystack = _locator_text_haystack(rec["raw"])
    if not haystack:
        return None
    semantic_targets = _selector_semantic_targets(haystack)
    rec_keywords = set(_field_keywords(rec["field_name"]))
    for base in baseline:
        if same_seq_base and base["seq"] == same_seq_base["seq"]:
            continue
        base_keywords = set(_field_keywords(base["field_name"]))
        # 定位器命中了别的字段的语义关键词，且这些关键词不属于本字段
        foreign = semantic_targets & base_keywords - rec_keywords
        if foreign:
            return base
        # 兜底：定位器文本直接出现其他字段名的中文词元
        lowered = haystack.lower()
        tokens = [token for token in re.split(r"[\s（）()【】\[\]：:，,。./\\_\-]+", base["field_name"])
                  if len(token) >= 2]
        if any(token.lower() in lowered for token in tokens):
            return base
    return None


def _neighbor_selector(rec: dict) -> bool:
    selector = rec["selector"].lower()
    return any(pattern in selector for pattern in _NEIGHBOR_PATTERNS)


def build_recording_diff(recorded_steps: list[dict], baseline_steps: list[dict]) -> list[dict]:
    """生成逐字段差异清单。返回的每一项都带字段名称、动作、样例值与判定理由。"""
    recorded = [_recording_view(raw, index) for index, raw in enumerate(recorded_steps, start=1)]
    baseline = [_baseline_view(step) for step in baseline_steps]
    baseline_by_seq = {item["seq"]: item for item in baseline}

    # 真正被录制覆盖的基线字段：同序号配对，或存在名称精确（归一化后）匹配。
    # 跨字段相似/语义命中只作为错位提示，不能让基线字段被当成「已覆盖」而漏报缺失。
    covered_base_seqs: set[int] = set()
    diff_items: list[dict] = []

    for rec in recorded:
        same_seq = baseline_by_seq.get(rec["seq"])
        cross = _cross_match(rec["field_name"], baseline, rec["seq"])
        selector_other = _selector_points_to_other_field(rec, baseline, same_seq)

        item = {
            "type": DiffType.UNCHANGED.value,
            "seq": rec["seq"],
            "field_name": rec["field_name"],
            "action": rec["action"],
            "sample_value": rec["sample_value"],
            "selector": rec["selector"],
            "baseline_seq": same_seq["seq"] if same_seq else None,
            "baseline_field_name": same_seq["field_name"] if same_seq else "",
            "baseline_action": same_seq["action"] if same_seq else "",
            "baseline_selector": same_seq["selector"] if same_seq else "",
            "reasons": [],
            "review_status": "unreviewed",
        }

        if same_seq is None:
            covered = next(
                (base for base in baseline if names_match(rec["field_name"], base["field_name"])),
                None,
            )
            if covered is not None:
                covered_base_seqs.add(covered["seq"])
            item["type"] = DiffType.ADDED.value
            item["reasons"].append("生效版本中没有同序号字段，属于新增步骤")
            if cross is not None:
                item["type"] = DiffType.SUSPECTED_MISALIGN.value
                item["reasons"].append(
                    f"字段名称与生效版本第 {cross['seq']} 步「{cross['field_name']}」高度相似，疑似错位/重排"
                )
            diff_items.append(item)
            continue

        covered_base_seqs.add(same_seq["seq"])
        name_ok = names_match(rec["field_name"], same_seq["field_name"])
        # 名称对不上时，再看录制里有没有其他步骤精确承接该基线字段；
        # 若没有，同序号基线字段实际已从本次录制中消失，应在后面按缺失报告。
        same_field_elsewhere = any(
            other is not rec and names_match(other["field_name"], same_seq["field_name"])
            for other in recorded
        )
        if not name_ok and not same_field_elsewhere:
            covered_base_seqs.discard(same_seq["seq"])

        if not name_ok and rec["action"] in FIELD_FILL_ACTIONS:
            item["type"] = DiffType.SUSPECTED_MISALIGN.value
            item["reasons"].append(
                f"同序号字段名称由「{same_seq['field_name']}」变为「{rec['field_name']}」，"
                "录入类动作可能把值填进相邻字段，稳健分无法识别此类语义错位；"
                f"原字段「{same_seq['field_name']}」将同时按缺失处理"
            )
            if cross is not None:
                item["reasons"].append(
                    f"新名称与生效版本第 {cross['seq']} 步「{cross['field_name']}」匹配，疑似定位到该相邻字段"
                )
        elif not name_ok:
            item["type"] = DiffType.UPDATED.value
            item["reasons"].append(f"字段名称变化：「{same_seq['field_name']}」→「{rec['field_name']}」")

        if not _same_action(rec["action"], same_seq["action"]):
            if item["type"] == DiffType.UNCHANGED.value:
                item["type"] = DiffType.UPDATED.value
            item["reasons"].append(f"动作变化：{same_seq['action']} → {rec['action']}")

        if selector_other is not None and item["type"] != DiffType.SUSPECTED_MISALIGN.value:
            item["type"] = DiffType.SUSPECTED_MISALIGN.value
            item["reasons"].append(
                f"定位器语义指向相邻字段「{selector_other['field_name']}」，疑似把值填进相邻输入框"
            )

        if item["type"] != DiffType.SUSPECTED_MISALIGN.value and _neighbor_selector(rec) and not name_ok:
            item["type"] = DiffType.SUSPECTED_MISALIGN.value
            item["reasons"].append("定位器依赖相邻节点结构（nth-child/兄弟轴），页面改版后极易漂移到相邻字段")

        if item["type"] == DiffType.UNCHANGED.value and rec["selector"] != same_seq["selector"]:
            item["type"] = DiffType.UPDATED.value
            item["reasons"].append("字段与动作未变，主定位器选择器有更新")

        if not item["reasons"]:
            item["reasons"].append("字段名称、动作与生效版本一致")

        diff_items.append(item)

    for base in baseline:
        if base["seq"] not in covered_base_seqs:
            diff_items.append({
                "type": DiffType.MISSING.value,
                "seq": None,
                "field_name": "",
                "action": "",
                "sample_value": "",
                "selector": "",
                "baseline_seq": base["seq"],
                "baseline_field_name": base["field_name"],
                "baseline_action": base["action"],
                "baseline_selector": base["selector"],
                "reasons": [f"生效版本第 {base['seq']} 步「{base['field_name']}」在本次录制中缺失"],
                "review_status": "unreviewed",
            })

    order = {
        DiffType.SUSPECTED_MISALIGN.value: 0,
        DiffType.ADDED.value: 1,
        DiffType.MISSING.value: 2,
        DiffType.UPDATED.value: 3,
        DiffType.UNCHANGED.value: 4,
    }
    diff_items.sort(key=lambda entry: (order[entry["type"]], entry.get("seq") or 9999,
                                       entry.get("baseline_seq") or 9999))
    return diff_items


def summarize_diff(diff_items: list[dict]) -> dict:
    counts = {kind.value: 0 for kind in DiffType}
    for item in diff_items:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return {
        "total": len(diff_items),
        "added": counts[DiffType.ADDED.value],
        "missing": counts[DiffType.MISSING.value],
        "suspected_misalign": counts[DiffType.SUSPECTED_MISALIGN.value],
        "updated": counts[DiffType.UPDATED.value],
        "unchanged": counts[DiffType.UNCHANGED.value],
        "requires_manual_confirmation": counts[DiffType.SUSPECTED_MISALIGN.value] > 0,
    }
