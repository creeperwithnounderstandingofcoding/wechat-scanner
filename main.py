#!/usr/bin/env python3
"""微信聊天记录扫描器 v3 — DB 解密方案

不需要关闭 SIP，不需要辅助功能权限。
只需要预先用 wechat-decrypt 工具提取密钥并解密数据库。

用法：
  python main.py              # 启动 GUI
  python main.py --cli        # CLI 模式
  python main.py --check      # 检查环境

前置步骤：
  1. sudo codesign --force --deep --sign - /Applications/WeChat.app
  2. cd ~/Desktop/feishu/wechat-decrypt && sudo ./find_all_keys_macos
  3. python decrypt_db.py

流程：
  1. 读取解密后的 DB（联系人、会话、消息）
  2. 用户选择要扫描的聊天（GUI 或 CLI）
  3. Claude 分析提取项目信息
  4. 确认后写入飞书 bitable
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import config
from extractor.db_reader import WeChatDBReader
from processor.pipeline import (
    area_scan,
    full_scan,
    format_messages_for_ai,
    area_scan_from_text,
    write_to_bitable,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("wechat_scanner.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


def check_environment() -> list[str]:
    """检查运行环境"""
    issues = []

    db_dir = config.DECRYPTED_DB_DIR
    if not db_dir.exists():
        issues.append(
            f"解密数据库目录不存在: {db_dir}\n"
            "  请先运行 wechat-decrypt 工具解密数据库:\n"
            "  1. cd ~/Desktop/feishu/wechat-decrypt\n"
            "  2. sudo ./find_all_keys_macos\n"
            "  3. python decrypt_db.py"
        )
        return issues

    # 检查关键数据库文件
    contact_db = db_dir / "contact" / "contact.db"
    session_db = db_dir / "session" / "session.db"
    if not contact_db.exists():
        issues.append(f"联系人数据库不存在: {contact_db}")
    if not session_db.exists():
        issues.append(f"会话数据库不存在: {session_db}")

    # 检查消息数据库
    msg_dbs = list((db_dir / "message").glob("message_*.db")) if (db_dir / "message").exists() else []
    if not msg_dbs:
        issues.append("未找到消息数据库 (message/message_*.db)")

    return issues


def run_cli():
    """CLI 模式"""
    print("=" * 60)
    print("  微信聊天记录扫描器 v3")
    print("  投资 Deal Flow 智能提取工具")
    print("  (DB 解密方案 — 无需关闭 SIP)")
    print("=" * 60)

    # 检查环境
    issues = check_environment()
    if issues:
        print("\n⚠️ 环境检查:")
        for issue in issues:
            print(f"  ❌ {issue}")
        sys.exit(1)

    print("\n✅ 环境检查通过")

    # 初始化 DB 读取器
    reader = WeChatDBReader(config.DECRYPTED_DB_DIR)
    contacts = reader.get_contacts()
    print(f"   加载 {len(contacts)} 个联系人")

    # 获取会话列表
    sessions = reader.get_sessions(limit=30)

    if not sessions:
        print("⚠️ 未找到会话记录。")
        sys.exit(1)

    # 显示会话列表
    print(f"\n最近 {len(sessions)} 个会话:")
    for i, s in enumerate(sessions, 1):
        count = reader.get_message_count(s["wxid"])
        group_tag = " [群]" if s["is_group"] else ""
        print(f"  {i:2d}. {s['display_name']:35s}{group_tag:5s} | {s['last_msg_time_str']} | {count} 条消息")

    # 选择聊天
    print("\n输入编号选择聊天（多个用逗号分隔，如 1,3,5）:")
    print("输入 a 扫描所有群聊")
    selection = input("> ").strip()

    if selection.lower() == "a":
        selected_sessions = [s for s in sessions if s["is_group"]]
    else:
        selected_indices = []
        for part in selection.replace("，", ",").split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(sessions):
                    selected_indices.append(idx)
        if not selected_indices:
            print("无效选择")
            return
        selected_sessions = [sessions[i] for i in selected_indices]

    print(f"\n将扫描 {len(selected_sessions)} 个聊天:")
    for s in selected_sessions:
        print(f"  - {s['display_name']}")

    # 选择扫描模式
    print("\n扫描模式:")
    print("1. 区域扫描 — 分析最近 30 条消息（快速）")
    print("2. 全局扫描 — 扫描全部历史消息（全面，消耗更多 token）")
    mode = input("选择 (1/2): ").strip()

    # 执行扫描
    all_results = []
    for s in selected_sessions:
        print(f"\n{'=' * 50}")
        print(f"扫描: {s['display_name']}")
        print(f"{'=' * 50}")

        def _progress(msg):
            print(f"  {msg}")

        if mode == "2":
            results = full_scan(reader, s["wxid"], s["display_name"],
                                batch_size=config.BATCH_SIZE, progress_cb=_progress)
            for r in results:
                all_results.append({"chat": s["display_name"], "result": r})
        else:
            result = area_scan(reader, s["wxid"], s["display_name"],
                               window_size=config.CONTEXT_WINDOW_SIZE, progress_cb=_progress)
            if result is None:
                pass
            elif isinstance(result, list):
                for r in result:
                    all_results.append({"chat": s["display_name"], "result": r})
            elif isinstance(result, dict) and result.get("meeting_type") != "无关内容":
                all_results.append({"chat": s["display_name"], "result": result})

    _show_results_and_write(all_results)


def _show_results_and_write(all_results: list[dict]):
    """展示结果并可选写入 bitable"""
    print(f"\n{'=' * 60}")
    print(f"扫描完成！发现 {len(all_results)} 个项目信息")
    print(f"{'=' * 60}")

    if not all_results:
        print("未发现项目相关内容。")
        return

    for i, item in enumerate(all_results, 1):
        r = item.get("result", item)
        print(f"\n{i}. {r.get('company_name', '未知')}")
        print(f"   来源: {item.get('chat', '微信')}")
        print(f"   类型: {r.get('meeting_type', '')}")
        print(f"   赛道: {r.get('industry', '')}")
        print(f"   摘要: {r.get('summary', '')[:200]}")
        print(f"   置信度: {r.get('confidence', 'low')}")

    if not config.BITABLE_APP_TOKEN:
        print("\n(BITABLE 未配置，无法写入)")
        _save_local(all_results)
        return

    choice = input(f"\n将 {len(all_results)} 条结果写入飞书 Deal Flow 表格？(y/n): ").strip().lower()
    if choice in ("y", "yes", "是"):
        success = 0
        for item in all_results:
            r = item.get("result", item)
            if write_to_bitable(r, item.get("chat", "")).get("ok"):
                success += 1
                print(f"  ✅ {r.get('company_name', '未知')}")
            else:
                print(f"  ❌ {r.get('company_name', '未知')}")
        print(f"\n写入完成: {success}/{len(all_results)} 条成功")
    else:
        _save_local(all_results)


def _save_local(results: list[dict]):
    """保存结果到本地 JSON"""
    output_file = Path("data") / f"scan_{int(time.time())}.json"
    output_file.parent.mkdir(exist_ok=True)
    output_file.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"结果已保存到: {output_file}")


# ===== GUI 模式 =====

def run_gui():
    """启动 tkinter GUI"""
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox, scrolledtext
    except ImportError:
        print("tkinter 不可用，切换到 CLI 模式")
        run_cli()
        return

    import threading

    root = tk.Tk()
    root.title("微信扫描器 — Deal Flow 智能提取 v3")
    root.geometry("780x650")

    # 标题
    title_label = tk.Label(root, text="微信聊天记录扫描器", font=("", 18, "bold"))
    title_label.pack(pady=10)
    subtitle_label = tk.Label(root, text="DB 解密方案 | 选择聊天 → AI 分析 → 入库飞书", font=("", 12))
    subtitle_label.pack()

    # 状态
    status_var = tk.StringVar(value="正在加载数据库...")
    status_label = tk.Label(root, textvariable=status_var, fg="gray", font=("", 11))
    status_label.pack(pady=5)

    # 聊天列表
    list_frame = tk.Frame(root)
    list_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=5)

    list_label = tk.Label(list_frame, text="会话列表（点击选择，Cmd+点击多选）:", anchor="w")
    list_label.pack(fill=tk.X)

    columns = ("name", "time", "msgs", "type")
    tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=14, selectmode="extended")
    tree.heading("name", text="名称")
    tree.heading("time", text="最后消息")
    tree.heading("msgs", text="消息数")
    tree.heading("type", text="类型")
    tree.column("name", width=280)
    tree.column("time", width=130)
    tree.column("msgs", width=80, anchor="center")
    tree.column("type", width=60, anchor="center")

    scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    session_data = []
    reader_ref = [None]

    # 日志区域
    log_frame = tk.LabelFrame(root, text="日志", padx=5, pady=5)
    log_frame.pack(fill=tk.X, padx=20, pady=5)
    log_text = scrolledtext.ScrolledText(log_frame, height=6, font=("Menlo", 11))
    log_text.pack(fill=tk.X)

    def log(msg):
        """线程安全的日志输出"""
        def _do():
            log_text.insert(tk.END, msg + "\n")
            log_text.see(tk.END)
        root.after(0, _do)

    def set_status(msg):
        root.after(0, lambda: status_var.set(msg))

    # 按钮区
    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=10)

    def load_data():
        """在后台线程加载解密数据库"""
        def _worker():
            issues = check_environment()
            if issues:
                set_status("⚠️ " + issues[0].split("\n")[0])
                root.after(0, lambda: messagebox.showwarning("环境检查", "\n".join(issues)))
                return

            set_status("正在加载数据库...")
            reader = WeChatDBReader(config.DECRYPTED_DB_DIR)
            contacts = reader.get_contacts()
            reader_ref[0] = reader
            log(f"加载 {len(contacts)} 个联系人")

            sessions = reader.get_sessions(limit=40)

            # 获取消息计数（这是慢操作）
            rows = []
            for idx, s in enumerate(sessions):
                count = reader.get_message_count(s["wxid"])
                tag = "群聊" if s["is_group"] else "私聊"
                rows.append((s, count, tag))
                if idx % 5 == 0:
                    set_status(f"正在加载... ({idx+1}/{len(sessions)})")

            # 回到主线程更新 UI
            def _update_ui():
                tree.delete(*tree.get_children())
                session_data.clear()
                for s, count, tag in rows:
                    tree.insert("", tk.END, values=(
                        s["display_name"], s["last_msg_time_str"], count, tag
                    ))
                    session_data.append(s)
                status_var.set(f"✅ 加载完成 | {len(sessions)} 个会话 | 选择聊天后点击扫描")
                log(f"加载 {len(sessions)} 个会话，就绪")

            root.after(0, _update_ui)

        threading.Thread(target=_worker, daemon=True).start()

    def scan_selected(deep=False):
        selected = tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先选择要扫描的聊天（点击行来选择，Cmd+点击多选）")
            return

        reader = reader_ref[0]
        if not reader:
            messagebox.showerror("错误", "数据库尚未加载完成")
            return

        indices = [tree.index(item) for item in selected]
        selected_sessions = [session_data[i] for i in indices]
        mode_str = "全局" if deep else "区域"

        def _worker():
            log(f"\n开始{mode_str}扫描 {len(selected_sessions)} 个聊天...")
            set_status(f"正在{mode_str}扫描...")

            all_results = []
            for s in selected_sessions:
                log(f"扫描: {s['display_name']}...")

                if deep:
                    results = full_scan(reader, s["wxid"], s["display_name"],
                                        batch_size=config.BATCH_SIZE, progress_cb=log)
                    for r in results:
                        all_results.append({"chat": s["display_name"], "result": r})
                else:
                    result = area_scan(reader, s["wxid"], s["display_name"],
                                       window_size=config.CONTEXT_WINDOW_SIZE, progress_cb=log)
                    if result and result.get("meeting_type") != "无关内容":
                        all_results.append({"chat": s["display_name"], "result": result})

            def _show():
                if all_results:
                    log(f"\n发现 {len(all_results)} 个项目信息!")
                    _show_results_gui(root, all_results, log)
                else:
                    log("未发现项目相关内容。")
                    messagebox.showinfo("结果", "未发现项目相关内容")
                status_var.set("扫描完成")

            root.after(0, _show)

        threading.Thread(target=_worker, daemon=True).start()

    def preview_chat():
        """预览选中聊天的消息"""
        selected = tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先选择一个聊天")
            return

        reader = reader_ref[0]
        if not reader:
            return

        idx = tree.index(selected[0])
        s = session_data[idx]
        messages = reader.get_messages(s["wxid"], limit=20)

        log(f"\n=== 预览: {s['display_name']} (最近 {len(messages)} 条) ===")
        for m in messages[-20:]:
            sender = m.get("sender", "") or "?"
            content = m.get("content", "")[:100]
            log(f"[{m['time_str']}] {sender}: {content}")

    ttk.Button(btn_frame, text="预览消息", command=preview_chat).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="区域扫描 (快速)", command=lambda: scan_selected(deep=False)).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="全局扫描 (全面)", command=lambda: scan_selected(deep=True)).pack(side=tk.LEFT, padx=5)

    # 强制渲染一帧后再加载数据
    root.update_idletasks()
    root.after(500, load_data)
    root.mainloop()


def _show_results_gui(parent, results: list[dict], log_fn=None):
    """在新窗口显示扫描结果"""
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox, scrolledtext
    except ImportError:
        return

    win = tk.Toplevel(parent)
    win.title(f"扫描结果 — {len(results)} 个项目")
    win.geometry("650x500")

    text = scrolledtext.ScrolledText(win, font=("", 12))
    text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    for i, item in enumerate(results, 1):
        r = item.get("result", item)
        text.insert(tk.END, f"{i}. {r.get('company_name', '未知')}\n")
        text.insert(tk.END, f"   来源: {item.get('chat', '微信')}\n")
        text.insert(tk.END, f"   类型: {r.get('meeting_type', '')}\n")
        text.insert(tk.END, f"   赛道: {r.get('industry', '')}\n")
        text.insert(tk.END, f"   摘要: {r.get('summary', '')[:200]}\n")
        text.insert(tk.END, f"   置信度: {r.get('confidence', 'low')}\n\n")

    def do_write():
        success = 0
        for item in results:
            r = item.get("result", item)
            if write_to_bitable(r, item.get("chat", "")).get("ok"):
                success += 1
        messagebox.showinfo("写入完成", f"成功写入 {success}/{len(results)} 条记录")
        if log_fn:
            log_fn(f"写入完成: {success}/{len(results)} 条")
        win.destroy()

    btn_frame = tk.Frame(win)
    btn_frame.pack(pady=10)
    ttk.Button(btn_frame, text="✅ 全部写入飞书表格", command=do_write).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="💾 保存到本地", command=lambda: [_save_local(results), win.destroy()]).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="关闭", command=win.destroy).pack(side=tk.LEFT, padx=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="微信聊天记录扫描器 v3")
    parser.add_argument("--cli", action="store_true", help="CLI 模式")
    parser.add_argument("--check", action="store_true", help="只检查环境")
    args = parser.parse_args()

    if args.check:
        issues = check_environment()
        if issues:
            for i in issues:
                print(f"❌ {i}")
            sys.exit(1)
        print("✅ 环境检查通过")
        sys.exit(0)

    if args.cli:
        run_cli()
    else:
        run_gui()
