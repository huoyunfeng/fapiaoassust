"""去重引擎（数据不变量 I1 / I2）。

纯函数：新发票指纹 vs 已存在指纹集合 -> 命中信息或 None。
- I1: 同一文件 hash 只能存在一条发票（重复文件在导入入口直接拦截）；
- I2: 同一 (invoice_code, invoice_no) 只能存在一条有效发票
  （DUPLICATE 状态的存量记录不参与判重，见仓储层索引注释）。
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Fingerprint:
    file_hash: str
    invoice_code: str = ""
    invoice_no: str = ""


@dataclass
class DuplicateHit:
    kind: str          # file_hash / code_no
    existing_id: int
    message: str


class DedupEngine:
    """纯函数判重。existing 为 [(id, file_hash, invoice_code, invoice_no)]。"""

    def check(self, fp: Fingerprint, existing: list[tuple]) -> Optional[DuplicateHit]:
        # I1: 文件 hash 精确唯一
        for row_id, file_hash, _, _ in existing:
            if file_hash and fp.file_hash and file_hash == fp.file_hash:
                return DuplicateHit(
                    "file_hash", row_id, f"与发票 #{row_id} 文件内容完全相同（I1: 文件哈希唯一）"
                )
        # I2: (invoice_code, invoice_no) 唯一；数电发票无代码（code 为空）时
        # 退化为仅按发票号判重（20 位数电号码全局唯一）
        if fp.invoice_no:
            for row_id, _, code, no in existing:
                if no and code == fp.invoice_code and no == fp.invoice_no:
                    return DuplicateHit(
                        "code_no", row_id,
                        f"与发票 #{row_id} 发票代码+号码相同（I2: 代码+号码唯一）",
                    )
        return None
