#!/usr/bin/env python3
"""
configure_regions.py
Android端末のスクリーンショットを取得してtkinterウィンドウに表示し、
クリックで「更新ボタンのタップ位置」または(ドラッグで)「OCR切り出し範囲」を
視覚的に指定して calib.json に保存する。

main.py は起動時にこのファイルを自動で読み込むので、
座標をコードに手書きする必要はない。

使い方:
  python3 configure_regions.py
  (ウィンドウ内)
    1. 📷再撮影  : Android端末の現在画面を取得して表示
    2. モードを選択してから
         - 「タップ位置を指定」: 1クリックで位置を指定(赤い十字マーカー)
         - 「OCR切り出し範囲を指定」: ドラッグで範囲を指定(緑の枠)
    3. ▶テストタップ: 指定したタップ位置を実際にadbでタップして確認(タップモードのみ)
    4. 💾保存       : calib.json に書き込み(既存の値は項目ごとに上書き)

前提: adb でAndroid端末に接続済みであること。Pillow(PIL)が必要。
"""

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Optional, Tuple

from PIL import ImageTk

from adb_device import adb_screencap, adb_tap

CALIB_FILE = Path(__file__).resolve().parent / "calib.json"
MAX_DISPLAY_H = 900  # ウィンドウに収めるための表示上限高さ(px)


class CalibApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("待ち数監視ツール キャリブレーション")

        self.mode = tk.StringVar(value="tap")
        self.scale = 1.0
        self.tap_point: Optional[Tuple[int, int]] = None  # 端末座標
        self.crop_box: Optional[Tuple[int, int, int, int]] = None  # 端末座標
        self.drag_start: Optional[Tuple[int, int]] = None  # canvas座標
        self.marker_ids: list = []

        top = tk.Frame(root)
        top.pack(fill="x")
        tk.Button(top, text="📷再撮影", command=self.refresh_screenshot).pack(side="left", padx=4, pady=4)
        tk.Radiobutton(top, text="タップ位置を指定", variable=self.mode, value="tap").pack(side="left")
        tk.Radiobutton(top, text="OCR切り出し範囲を指定", variable=self.mode, value="crop").pack(side="left")
        tk.Button(top, text="▶テストタップ", command=self.test_tap).pack(side="left", padx=4)
        tk.Button(top, text="💾保存", command=self.save).pack(side="left", padx=4)

        self.status = tk.Label(root, text="まずは📷再撮影を押してください", anchor="w")
        self.status.pack(fill="x", padx=4)

        self.canvas = tk.Canvas(root, bg="#222")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        self.refresh_screenshot()

    # ---- スクショ取得・表示 ----
    def refresh_screenshot(self) -> None:
        try:
            img = adb_screencap()
        except Exception as e:
            messagebox.showerror(
                "スクショ取得に失敗",
                f"USBデバッグが接続先デバイスから許可されていることを確認してください: {e}",
            )
            return
        w, h = img.size
        scale = min(1.0, MAX_DISPLAY_H / h)
        self.scale = scale
        disp = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        self.tk_img = ImageTk.PhotoImage(disp)
        self.canvas.config(width=disp.width, height=disp.height)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)
        self.marker_ids = []
        self.status.config(text=f"撮影完了 (元解像度 {w}x{h}, 表示倍率 {scale:.2f})")

    # ---- 座標変換(表示canvas座標 -> 端末ピクセル座標) ----
    def canvas_to_device(self, cx: float, cy: float) -> Tuple[int, int]:
        return int(cx / self.scale), int(cy / self.scale)

    # ---- マウス操作 ----
    def on_press(self, event) -> None:
        self.drag_start = (event.x, event.y)
        if self.mode.get() == "tap":
            self.set_tap_marker(event.x, event.y)

    def on_drag(self, event) -> None:
        if self.mode.get() == "crop" and self.drag_start:
            self.draw_rect_preview(self.drag_start, (event.x, event.y))

    def on_release(self, event) -> None:
        if self.mode.get() == "crop" and self.drag_start:
            x0, y0 = self.canvas_to_device(*self.drag_start)
            x1, y1 = self.canvas_to_device(event.x, event.y)
            self.crop_box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            self.status.config(text=f"OCR切り出し範囲(端末座標): {self.crop_box}")
        self.drag_start = None

    def set_tap_marker(self, cx: float, cy: float) -> None:
        for mid in self.marker_ids:
            self.canvas.delete(mid)
        r = 6
        self.marker_ids = [
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline="red", width=2),
            self.canvas.create_line(cx - r * 2, cy, cx + r * 2, cy, fill="red"),
            self.canvas.create_line(cx, cy - r * 2, cx, cy + r * 2, fill="red"),
        ]
        self.tap_point = self.canvas_to_device(cx, cy)
        self.status.config(text=f"タップ位置(端末座標): {self.tap_point}")

    def draw_rect_preview(self, start, current) -> None:
        self.canvas.delete("rect_preview")
        self.canvas.create_rectangle(*start, *current, outline="lime", width=2, tags="rect_preview")

    # ---- ボタン動作 ----
    def test_tap(self) -> None:
        if self.mode.get() != "tap" or not self.tap_point:
            messagebox.showinfo("確認", "「タップ位置を指定」モードで位置を決めてからテストしてください")
            return
        adb_tap(*self.tap_point)
        self.status.config(text=f"{self.tap_point} をタップしました。少し待って📷再撮影で結果を確認してください")

    def save(self) -> None:
        data = {}
        if CALIB_FILE.exists():
            try:
                data = json.loads(CALIB_FILE.read_text())
            except Exception:
                data = {}
        if self.tap_point:
            data["tap_pos"] = list(self.tap_point)
        if self.crop_box:
            data["crop_box"] = list(self.crop_box)
        if not data:
            messagebox.showinfo("確認", "タップ位置か切り出し範囲を先に指定してください")
            return
        CALIB_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        messagebox.showinfo("保存完了", f"{CALIB_FILE} に保存しました:\n{json.dumps(data)}")


if __name__ == "__main__":
    root = tk.Tk()
    CalibApp(root)
    root.mainloop()
