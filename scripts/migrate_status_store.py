"""
一次性迁移 data/status_store.json：把旧的两态/五态平铺格式升级成新的细化状态。

规则：
- "applied"  -> "applied_confirmed"（过去就是"认为投成功了"，没有更细信息可回溯，当 confirmed）
- 纯字符串值 -> {"status": ..., "reason": "migrated", "updated_at": <now>}
- "new" / "viewed" / "contacted" / "ignored" -> 原样保留（Tab A 商机也在用），
  只包装成 dict 格式
- 已经是新格式（dict）的记录 -> 不动

迁移前把原文件备份成 status_store.json.bak-<时间戳>。

安全默认：不加参数 = 只预览（dry run），不写任何东西。确认预览结果没问题后，
再加 --apply 才会真正落盘（落盘前一定先备份成 status_store.json.bak-<时间戳>）。

跑法：python -m scripts.migrate_status_store            （预览，默认）
      python -m scripts.migrate_status_store --apply    （确认后执行）
"""
import json
import os
import shutil
import sys
from datetime import datetime, timezone

STATUS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "status_store.json")

_MAP = {"applied": "applied_confirmed"}


def migrate(dry_run: bool = False) -> dict:
    if not os.path.exists(STATUS_FILE):
        print(f"没有 {STATUS_FILE}，无需迁移（还没有任何投递/状态记录）。")
        return {"migrated": 0, "unchanged": 0}

    with open(STATUS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    now = datetime.now(timezone.utc).isoformat()
    out: dict = {}
    migrated = unchanged = 0
    for rid, raw in data.items():
        if isinstance(raw, dict) and "status" in raw:
            out[rid] = raw
            unchanged += 1
            continue
        old = raw if isinstance(raw, str) else "new"
        new = _MAP.get(old, old)
        out[rid] = {
            "status": new,
            "reason": "migrated" + (f":{old}->{new}" if new != old else ""),
            "updated_at": now,
        }
        migrated += 1
        if new != old:
            print(f"  {rid}: {old!r} -> {new!r}")

    print(f"\n共 {len(data)} 条：迁移 {migrated} 条，已是新格式 {unchanged} 条。")

    if dry_run:
        print("（预览模式，未写入。确认无误后加 --apply 执行。）")
        return {"migrated": migrated, "unchanged": unchanged}

    backup = STATUS_FILE + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(STATUS_FILE, backup)
    print(f"原文件已备份到 {backup}")
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("迁移完成。")
    return {"migrated": migrated, "unchanged": unchanged}


if __name__ == "__main__":
    # 默认 dry run；只有显式 --apply 才真正写入
    migrate(dry_run="--apply" not in sys.argv)
