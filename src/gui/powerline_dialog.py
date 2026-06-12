"""
GUI - 电力线坐标录入对话框

支持: 新增、编辑、删除电力线段
"""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import List, Dict, Optional


class PowerLineDialog(tk.Toplevel):
    """电力线管理对话框"""

    def __init__(self, parent, power_lines: List[Dict],
                 on_save: callable = None):
        super().__init__(parent)
        self.title("电力线管理")
        self.geometry("750x520")
        self.resizable(True, True)
        self.configure(bg="#1e1e2e")

        self.power_lines = power_lines
        self.on_save = on_save
        self.selected_index = None

        self._build_ui()
        self._refresh_list()

        # 模态
        self.transient(parent)
        self.grab_set()

    def _build_ui(self):
        """构建界面"""
        # 标题
        header = tk.Label(
            self, text="电力线段管理",
            font=("Microsoft YaHei", 14, "bold"),
            bg="#1e1e2e", fg="#cdd6f4"
        )
        header.pack(pady=(10, 5))

        # 主容器
        main_frame = tk.Frame(self, bg="#1e1e2e")
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 左侧: 列表
        list_frame = tk.LabelFrame(
            main_frame, text=" 电力线段列表 ",
            font=("Microsoft YaHei", 10),
            bg="#1e1e2e", fg="#a6adc8",
            foreground="#a6adc8"
        )
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        # Treeview
        columns = ("name", "coords")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings",
            height=12, selectmode="browse"
        )
        self.tree.heading("name", text="名称")
        self.tree.heading("coords", text="端点坐标")
        self.tree.column("name", width=120)
        self.tree.column("coords", width=300)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # 列表按钮
        list_btn_frame = tk.Frame(list_frame, bg="#1e1e2e")
        list_btn_frame.pack(fill=tk.X, pady=(5, 0))

        tk.Button(
            list_btn_frame, text="+ 新增", command=self._add_new,
            bg="#a6e3a1", fg="#1e1e2e", font=("Microsoft YaHei", 9, "bold"),
            relief=tk.FLAT, padx=12, pady=4
        ).pack(side=tk.LEFT, padx=2)

        tk.Button(
            list_btn_frame, text="− 删除", command=self._delete_selected,
            bg="#f38ba8", fg="#1e1e2e", font=("Microsoft YaHei", 9, "bold"),
            relief=tk.FLAT, padx=12, pady=4
        ).pack(side=tk.LEFT, padx=2)

        # 右侧: 编辑表单
        edit_frame = tk.LabelFrame(
            main_frame, text=" 编辑电力线段 ",
            font=("Microsoft YaHei", 10),
            bg="#1e1e2e", fg="#a6adc8",
            foreground="#a6adc8"
        )
        edit_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(5, 0))

        # 名称
        tk.Label(edit_frame, text="线段名称:", bg="#1e1e2e", fg="#cdd6f4",
                 font=("Microsoft YaHei", 9)).pack(anchor=tk.W, pady=(10, 2))
        self.name_var = tk.StringVar()
        tk.Entry(
            edit_frame, textvariable=self.name_var,
            font=("Consolas", 10), bg="#313244", fg="#cdd6f4",
            insertbackground="#cdd6f4", relief=tk.FLAT
        ).pack(fill=tk.X, padx=10, ipady=3)

        # 端点 1
        tk.Label(edit_frame, text="端点 1 (纬度, 经度, 海拔):", bg="#1e1e2e", fg="#a6adc8",
                 font=("Microsoft YaHei", 8)).pack(anchor=tk.W, pady=(10, 2), padx=10)

        p1_frame = tk.Frame(edit_frame, bg="#1e1e2e")
        p1_frame.pack(fill=tk.X, padx=10)

        self.lat1_var = tk.StringVar()
        self.lon1_var = tk.StringVar()
        self.alt1_var = tk.StringVar()

        for label, var in [("纬度", self.lat1_var), ("经度", self.lon1_var), ("海拔(m)", self.alt1_var)]:
            f = tk.Frame(p1_frame, bg="#1e1e2e")
            f.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
            tk.Label(f, text=label, bg="#1e1e2e", fg="#6c7086", font=("Microsoft YaHei", 7)).pack()
            tk.Entry(
                f, textvariable=var, font=("Consolas", 10),
                bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                relief=tk.FLAT, width=12
            ).pack(fill=tk.X, ipady=2)

        # 端点 2
        tk.Label(edit_frame, text="端点 2 (纬度, 经度, 海拔):", bg="#1e1e2e", fg="#a6adc8",
                 font=("Microsoft YaHei", 8)).pack(anchor=tk.W, pady=(10, 2), padx=10)

        p2_frame = tk.Frame(edit_frame, bg="#1e1e2e")
        p2_frame.pack(fill=tk.X, padx=10)

        self.lat2_var = tk.StringVar()
        self.lon2_var = tk.StringVar()
        self.alt2_var = tk.StringVar()

        for label, var in [("纬度", self.lat2_var), ("经度", self.lon2_var), ("海拔(m)", self.alt2_var)]:
            f = tk.Frame(p2_frame, bg="#1e1e2e")
            f.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
            tk.Label(f, text=label, bg="#1e1e2e", fg="#6c7086", font=("Microsoft YaHei", 7)).pack()
            tk.Entry(
                f, textvariable=var, font=("Consolas", 10),
                bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                relief=tk.FLAT, width=12
            ).pack(fill=tk.X, ipady=2)

        # 按钮
        btn_frame = tk.Frame(edit_frame, bg="#1e1e2e")
        btn_frame.pack(fill=tk.X, padx=10, pady=(15, 10))

        tk.Button(
            btn_frame, text="保存修改", command=self._save_current,
            bg="#89b4fa", fg="#1e1e2e", font=("Microsoft YaHei", 10, "bold"),
            relief=tk.FLAT, padx=16, pady=6
        ).pack(side=tk.LEFT, padx=2)

        tk.Button(
            btn_frame, text="清空表单", command=self._clear_form,
            bg="#45475a", fg="#cdd6f4", font=("Microsoft YaHei", 9),
            relief=tk.FLAT, padx=12, pady=6
        ).pack(side=tk.LEFT, padx=5)

        # 底部按钮
        bottom_frame = tk.Frame(self, bg="#1e1e2e")
        bottom_frame.pack(fill=tk.X, padx=10, pady=(5, 10))

        tk.Button(
            bottom_frame, text="保存全部并关闭", command=self._save_all,
            bg="#a6e3a1", fg="#1e1e2e", font=("Microsoft YaHei", 11, "bold"),
            relief=tk.FLAT, padx=20, pady=8
        ).pack(side=tk.RIGHT, padx=5)

        tk.Button(
            bottom_frame, text="取消", command=self.destroy,
            bg="#585b70", fg="#cdd6f4", font=("Microsoft YaHei", 10),
            relief=tk.FLAT, padx=16, pady=8
        ).pack(side=tk.RIGHT, padx=5)

    def _refresh_list(self):
        """刷新列表"""
        for item in self.tree.get_children():
            self.tree.delete(item)

        for i, line in enumerate(self.power_lines):
            name = line.get("name", f"线段{i+1}")
            coords = (
                f"({line.get('lat1',0):.5f}, {line.get('lon1',0):.5f}, {line.get('alt1',0):.0f}m) → "
                f"({line.get('lat2',0):.5f}, {line.get('lon2',0):.5f}, {line.get('alt2',0):.0f}m)"
            )
            self.tree.insert("", tk.END, iid=str(i), values=(name, coords))

    def _on_select(self, event):
        """选择列表项"""
        selection = self.tree.selection()
        if not selection:
            return

        idx = int(selection[0])
        self.selected_index = idx
        line = self.power_lines[idx]

        self.name_var.set(line.get("name", ""))
        self.lat1_var.set(str(line.get("lat1", "")))
        self.lon1_var.set(str(line.get("lon1", "")))
        self.alt1_var.set(str(line.get("alt1", "")))
        self.lat2_var.set(str(line.get("lat2", "")))
        self.lon2_var.set(str(line.get("lon2", "")))
        self.alt2_var.set(str(line.get("alt2", "")))

    def _clear_form(self):
        """清空表单"""
        self.selected_index = None
        self.name_var.set("")
        self.lat1_var.set("")
        self.lon1_var.set("")
        self.alt1_var.set("")
        self.lat2_var.set("")
        self.lon2_var.set("")
        self.alt2_var.set("")
        self.tree.selection_remove(self.tree.selection())

    def _add_new(self):
        """新增线段"""
        self._clear_form()
        self.name_var.set(f"新线段{len(self.power_lines)+1}")

    def _delete_selected(self):
        """删除选中线段"""
        if self.selected_index is None:
            messagebox.showwarning("提示", "请先选择要删除的线段")
            return

        if messagebox.askyesno("确认", "确定要删除选中的电力线段吗?"):
            del self.power_lines[self.selected_index]
            self._clear_form()
            self._refresh_list()

    def _save_current(self):
        """保存当前编辑的线段"""
        try:
            line_data = {
                "name": self.name_var.get().strip(),
                "lat1": float(self.lat1_var.get()),
                "lon1": float(self.lon1_var.get()),
                "alt1": float(self.alt1_var.get()),
                "lat2": float(self.lat2_var.get()),
                "lon2": float(self.lon2_var.get()),
                "alt2": float(self.alt2_var.get()),
            }
        except ValueError:
            messagebox.showerror("错误", "请输入有效的数值坐标")
            return

        if not line_data["name"]:
            messagebox.showerror("错误", "请输入线段名称")
            return

        if self.selected_index is not None:
            self.power_lines[self.selected_index] = line_data
        else:
            self.power_lines.append(line_data)

        self._refresh_list()

    def _save_all(self):
        """保存全部并关闭"""
        if self.on_save:
            self.on_save(self.power_lines)
        self.destroy()
