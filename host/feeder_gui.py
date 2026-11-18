"""feeder_gui.py - Photon 飞达管理助手（GUI）

功能：
  - 串口连接：USB-RS485 直连，或经主板 M485 桥接
  - 扫描总线：0xC3 找未编址飞达 + 逐地址 0x01 找已编址飞达
  - 修改槽位：0xC2 按 UUID 写入新地址，不影响其它飞达
  - 闪灯定位：0xC1 让选中的飞达闪灯，方便对照实物

打包 EXE：pyinstaller --noconfirm --onefile --windowed --name Photon飞达管理助手 host/feeder_gui.py
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import photon_protocol as pp
from feeder_cli import M485Transport, SerialTransport


def uuid_text(u: bytes) -> str:
    return "-".join(f"{b:02X}" for b in u)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Photon 飞达管理助手")
        self.geometry("900x600")
        self.minsize(820, 520)

        self.t = None                 # 传输层（SerialTransport / M485Transport）
        self.feeders: dict[bytes, dict] = {}
        self.busy = False
        self.q: queue.Queue = queue.Queue()

        self._build()
        self.refresh_ports()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- 界面 ----------------
    def _build(self):
        top = ttk.LabelFrame(self, text="连接")
        top.pack(fill="x", padx=8, pady=6)

        ttk.Label(top, text="串口").grid(row=0, column=0, padx=4, pady=6)
        self.port_var = tk.StringVar(value="COM9")
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var, width=10)
        self.port_cb.grid(row=0, column=1, padx=4)
        ttk.Button(top, text="刷新", command=self.refresh_ports).grid(row=0, column=2, padx=4)

        self.m485_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="M485 桥接（主板串口）", variable=self.m485_var).grid(
            row=0, column=3, padx=8)

        self.conn_btn = ttk.Button(top, text="打开", command=self.toggle_conn)
        self.conn_btn.grid(row=0, column=4, padx=8)
        self.conn_lbl = ttk.Label(top, text="未连接", foreground="gray")
        self.conn_lbl.grid(row=0, column=5, padx=8)

        mid = ttk.LabelFrame(self, text="飞达列表")
        mid.pack(fill="both", expand=True, padx=8, pady=4)

        bar = ttk.Frame(mid)
        bar.pack(fill="x", pady=4)
        self.scan_btn = ttk.Button(bar, text="扫描飞达", command=self.scan)
        self.scan_btn.pack(side="left", padx=4)
        ttk.Label(bar, text="地址扫描上限").pack(side="left", padx=(16, 2))
        self.range_var = tk.StringVar(value="20")
        ttk.Spinbox(bar, from_=1, to=254, textvariable=self.range_var, width=5).pack(side="left")
        self.id_btn = ttk.Button(bar, text="闪灯定位", command=self.identify)
        self.id_btn.pack(side="left", padx=16)

        cols = ("slot", "uuid", "state")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=12)
        self.tree.heading("slot", text="槽位")
        self.tree.heading("uuid", text="UUID（Hardware ID）")
        self.tree.heading("state", text="状态")
        self.tree.column("slot", width=70, anchor="center")
        self.tree.column("uuid", width=420, anchor="center")
        self.tree.column("state", width=120, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", lambda e: self.identify())

        edit = ttk.LabelFrame(self, text="修改槽位（先选中一行）")
        edit.pack(fill="x", padx=8, pady=4)
        ttk.Label(edit, text="新槽位").pack(side="left", padx=6, pady=6)
        self.slot_var = tk.StringVar(value="1")
        ttk.Spinbox(edit, from_=1, to=254, textvariable=self.slot_var, width=6).pack(side="left")
        self.prog_btn = ttk.Button(edit, text="写入槽位", command=self.program)
        self.prog_btn.pack(side="left", padx=12)
        ttk.Label(edit, text="写入后请在 OpenPnP 中同步修改槽位与 Hardware ID",
                  foreground="gray").pack(side="left", padx=12)

        self.log = tk.Text(self, height=9, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="x", padx=8, pady=(2, 2))
        self.status = ttk.Label(self, text="就绪", anchor="w")
        self.status.pack(fill="x", padx=8, pady=(0, 4))

    # ---------------- 工具 ----------------
    def log_msg(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, busy: bool, text: str = ""):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.scan_btn, self.prog_btn, self.id_btn, self.conn_btn):
            b.configure(state=state)
        self.status.configure(text=text or ("就绪" if not busy else "处理中..."))

    def refresh_ports(self):
        try:
            from serial.tools import list_ports
            ports = sorted(p.device for p in list_ports.comports())
        except Exception:
            ports = []
        self.port_cb.configure(values=ports)
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def toggle_conn(self):
        if self.t is not None:
            try:
                self.t.close()
            except Exception:
                pass
            self.t = None
            self.conn_btn.configure(text="打开")
            self.conn_lbl.configure(text="未连接", foreground="gray")
            self.log_msg("串口已关闭")
            return
        port = self.port_var.get().strip()
        try:
            if self.m485_var.get():
                self.t = M485Transport(port)
            else:
                self.t = SerialTransport(port)
        except SystemExit as exc:
            messagebox.showerror("打开失败", str(exc))
            return
        self.conn_btn.configure(text="关闭")
        self.conn_lbl.configure(text=f"已连接 {port}", foreground="green")
        self.log_msg(f"已打开 {port}（{'M485 桥接' if self.m485_var.get() else 'RS485 直连'}）")

    def selected_uuid(self) -> bytes | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选中一台飞达")
            return None
        return bytes.fromhex(sel[0].replace("-", ""))

    def on_select(self, _event=None):
        sel = self.tree.selection()
        if sel:
            self.slot_var.set(self.tree.item(sel[0], "values")[0])

    def on_close(self):
        if self.t is not None:
            try:
                self.t.close()
            except Exception:
                pass
        self.destroy()

    # ---------------- 后台任务 ----------------
    def _run(self, fn, *args):
        if self.busy:
            return
        if self.t is None:
            messagebox.showinfo("提示", "请先打开串口")
            return
        self.set_busy(True)
        threading.Thread(target=self._worker, args=(fn, args), daemon=True).start()

    def _worker(self, fn, args):
        try:
            fn(*args)
        except Exception as exc:                       # 串口异常等统一进日志
            self.q.put(("log", f"出错：{exc}"))
        finally:
            self.q.put(("busy", False))

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.log_msg(data)
                elif kind == "busy":
                    self.set_busy(data)
                elif kind == "progress":
                    self.status.configure(text=data)
                elif kind == "scan_done":
                    self._fill_tree(data)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---------------- 扫描 ----------------
    def scan(self):
        try:
            max_addr = max(1, min(254, int(self.range_var.get())))
        except ValueError:
            messagebox.showinfo("提示", "地址扫描上限必须是数字")
            return
        self._run(self._scan_worker, max_addr)

    def _scan_worker(self, max_addr: int):
        found: dict[bytes, dict] = {}
        pid = 0x40
        self.q.put(("progress", "扫描未编址飞达（0xC3）..."))
        for _ in range(3):
            for p in self.t.transact(pp.uninitialized_feeders_respond(pid), timeout=0.15):
                if len(p.payload) >= 13:
                    found[p.payload[1:13]] = {"addr": p.frm, "state": "未编址"}
            pid = (pid + 1) & 0xFF
        for addr in range(1, max_addr + 1):
            self.q.put(("progress", f"扫描地址 {addr}/{max_addr} ..."))
            for p in self.t.transact(pp.get_feeder_id(addr, pid), timeout=0.12):
                if len(p.payload) >= 13:
                    found[p.payload[1:13]] = {"addr": p.frm, "state": "已编址"}
            pid = (pid + 1) & 0xFF
        self.q.put(("log", f"扫描完成：发现 {len(found)} 台飞达"))
        self.q.put(("scan_done", found))

    def _fill_tree(self, found: dict[bytes, dict]):
        self.feeders = found
        self.tree.delete(*self.tree.get_children())
        for u, info in sorted(found.items(), key=lambda kv: kv[1]["addr"]):
            self.tree.insert("", "end", iid=uuid_text(u),
                             values=(info["addr"], uuid_text(u), info["state"]))

    # ---------------- 写槽位 / 定位 ----------------
    def program(self):
        u = self.selected_uuid()
        if u is None:
            return
        try:
            new_addr = int(self.slot_var.get())
        except ValueError:
            messagebox.showinfo("提示", "槽位必须是数字")
            return
        if not 1 <= new_addr <= 254:
            messagebox.showinfo("提示", "槽位范围 1~254")
            return
        used = {info["addr"]: other for other, info in self.feeders.items() if other != u}
        if new_addr in used:
            if not messagebox.askyesno("槽位冲突",
                    f"槽位 {new_addr} 已被 {uuid_text(used[new_addr])} 占用，仍要写入吗？\n"
                    "两台同地址会导致总线冲突！"):
                return
        if not messagebox.askyesno("确认", f"将 {uuid_text(u)}\n写入槽位 {new_addr}？"):
            return
        self._run(self._program_worker, u, new_addr)

    def _program_worker(self, u: bytes, new_addr: int):
        r = self.t.transact(pp.program_feeder_floor(u, new_addr, 0x50))
        if r and r[0].payload and r[0].payload[0] == pp.STATUS_OK:
            self.q.put(("log", f"写入成功：{uuid_text(u)} → 槽位 {new_addr}"))
            self.q.put(("progress", "写入成功，正在重新扫描..."))
            self._scan_worker(max(20, new_addr))
        else:
            self.q.put(("log", "写入失败：无应答或状态错误"))

    def identify(self):
        u = self.selected_uuid()
        if u is not None:
            self._run(self._identify_worker, u)

    def _identify_worker(self, u: bytes):
        r = self.t.transact(pp.identify_feeder(u, 0x51))
        self.q.put(("log", f"闪灯定位：{uuid_text(u)} "
                           f"{'已应答，飞达应闪灯 3 下' if r else '无应答'}"))


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
