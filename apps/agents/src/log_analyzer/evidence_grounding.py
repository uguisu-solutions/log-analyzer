"""証拠グラウンディング検証（決定的・LLM 不使用）。

integrator が出した根本原因候補の evidence / summary に引用された
「具体的な識別子」（id=N / workspace_id=N / IP / MAC 等）が、AI が実際に
見た入力コーパス（圧縮後の log_text ＋ 取得済みログ）に **実在するか** を
機械照合する。実在しなければ「でっち上げ（hallucinated specificity）」の
疑いとして警告し、確信度に上限をかける。

狙い（原因④: 証拠のでっち上げ・自信過剰）:
- GPT 監査が毎回指摘してきた「id=91171 は提示ログ内で確認できない」型の
  過剰に具体的な主張を、人手でなく決定的に検出する。
- 2-a（プロンプトでの証拠規律）を通り抜けて残った捏造の安全ネット。

設計（誤検知を避ける）:
- 照合対象は **識別子系のみ**（id/workspace_id/user_id 等の数値、IP、MAC）。
  件数（「55 件」）は圧縮サマリ（×N 件）と突き合わせが曖昧なので **対象外**。
- 照合は語境界つき（`\b` 相当）。値がコーパスのどこかに実在すれば grounded と
  みなす（緩め＝安全側。真にどこにも無い値だけを ungrounded とする）。
- コード由来の記述（クラス名/メソッド名）は識別子パターンに当たらないので
  誤って ungrounded 判定しない。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


def _enabled() -> bool:
    raw = (os.environ.get("EVIDENCE_GROUNDING_ENABLED") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _conf_cap() -> float:
    """未接地の具体値が見つかったときに確信度へかける上限。"""
    raw = os.environ.get("EVIDENCE_GROUNDING_CONF_CAP")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 0.6


# ─── 識別子アトムの抽出 ─────────────────────────────────────────────

# id 系のキー = 数値。ログの主キー/外部キーとして引用されがちなもの。
_ID_KV = re.compile(
    r"(?P<key>ai_document_id|document_id|workspace_id|analysis_id|user_id|doc_id|uid|ws|id)"
    r"\s*[=:]\s*(?P<val>\d+)",
    re.IGNORECASE,
)
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")


@dataclass(frozen=True)
class Atom:
    label: str   # 表示用（例: "workspace_id=91171"）
    needle: str   # コーパス照合用の値（例: "91171"）


def extract_atoms(text: str) -> list[Atom]:
    """テキストから照合対象の識別子アトムを抽出（重複除去、出現順保持）。"""
    if not text:
        return []
    seen: set[tuple[str, str]] = set()
    atoms: list[Atom] = []

    def _add(label: str, needle: str) -> None:
        key = (label, needle)
        if key not in seen:
            seen.add(key)
            atoms.append(Atom(label=label, needle=needle))

    for m in _ID_KV.finditer(text):
        key = m.group("key").lower()
        val = m.group("val")
        _add(f"{key}={val}", val)
    for m in _IPV4.finditer(text):
        _add(m.group(0), m.group(0))
    for m in _MAC.finditer(text):
        _add(m.group(0), m.group(0))
    return atoms


def _in_corpus(needle: str, corpus: str) -> bool:
    """needle が語境界つきでコーパスに実在するか。"""
    if not needle:
        return True
    return re.search(r"(?<![\w.]){}(?![\w.])".format(re.escape(needle)), corpus) is not None


@dataclass
class GroundingReport:
    total_atoms: int = 0
    grounded: int = 0
    ungrounded: list[str] = field(default_factory=list)  # 未接地アトムの label

    @property
    def has_ungrounded(self) -> bool:
        return bool(self.ungrounded)

    @property
    def grounding_rate(self) -> float:
        if self.total_atoms == 0:
            return 1.0
        return self.grounded / self.total_atoms


def check_grounding(candidates: list[dict], corpus: str) -> GroundingReport:
    """根本原因候補の summary/evidence に引用された識別子アトムを対象に、
    コーパス（AI が見た入力）への接地状況を検証する。"""
    report = GroundingReport()
    if not _enabled():
        return report

    seen_needles: set[str] = set()
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        parts = [str(cand.get("summary") or "")]
        ev = cand.get("evidence")
        if isinstance(ev, list):
            parts.extend(str(x) for x in ev)
        text = "\n".join(parts)
        for atom in extract_atoms(text):
            if atom.needle in seen_needles:
                continue
            seen_needles.add(atom.needle)
            report.total_atoms += 1
            if _in_corpus(atom.needle, corpus):
                report.grounded += 1
            else:
                report.ungrounded.append(atom.label)
    return report


def apply_grounding(
    candidates: list[dict],
    confidence: float,
    corpus: str,
) -> tuple[float, GroundingReport]:
    """グラウンディング検証を実行し、(調整後 confidence, レポート) を返す。

    未接地の具体値がある場合、確信度に上限をかける（安全ネット）。無効時は
    confidence をそのまま返す。
    """
    report = check_grounding(candidates, corpus)
    if report.has_ungrounded:
        confidence = min(confidence, _conf_cap())
    return confidence, report
